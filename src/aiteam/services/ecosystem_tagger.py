"""Ecosystem three-layer tagging system — Layer 1 (topics) + Layer 2 (rules) + Layer 3 (LLM).

Design points:
- Layer 1: GitHub topics mapped directly, confidence=0.95, source=GITHUB_TOPIC
- Layer 2: name+description+topics keyword/regex rule matching, confidence=0.7, source=AUTO_RULE
- Layer 3: LLM fallback — this service returns a dispatch_plan; after the Leader dispatches sub-agents, results are
written back
  via apply_llm_tags(), which receives the sub-agents' JSON output (tags + confidence) and writes source=AUTO_LLM
- Unified application: tag_repo() / tag_repos_batch() automatically run Layer 1+2 and mark which repos need Layer 3
- Does not call the LLM API directly (saves tokens); the CC main process schedules the sub-agents

See build_llm_dispatch_plan() for the Layer 3 sub-agent prompt template.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from aiteam.services.agent_template_registry import check_subagent_types
from aiteam.services.ecosystem_tag_rules import (
    GITHUB_TOPIC_ALIASES,
    KEYWORD_RULES,
    match_docs_only_by_language,
    match_github_topics,
    match_keyword_rules,
    match_language_tag,
)
from aiteam.storage.repository import StorageRepository
from aiteam.types import (
    EcosystemRepoTag,
    EcosystemTag,
    EcosystemTagSource,
)

logger = logging.getLogger(__name__)


# ============================================================
# Layer 3 LLM sub-agent dispatch constants
# ============================================================

# The maximum concurrent sub-agents per batch, to avoid a token spike
MAX_LLM_CONCURRENCY: int = 20

# Default subagent_type for Layer 3. It must be a type the Agent tool really
# accepts: CC rejects unknown ones outright ("Agent type '<x>' not found"), which
# kills the whole dispatch plan. 'general-purpose' is a CC built-in (present even
# without the OS plugin installed) and carries tools='*' — required here because
# the write-back tool ecosystem_tag_apply_llm_result is explicitly excluded from
# several OS templates.
DEFAULT_LLM_AGENT_TEMPLATE: str = "general-purpose"

# Dispatch charter: orchestrate with Fable, execute with Opus. 'general-purpose'
# has no template frontmatter to carry a model, so pin it here — otherwise the
# sub-agent silently inherits the orchestrator's higher-tier model.
DEFAULT_LLM_AGENT_MODEL: str = "opus"

# When the Layer 1 / 2 hit count is below this threshold, a Layer 3 fallback is deemed necessary
LLM_FALLBACK_TAG_THRESHOLD: int = 2

# Layer 1 / 2 / 3's respective default confidences
CONFIDENCE_TOPIC: float = 0.95
CONFIDENCE_RULE: float = 0.7
CONFIDENCE_LANGUAGE: float = 0.9  # K4: language field is highly reliable
CONFIDENCE_LLM_DEFAULT: float = 0.6

# Layer 3 Sub-agent prompt template (< 500 words)
_LLM_TAGGER_PROMPT_TEMPLATE = """\
You are an ecosystem repo tagging sub-agent. Read the metadata of the repo below and select the """ \
    """1-5 best-matching tags from the allowed tag list.

Repo info:
- Full name: {repo_full_name}
- Description: {description}
- Existing topics: {topics}
- Primary language: {language}
- Existing tags: {existing_tags}

Allowed tags (select only from this list):
{allowed_tags}

Output strict JSON (no markdown wrapping, no explanation):
{{"tags": [{{"name": "memory_system", "confidence": 0.85}}, ...]}}

