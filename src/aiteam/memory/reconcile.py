"""AI Team OS — Memory reconcile coarse screening (memory system v2 P2).

Step one of design §4, "coarse screening (zero LLM)": cluster valid task_memos by scope_path/task,
compute pairwise similarity within each cluster with the built-in BM25, and pair those above the threshold into
candidate groups for the in-CC-session agent calling the tool
to make the LLM fine judgment (KEEP/MERGE/INVALIDATE/NOOP).

Core architecture constraint — the OS has no independent LLM credentials: this module performs only deterministic
candidate coarse screening, and the judgment is completed by the agent
(following ecosystem apply_shallow_summary's "the agent computes, the tool stores" pattern). Pure Python,
reusing the retriever's BM25, with no third-party dependencies.
"""

from __future__ import annotations

from typing import Any

from aiteam.memory.retriever import _bm25_scores, _tokenize_bm25
from aiteam.types import TaskMemo

# Pairwise similarity threshold: sim = symmetrically normalized BM25 (0..1); ≥ the threshold pairs into a candidate
# edge.
DEFAULT_SIM_THRESHOLD = 0.45
# Promotion (distillation) candidate bar: when the member count or cross-task count reaches the bar, it is marked as
# direction-layer promotion material.
_PROMOTION_MIN_MEMBERS = 3
_PROMOTION_MIN_TASKS = 2


def _cluster_key(memo: TaskMemo) -> str:
    """Cluster key: scope_path first (item 2 path scope), falling back to the same task when empty.

    Memos with a non-empty scope_path cluster across tasks by path scope; those without a scope_path cluster only within
    the same
    task, avoiding stuffing the whole project's unlabeled memos into one giant cluster.
    """
    sp = (memo.scope_path or "").strip()
    return f"scope:{sp}" if sp else f"task:{memo.task_id}"


