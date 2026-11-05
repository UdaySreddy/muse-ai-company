"""HuggingFace Spaces fetcher — multi-source ecosystem profile supplement.

Design philosophy (v1.6.1):
- An HF Space and a GitHub repo are **parallel project-hosting platforms**, not distribution channels
- The same project across sources is **merged into one profile** (a multi-source sources list); no two independent rows
are created
- Fetched via the public HF HTTP API (no token / SDK needed)

Integration flow:
    1. list_spaces(filter_tags=...) → get the candidate list
    2. get_space_detail(id) → get cardData + tags
    3. extract_linked_github(readme) → parse the GitHub URL (README text search)
    4. merge_or_create_profile(repo, space, gh_url):
       - gh_url present and the github profile exists → append the source to the existing one
       - gh_url present but the github profile does not exist → reverse-lookup the gh API to create a github profile +
       dual sources
       - No gh_url → create an independent hf_space profile

API endpoints (public, no auth):
- GET https://huggingface.co/api/spaces?search=...&sort=likes&limit=N
- GET https://huggingface.co/api/spaces/{owner}/{space}
- GET https://huggingface.co/{owner}/{space}/raw/main/README.md
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from aiteam.clock import utc_now

logger = logging.getLogger(__name__)


HF_API_BASE = "https://huggingface.co/api"
HF_HUB_BASE = "https://huggingface.co"


# Regex for links to a GitHub repo (handles https/http, with or without trailing slash / .git)
_GITHUB_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.\-]+)/([A-Za-z0-9_.\-]+?)(?:\.git)?(?:/|$|\s|\")",
    re.IGNORECASE,
)


# Whitelist of Claude/Agent/MCP ecosystem-related HF tags (for preliminary filtering, reducing noise)
DEFAULT_FILTER_TAGS = (
    "agent",
    "agents",
    "mcp",
    "mcp-server",
    "mcp-client",
    "claude",
    "claude-code",
    "anthropic",
    "llm-agent",
    "autonomous",
    "multi-agent",
)


@dataclass
class HfSpaceCandidate:
    """A single HF Space candidate record (lightweight metadata from the list API)."""

    space_id: str  # "owner/space"
    likes: int
    sdk: str | None
    tags: list[str]
    private: bool
    created_at: str | None = None

    @property
    def owner(self) -> str:
        return self.space_id.split("/", 1)[0] if "/" in self.space_id else ""

    @property
    def name(self) -> str:
        return self.space_id.split("/", 1)[1] if "/" in self.space_id else self.space_id


@dataclass
class HfSpaceDetail:
    """The complete HF Space details (including cardData + the linked github URL)."""

    space_id: str
    likes: int
    sdk: str | None
    tags: list[str]
    author: str
    short_description: str
    last_modified: datetime | None
    created_at: datetime | None
    gated: bool
    disabled: bool
    private: bool
    host: str | None  # demo url
    linked_github: str | None = None  # "owner/repo" if found in README
    readme_excerpt: str = ""  # First 400 characters
    raw: dict[str, Any] = field(default_factory=dict)


def _http_get(url: str, timeout: float = 15.0) -> str:
    """GET text (using urllib to avoid extra dependencies)."""
    req = Request(url, headers={"User-Agent": "ai-team-os/v1.6.1 ecosystem-hf-fetcher"})
    with urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_get_json(url: str, timeout: float = 15.0) -> Any:
    return json.loads(_http_get(url, timeout=timeout))


def _parse_iso8601(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # HF returns the "2025-06-10T23:16:21.000Z" format
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def extract_linked_github(readme_text: str) -> str | None:
    """Extract the first github.com/owner/repo link from the README text, returning "owner/repo".

    Skips huggingface's own unrelated links, anchors, and user-only URLs.
    """
    if not readme_text:
        return None
    for m in _GITHUB_URL_RE.finditer(readme_text):
        owner, repo = m.group(1), m.group(2)
        # Filter things obviously not repos (e.g. github.com/sponsors/...)
        if owner.lower() in {"sponsors", "issues", "marketplace", "settings"}:
            continue
        # Filter awesome-list self-reference noise (owner+repo both too short, or non-ASCII anomalies)
        if len(repo) < 2:
            continue
        return f"{owner}/{repo}"
    return None


# ============================================================
# Public API
# ============================================================


def list_spaces_by_search(
    search: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
    sort: str = "likes",
) -> list[HfSpaceCandidate]:
    """Call the HF list API to get the candidate list.

    The HF API does not support OR multi-tag queries in a single call; each tag must be requested separately, then
    merged and deduplicated.
    """
    seen_ids: set[str] = set()
    results: list[HfSpaceCandidate] = []
    queries: list[str] = []
    if search:
        queries.append(f"search={quote(search)}")
    if tags:
        for tag in tags:
            queries.append(f"tags={quote(tag)}")
    if not queries:
        queries.append("")  # all

    for q in queries:
        url = f"{HF_API_BASE}/spaces?{q}&limit={limit}&sort={sort}&direction=-1"
        try:
            data = _http_get_json(url)
        except Exception as e:
            logger.warning("HF list spaces fail q=%s: %s", q, e)
            continue
        if not isinstance(data, list):
            continue
        for entry in data:
            sid = entry.get("id")
            if not sid or sid in seen_ids:
                continue
            seen_ids.add(sid)
            results.append(
                HfSpaceCandidate(
                    space_id=sid,
                    likes=entry.get("likes", 0),
                    sdk=entry.get("sdk"),
                    tags=list(entry.get("tags") or []),
                    private=bool(entry.get("private", False)),
                    created_at=entry.get("createdAt"),
                )
            )
    # Sort by likes descending
    results.sort(key=lambda c: c.likes, reverse=True)
    return results


def get_space_detail(space_id: str, fetch_readme: bool = True) -> HfSpaceDetail | None:
    """Get a single Space's details + optionally parse the github link from the README."""
    try:
        d = _http_get_json(f"{HF_API_BASE}/spaces/{space_id}")
    except Exception as e:
        logger.warning("HF detail fetch fail %s: %s", space_id, e)
        return None

    if not isinstance(d, dict):
        return None

    card = d.get("cardData") or {}
    readme_excerpt = ""
    linked_gh: str | None = None
    if fetch_readme:
        try:
            readme = _http_get(f"{HF_HUB_BASE}/{space_id}/raw/main/README.md", timeout=10.0)
            readme_excerpt = readme[:400]
            linked_gh = extract_linked_github(readme)
        except Exception:
            # The README may not exist; not treated as an error
            pass

    return HfSpaceDetail(
        space_id=d.get("id", space_id),
        likes=d.get("likes", 0),
        sdk=d.get("sdk") or card.get("sdk"),
        tags=list(d.get("tags") or []),
        author=d.get("author", space_id.split("/", 1)[0] if "/" in space_id else ""),
        short_description=card.get("short_description", "") or "",
        last_modified=_parse_iso8601(d.get("lastModified")),
        created_at=_parse_iso8601(d.get("createdAt")),
        gated=bool(d.get("gated", False)),
        disabled=bool(d.get("disabled", False)),
        private=bool(d.get("private", False)),
        host=d.get("host"),
        linked_github=linked_gh,
        readme_excerpt=readme_excerpt,
        raw=d,
    )