Requirements:
1. confidence range 0.0-1.0; select only tags you are confident in at >= 0.5
2. Do not invent new tags; anything not in the allowed list is not selected
3. At most 5 tags
4. If there is no high-confidence match, return {{"tags": []}}
"""


# ============================================================
# Result data structures
# ============================================================

@dataclass
class TagApplyResult:
    """One repo's tagging result."""

    repo_id: str
    repo_full_name: str
    layer1_tags: list[str] = field(default_factory=list)  # tag name hit via GitHub topics
    layer2_tags: list[str] = field(default_factory=list)  # tag name hit via rules
    layer3_tags: list[str] = field(default_factory=list)  # tag name hit via the LLM
    skipped_unknown: list[str] = field(default_factory=list)  # tags output by a sub-agent but with no dictionary
    # counterpart
    needs_llm: bool = False  # True means the Layer 1+2 hit count is below the threshold

    def total_applied(self) -> int:
        return len(self.layer1_tags) + len(self.layer2_tags) + len(self.layer3_tags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "repo_full_name": self.repo_full_name,
            "layer1_tags": self.layer1_tags,
            "layer2_tags": self.layer2_tags,
            "layer3_tags": self.layer3_tags,
            "skipped_unknown": self.skipped_unknown,
            "needs_llm": self.needs_llm,
            "total_applied": self.total_applied(),
        }


@dataclass
class BatchApplyStats:
    """Batch tagging statistics."""

    repos_processed: int = 0
    layer1_applied: int = 0
    layer2_applied: int = 0
    layer3_applied: int = 0
    repos_needing_llm: int = 0
    repos_failed: int = 0
    by_repo: list[TagApplyResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repos_processed": self.repos_processed,
            "layer1_applied": self.layer1_applied,
            "layer2_applied": self.layer2_applied,
            "layer3_applied": self.layer3_applied,
            "repos_needing_llm": self.repos_needing_llm,
            "repos_failed": self.repos_failed,
            "by_repo": [r.to_dict() for r in self.by_repo],
        }


# ============================================================
# Tagger Service
# ============================================================