def _pairwise_edges(
    tokenized: list[list[str]], threshold: float
) -> list[tuple[int, int, float]]:
    """Pairwise BM25 similarity within a cluster, returning the above-threshold symmetric edges (i, j, sim).

    BM25 itself is asymmetric (query vs doc). Score the whole cluster with each memo as the query,
    normalize by self-score to get sim(i→j)=score[j]/score[i], then take the two-direction min for a conservative
    symmetrization.
    """
    n = len(tokenized)
    if n < 2:
        return []
    # Score with each entry as the query: rows[i][j] = j's raw BM25 score when i is the query
    rows: list[list[float]] = []
    for i in range(n):
        q = tokenized[i]
        rows.append(_bm25_scores(tokenized, q) if q else [0.0] * n)

    edges: list[tuple[int, int, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            self_i = rows[i][i]
            self_j = rows[j][j]
            sim_ij = rows[i][j] / self_i if self_i > 0 else 0.0
            sim_ji = rows[j][i] / self_j if self_j > 0 else 0.0
            sim = min(sim_ij, sim_ji)  # conservative: only similar when both directions are similar
            if sim >= threshold:
                edges.append((i, j, sim))
    return edges


def _connected_components(n: int, edges: list[tuple[int, int, float]]) -> list[list[int]]:
    """Union-find: merge candidate edges into connected components (candidate groups), keeping only groups of size ≥
    2."""
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, j, _sim in edges:
        parent[find(i)] = find(j)

    groups: dict[int, list[int]] = {}
    for idx in range(n):
        groups.setdefault(find(idx), []).append(idx)
    return [members for members in groups.values() if len(members) >= 2]


def _memo_brief(memo: TaskMemo) -> dict[str, Any]:
    """The external view of a single memo within a candidate group (for the agent's fine judgment, including the full
    text)."""
    return {
        "id": memo.id,
        "task_id": memo.task_id,
        "memo_type": memo.memo_type,
        "content": memo.content,
        "scope_path": memo.scope_path or "",
        "quality_score": memo.quality_score,
        "created_at": memo.created_at.isoformat() if memo.created_at else None,
    }


def build_candidate_groups(
    memos: list[TaskMemo],
    threshold: float = DEFAULT_SIM_THRESHOLD,
) -> list[dict[str, Any]]:
    """Episodic-layer candidate coarse screening: cluster → in-cluster BM25 pairwise pairing → connected components form
    candidate groups.

    Args:
        memos: valid task_memos (the caller has already filtered invalid_at IS NULL).
        threshold: the symmetrically normalized BM25 similarity threshold.

    Returns:
        The candidate group list; each group contains cluster_key/scope_path/member full texts/average similarity/
        cross-task count/promotion_candidate flag. Empty clusters or no pairings produce nothing.
    """
    # 1) Cluster by cluster_key
    clusters: dict[str, list[TaskMemo]] = {}
    for m in memos:
        clusters.setdefault(_cluster_key(m), []).append(m)

    groups: list[dict[str, Any]] = []
    for key, members in clusters.items():
        if len(members) < 2:
            continue
        tokenized = [_tokenize_bm25(m.content) for m in members]
        edges = _pairwise_edges(tokenized, threshold)
        if not edges:
            continue
        # Each pair's similarity is used for the in-group average-similarity display
        sim_lookup = {(i, j): s for i, j, s in edges}
        for comp in _connected_components(len(members), edges):
            comp_set = set(comp)
            comp_sims = [
                s for (i, j), s in sim_lookup.items() if i in comp_set and j in comp_set
            ]
            comp_members = [members[idx] for idx in comp]
            distinct_tasks = {m.task_id for m in comp_members}
            promotion = (
                len(comp_members) >= _PROMOTION_MIN_MEMBERS
                or len(distinct_tasks) >= _PROMOTION_MIN_TASKS
            )
            groups.append(
                {
                    "cluster_key": key,
                    "scope_path": comp_members[0].scope_path or "",
                    "member_count": len(comp_members),
                    "distinct_tasks": len(distinct_tasks),
                    "avg_similarity": round(
                        sum(comp_sims) / len(comp_sims), 4
                    )
                    if comp_sims
                    else 0.0,
                    "promotion_candidate": promotion,
                    "members": [_memo_brief(m) for m in comp_members],
                }
            )

    # High-similarity + cross-task groups sort first, so the agent handles them with priority
    groups.sort(
        key=lambda g: (g["promotion_candidate"], g["avg_similarity"]), reverse=True
    )
    return groups


# Operation-guide constant (attached to the response, telling the calling agent the four operations' semantics + the
# three principles).
OPERATION_GUIDE: dict[str, Any] = {
    "operations": {
        "KEEP": "Keep both (no redundancy / each carries information) — no operation needs to be submitted in apply.",
        "MERGE": "Merge: submit {op:'merge', content: the new merged content, memo_ids:[the entries being merged]}; "
        "the tool creates a new memo and sets each merged entry invalid + invalidated_by pointing at the new entry "
        "(Zep invalidation semantics).",
        "INVALIDATE": "Contradicted/overturned: submit {op:'invalidate', memo_ids:[...]} to invalidate it (no "
                      "deletion).",
        "NOOP": "Leave this group untouched — no operation needs to be submitted.",
    },
    "reconcile_principles": [
        "Keep only entries useful to almost every future task (invalidate low-value ones during organization).",
        "Point to the authoritative file/tool rather than restating its content (over-long content is degraded to a "
        "pointer entry).",
        "Prefer concise rewrites over appending (MERGE into a shorter, more precise new entry; do not stack).",
    ],
    "distill_and_score": {
        "promote": "Conclusions/user corrections recurring across memos → promote into a direction-layer entry: "
        "{op:'promote', content, kind:constraint/design/directive/preference, "
        "source_refs:[source memo ids]} (the red lines apply as usual: a single entry ≤400 characters, and it must fit "
        "the bucket character"
        "quotas global 1200 / project 1500 / user 300 — the storage cap is the injection budget).",
        "score": "Add a quality score to a summary/decision-type memo: "
        "{op:'score', memo_id, quality_score:1-10, reason}.",
    },
    "ultracode_hint": "When the candidate-group count is large, open ultracode and use a Workflow to fine-judge the "
                      "groups concurrently, then apply uniformly after collecting the results.",
}