def build_hf_source_entry(detail: HfSpaceDetail) -> dict[str, Any]:
    """Build one entry of the sources list (HF Space perspective)."""
    return {
        "kind": "hf_space",
        "id": detail.space_id,
        "likes": detail.likes,
        "url": f"{HF_HUB_BASE}/spaces/{detail.space_id}",
        "demo_url": detail.host,
        "sdk": detail.sdk,
        "last_seen_at": utc_now().isoformat(),
    }


def build_github_source_entry(repo_full_name: str, stars: int = 0) -> dict[str, Any]:
    """Build one entry of the sources list (GitHub perspective, linked from HF)."""
    return {
        "kind": "github",
        "id": repo_full_name,
        "stars": stars,
        "url": f"https://github.com/{repo_full_name}",
        "last_seen_at": utc_now().isoformat(),
    }


# ============================================================
# Dry-run / PoC helper
# ============================================================


def dry_run_scan(
    search: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Exploratory scan: returns candidates + link analysis, without writing to the DB.

    Used by PoC #49 to verify the dedup logic can effectively hit github projects.
    """
    candidates = list_spaces_by_search(search=search, tags=tags, limit=limit)
    details: list[HfSpaceDetail] = []
    for c in candidates[:limit]:
        d = get_space_detail(c.space_id)
        if d:
            details.append(d)

    linked = [d for d in details if d.linked_github]
    isolated = [d for d in details if not d.linked_github]
    return {
        "total_candidates": len(candidates),
        "fetched_detail": len(details),
        "with_github_link": len(linked),
        "isolated_hf_only": len(isolated),
        "linked_pairs": [
            {"hf": d.space_id, "github": d.linked_github, "likes": d.likes, "sdk": d.sdk}
            for d in linked
        ],
        "isolated_samples": [
            {"hf": d.space_id, "likes": d.likes, "sdk": d.sdk, "desc": d.short_description[:80]}
            for d in isolated[:10]
        ],
    }


if __name__ == "__main__":
    # Single-file PoC entry point
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "dry_run":
        result = dry_run_scan(tags=["mcp", "agent"], limit=15)
        print(json.dumps(result, indent=2, ensure_ascii=False))