class EcosystemTagger:
    """Three-layer tagging service, interfacing with StorageRepository.

    Usage:
      tagger = EcosystemTagger(repo)
      result = await tagger.tag_repo(repo_profile)
      stats = await tagger.tag_repos_batch(profiles)
      plan = tagger.build_llm_dispatch_plan(repos_needing_llm)
      await tagger.apply_llm_tags(repo_id, [{"name": "memory_system", "confidence": 0.85}], agent_id)
    """

    def __init__(
        self,
        repo: StorageRepository,
        project_id: str = "",
    ) -> None:
        """Initialize the tagging service.

        Args:
            repo: the data access layer.
            project_id: optional project scope; when empty, repo._project_scope is passed through.
                All repo-tag association writes carry this project id.
        """
        self._repo = repo
        self._project_id = project_id or repo._project_scope or ""
        self._tag_cache: dict[str, EcosystemTag] | None = None  # name -> tag

    async def _load_tag_dict(self) -> dict[str, EcosystemTag]:
        """Load and cache the whole tag dictionary (indexed by name)."""
        if self._tag_cache is None:
            tags = await self._repo.list_tags(limit=500)
            self._tag_cache = {t.name: t for t in tags}
        return self._tag_cache

    def invalidate_cache(self) -> None:
        """Clear the tag cache — called after the upstream manually adds tags."""
        self._tag_cache = None

    async def _resolve_tag_name_to_id(self, name: str) -> str | None:
        """Resolve a tag name to a tag id; returns None when unregistered."""
        cache = await self._load_tag_dict()
        tag = cache.get(name)
        return tag.id if tag else None

    # --------------------------------------------------------
    # Public single-repo entry
    # --------------------------------------------------------

    async def tag_repo(
        self,
        *,
        repo_id: str,
        repo_full_name: str,
        name: str,
        description: str | None,
        topics: list[str],
        owner: str = "",
        language: str | None = None,
        agent_id: str | None = "ecosystem-tagger",
        replace_auto: bool = False,
    ) -> TagApplyResult:
        """Run Layer 1 + 2 automatic tagging on a single repo, writing EcosystemRepoTag rows.

        Layer 3 (LLM) is not triggered in this method; it only returns the needs_llm flag for the caller to decide
        whether to dispatch.

        K4: adds direct mapping of the language field (CONFIDENCE_LANGUAGE=0.9), and when the primary language is a
        document type
        such as Jupyter Notebook / TeX / HTML, infers the docs_only tag.

        K4 follow-up: with ``replace_auto=True``, first delete that repo's old tags with source ∈
        {github_topic, auto_rule} before writing; manual / auto_llm are kept.
        Used to clean stale automatic tags after a rule upgrade.
        """
        result = TagApplyResult(repo_id=repo_id, repo_full_name=repo_full_name)
        cache = await self._load_tag_dict()

        if replace_auto:
            await self._repo.delete_repo_tags_by_sources(
                repo_id=repo_id,
                sources=[
                    EcosystemTagSource.GITHUB_TOPIC.value,
                    EcosystemTagSource.AUTO_RULE.value,
                ],
                project_id=self._project_id or None,
            )

        # Layer 1: GitHub topics
        layer1 = match_github_topics(topics)
        for tag_name in layer1:
            if tag_name not in cache:
                result.skipped_unknown.append(tag_name)
                continue
            await self._repo.add_repo_tag(
                EcosystemRepoTag(
                    project_id=self._project_id or None,
                    repo_id=repo_id,
                    tag_id=cache[tag_name].id,
                    confidence=CONFIDENCE_TOPIC,
                    source=EcosystemTagSource.GITHUB_TOPIC,
                    agent_id=agent_id,
                ),
                project_id=self._project_id or None,
            )
            result.layer1_tags.append(tag_name)

        # Layer 2a: language field -> tech_stack tag (high confidence)
        lang_match = match_language_tag(language)
        for tag_name in lang_match:
            if tag_name in layer1:
                continue
            if tag_name not in cache:
                result.skipped_unknown.append(tag_name)
                continue
            await self._repo.add_repo_tag(
                EcosystemRepoTag(
                    project_id=self._project_id or None,
                    repo_id=repo_id,
                    tag_id=cache[tag_name].id,
                    confidence=CONFIDENCE_LANGUAGE,
                    source=EcosystemTagSource.AUTO_RULE,
                    agent_id=agent_id,
                ),
                project_id=self._project_id or None,
            )
            result.layer2_tags.append(tag_name)

        # Layer 2b: docs_only inference from language
        if match_docs_only_by_language(language) and "docs_only" not in result.layer2_tags:
            if "docs_only" in cache:
                await self._repo.add_repo_tag(
                    EcosystemRepoTag(
                        project_id=self._project_id or None,
                        repo_id=repo_id,
                        tag_id=cache["docs_only"].id,
                        confidence=CONFIDENCE_LANGUAGE,
                        source=EcosystemTagSource.AUTO_RULE,
                        agent_id=agent_id,
                    ),
                    project_id=self._project_id or None,
                )
                result.layer2_tags.append("docs_only")

        # Layer 2c: keyword rules — skip tags already hit by Layer 1+2a/b
        layer2 = match_keyword_rules(name=name, description=description, topics=topics, owner=owner)
        already = set(layer1) | set(result.layer2_tags)
        for tag_name in layer2:
            if tag_name in already:
                continue
            if tag_name not in cache:
                result.skipped_unknown.append(tag_name)
                continue
            await self._repo.add_repo_tag(
                EcosystemRepoTag(
                    project_id=self._project_id or None,
                    repo_id=repo_id,
                    tag_id=cache[tag_name].id,
                    confidence=CONFIDENCE_RULE,
                    source=EcosystemTagSource.AUTO_RULE,
                    agent_id=agent_id,
                ),
                project_id=self._project_id or None,
            )
            result.layer2_tags.append(tag_name)

        if result.total_applied() < LLM_FALLBACK_TAG_THRESHOLD:
            result.needs_llm = True
        return result

    async def tag_repos_batch(
        self,
        repos: list[dict[str, Any]],
        *,
        agent_id: str | None = "ecosystem-tagger",
        replace_auto: bool = False,
    ) -> BatchApplyStats:
        """Batch tagging.

        Each repos element must contain: id, repo_full_name, name, description, topics, owner, language.

        ``replace_auto=True`` is passed through to the single-repo tag_repo, for batch-cleaning stale automatic tags
        after a rule upgrade.
        """
        stats = BatchApplyStats()
        for r in repos:
            try:
                tr = await self.tag_repo(
                    repo_id=r["id"],
                    repo_full_name=r["repo_full_name"],
                    name=r.get("name", ""),
                    description=r.get("description"),
                    topics=r.get("topics") or [],
                    owner=r.get("owner", ""),
                    language=r.get("language"),
                    agent_id=agent_id,
                    replace_auto=replace_auto,
                )
                stats.by_repo.append(tr)
                stats.layer1_applied += len(tr.layer1_tags)
                stats.layer2_applied += len(tr.layer2_tags)
                if tr.needs_llm:
                    stats.repos_needing_llm += 1
            except Exception as exc:
                logger.warning("tag_repo failed for %s: %s", r.get("repo_full_name", "?"), exc)
                stats.repos_failed += 1
            stats.repos_processed += 1
        return stats

    # --------------------------------------------------------
    # Layer 3: LLM dispatch_plan + result apply
    # --------------------------------------------------------

    async def build_llm_dispatch_plan(
        self,
        repos: list[dict[str, Any]],
        *,
        agent_template: str = DEFAULT_LLM_AGENT_TEMPLATE,
        max_concurrency: int = MAX_LLM_CONCURRENCY,
    ) -> dict[str, Any]:
        """Generate a dispatch plan for repos needing the Layer 3 LLM fallback.

        On receiving it, the Leader spawns sub-agents item by item per the plan["dispatch"] list,
        and after the sub-agents finish, calls ecosystem_tag_apply_llm_result (an MCP tool) to write the results back.

        agent_template is checked once against the template source of truth: a hit passes; a miss is **let through
        as-is** and reported
        in the plan's top-level template_warnings — templates are accelerators, not a whitelist; CC's built-in types
        cannot be enumerated, and whether to switch to a nearby template is decided by the person reading the plan.
        """
        cache = await self._load_tag_dict()
        allowed_tags = sorted(cache.keys())
        allowed_brief = "\n".join(f"- {n} ({cache[n].category.value})" for n in allowed_tags)

        # Truncate to max_concurrency
        batch = repos[:max_concurrency]
        skipped = max(len(repos) - max_concurrency, 0)

        dispatch: list[dict[str, Any]] = []
        for r in batch:
            existing_tags_rows = await self._repo.list_repo_tags(repo_id=r["id"])
            existing_names: list[str] = []
            for row in existing_tags_rows:
                t = await self._repo.get_tag(row.tag_id)
                if t:
                    existing_names.append(t.name)

            prompt = _LLM_TAGGER_PROMPT_TEMPLATE.format(
                repo_full_name=r.get("repo_full_name", "?"),
                description=(r.get("description") or "(none)")[:300],
                topics=", ".join(r.get("topics") or []) or "(none)",
                language=r.get("language") or "(unknown)",
                existing_tags=", ".join(existing_names) or "(none)",
                allowed_tags=allowed_brief,
            )
            dispatch.append({
                "repo_id": r["id"],
                "repo_full_name": r.get("repo_full_name"),
                "launch_call": {
                    "tool": "Agent",
                    "params": {
                        # No team_name: CC v2.1.219 deprecates and ignores it —
                        # the session owns a single implicit team and OS-side
                        # attribution is handled by SubagentStart auto-enrolment.
                        "subagent_type": agent_template,
                        "model": DEFAULT_LLM_AGENT_MODEL,
                        "description": f"Tag fallback-{r.get('repo_full_name', '?')}",
                        "prompt": prompt,
                    },
                },
            })

        # Report-only: the dispatch above already carries the caller's own template.
        template_warnings = check_subagent_types(
            [(agent_template, f"all {len(dispatch)} dispatch items")]
        )
        instructions = (
            "The Leader calls each dispatch[i].launch_call in sequence to dispatch sub-agents."
            "After a sub-agent outputs its JSON, it submits via the MCP tool ecosystem_tag_apply_llm_result"
            ": repo_id + the tags array."
        )
        for w in template_warnings:
            instructions += f"\n⚠️ {w['message']}"

        return {
            "agent_template": agent_template,
            "max_concurrency": max_concurrency,
            "total_requested": len(repos),
            "dispatched": len(dispatch),
            "skipped_due_to_limit": skipped,
            "dispatch": dispatch,
            "template_warnings": template_warnings,
            "instructions": instructions,
        }

    async def apply_llm_tags(
        self,
        *,
        repo_id: str,
        repo_full_name: str,
        llm_output_tags: list[dict[str, Any]],
        agent_id: str | None = None,
    ) -> TagApplyResult:
        """Receive Layer 3 sub-agent output and write EcosystemRepoTag (source=AUTO_LLM).

        llm_output_tags: list of {"name": str, "confidence": float}
        Tags not in the dictionary are uniformly ignored and recorded in skipped_unknown.
        """
        cache = await self._load_tag_dict()
        result = TagApplyResult(repo_id=repo_id, repo_full_name=repo_full_name)

        for entry in llm_output_tags:
            name = (entry.get("name") or "").strip()
            try:
                conf = float(entry.get("confidence", CONFIDENCE_LLM_DEFAULT))
            except (TypeError, ValueError):
                conf = CONFIDENCE_LLM_DEFAULT
            conf = max(0.0, min(1.0, conf))
            if not name:
                continue
            if name not in cache:
                result.skipped_unknown.append(name)
                continue
            await self._repo.add_repo_tag(
                EcosystemRepoTag(
                    project_id=self._project_id or None,
                    repo_id=repo_id,
                    tag_id=cache[name].id,
                    confidence=conf,
                    source=EcosystemTagSource.AUTO_LLM,
                    agent_id=agent_id,
                ),
                project_id=self._project_id or None,
            )
            result.layer3_tags.append(name)
        return result

    # --------------------------------------------------------
    # Manual tagging
    # --------------------------------------------------------

    async def manual_tag(
        self,
        *,
        repo_id: str,
        tag_name: str,
        confidence: float = 1.0,
        agent_id: str | None = None,
    ) -> EcosystemRepoTag | None:
        """Manual tagging — source=MANUAL, confidence defaults to 1.0."""
        cache = await self._load_tag_dict()
        tag = cache.get(tag_name)
        if tag is None:
            return None
        rt = EcosystemRepoTag(
            project_id=self._project_id or None,
            repo_id=repo_id,
            tag_id=tag.id,
            confidence=max(0.0, min(1.0, confidence)),
            source=EcosystemTagSource.MANUAL,
            agent_id=agent_id,
        )
        return await self._repo.add_repo_tag(
            rt, project_id=self._project_id or None
        )

    async def remove_tag(self, repo_id: str, tag_name: str) -> bool:
        cache = await self._load_tag_dict()
        tag = cache.get(tag_name)
        if tag is None:
            return False
        return await self._repo.remove_repo_tag(
            repo_id, tag.id, project_id=self._project_id or None
        )


# ============================================================
# Expose constants for test access
# ============================================================

__all__ = [
    "BatchApplyStats",
    "CONFIDENCE_LLM_DEFAULT",
    "CONFIDENCE_RULE",
    "CONFIDENCE_TOPIC",
    "EcosystemTagger",
    "GITHUB_TOPIC_ALIASES",
    "KEYWORD_RULES",
    "LLM_FALLBACK_TAG_THRESHOLD",
    "MAX_LLM_CONCURRENCY",
    "TagApplyResult",
]
