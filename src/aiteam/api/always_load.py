"""Tool progressive loading P1 — alwaysLoad dynamic rotation core logic (pure Python, no I/O).

During session startup, tag recently high-frequency MCP tools with ``_meta {"anthropic/alwaysLoad": true}`` to exempt
them from
CC's defer. Data fetching (a single SQL query) lives in the repository layer; this module only handles normalization +
hysteresis debounce +
hard-cap adjudication, all as unit-testable pure functions. See the P1 section of docs/tool-loading-design.md for the
design spec.

Complexity ceiling (spec red line): no decay curves / real-time counting / background recomputation / ML scoring.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Client-side full tool-name prefix (agent_activities.tool_name in the database carries this prefix; bare names strip
# it).
TOOL_PREFIX = "mcp__ai-team-os__"

# The alwaysLoad resident context cost is fixed, so the bar is strict: hard cap 5, target 3 (user ruling 2026-07-13).
ALWAYSLOAD_HARD_CAP = 5
ALWAYSLOAD_TARGET = 3

# Hysteresis debounce: a challenger's current-period frequency must be > incumbent × this factor to rotate in; otherwise
# the incumbent stays (prevents boundary flapping).
HYSTERESIS_FACTOR = 1.2

# Cross-day threshold: a selected tool's activity must span ≥ this many days, blocking single-day burst calls.
CROSS_DAY_THRESHOLD = 2

# Rotation audit event type; each computation records one row, and that row is also the next period's hysteresis
# baseline (state and audit combined).
ROTATION_EVENT_TYPE = "tool.alwaysload.rotation"


@dataclass(frozen=True)
class Candidate:
    """One qualified candidate: bare tool name + current-period frequency + cross-day count."""

    name: str
    count: int
    days: int


@dataclass
class RotationResult:
    """Rotation adjudication result."""

    tools: list[Candidate] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [c.name for c in self.tools]


def normalize_tool_name(full_name: str) -> str:
    """Strip the ``mcp__ai-team-os__`` prefix to get the bare tool name; return as-is when there is no prefix."""
    if full_name.startswith(TOOL_PREFIX):
        return full_name[len(TOOL_PREFIX):]
    return full_name


def build_candidates(
    rows: list[tuple[str, int, int]],
    registered: set[str] | None,
) -> list[Candidate]:
    """Normalize SQL rows into bare-name candidates and filter by the currently registered tool names (preventing
    deleted tools from being selected).

    Args:
        rows: ``[(full_name, count, days), ...]`` returned by the repository, already in descending frequency order.
        registered: the set of bare tool names actually registered by the current MCP server; ``None`` means no
        registration filtering
            (e.g. when a manual curl does not pass the registered parameter).

    Returns:
        Candidate list in descending frequency order. If the same bare name appears multiple times (theoretically
        impossible), take the first (the highest frequency).
    """
    seen: set[str] = set()
    out: list[Candidate] = []
    for full_name, count, days in rows:
        bare = normalize_tool_name(full_name)
        if bare in seen:
            continue
        if registered is not None and bare not in registered:
            continue
        seen.add(bare)
        out.append(Candidate(name=bare, count=int(count), days=int(days)))
    # The SQL results are already in descending frequency order; stable-sort once more here in case the caller passes
    # them out of order.
    out.sort(key=lambda c: c.count, reverse=True)
    return out


def compute_rotation(
    candidates: list[Candidate],
    incumbents: list[str],
    *,
    target: int = ALWAYSLOAD_TARGET,
    hard_cap: int = ALWAYSLOAD_HARD_CAP,
    hysteresis_factor: float = HYSTERESIS_FACTOR,
) -> RotationResult:
    """Hysteresis rotation adjudication (pure Python).

    Rules:
    - Only candidates satisfying the cross-day threshold (``candidates`` are already filtered upstream) qualify; if a
      previous-period incumbent
      is not among the candidates (it fell below the threshold or was deleted), it is automatically out.
    - ``target`` slots (capped by ``hard_cap``). Empty slots are filled directly by the strongest challengers, with no
      hysteresis.
    - When every slot is filled by incumbents, a challenger needs a current-period frequency > weakest incumbent ×
      ``hysteresis_factor``
      to displace one; otherwise the incumbents stay (prevents boundary flapping). Challengers decrease in frequency;
      once the strongest challenger cannot displace, stop.
    - When data is insufficient (qualified < target), do not pad the numbers; return the few qualified ones or an empty
      list.

    Args:
        candidates: qualified candidates in descending frequency order.
        incumbents: the previous period's list (bare names), taken from the most recent rotation audit event.

    Returns:
        RotationResult: the list + rotated-in / rotated-out.
    """
    slots = min(target, hard_cap)
    if slots <= 0 or not candidates:
        return RotationResult(tools=[], added=[], removed=list(incumbents))

    incumbent_set = set(incumbents)
    # Incumbents in descending frequency order within the candidates (only those still qualified appear here).
    retained = [c for c in candidates if c.name in incumbent_set]
    # Challengers in descending frequency order.
    challengers = [c for c in candidates if c.name not in incumbent_set]

    selected: list[Candidate] = retained[:slots]
    # Empty slots are filled directly by the strongest challengers (no displacement involved, no hysteresis needed).
    ci = 0
    while len(selected) < slots and ci < len(challengers):
        selected.append(challengers[ci])
        ci += 1

    # Hysteresis displacement: triggered only when unused challengers remain and all slots are full.
    # Each round takes the strongest unused challenger vs the current weakest incumbent; if it can displace, swap; if
    # not, stop (subsequent challengers are only weaker).
    while ci < len(challengers) and len(selected) >= slots:
        challenger = challengers[ci]
        incumbents_in = [c for c in selected if c.name in incumbent_set]
        if not incumbents_in:
            break
        weakest = min(incumbents_in, key=lambda c: c.count)
        if challenger.count > weakest.count * hysteresis_factor:
            selected.remove(weakest)
            selected.append(challenger)
            ci += 1
        else:
            break

    # The output is presented stably in descending frequency order.
    selected.sort(key=lambda c: c.count, reverse=True)
    final_names = {c.name for c in selected}
    added = [c.name for c in selected if c.name not in incumbent_set]
    removed = [n for n in incumbents if n not in final_names]
    return RotationResult(tools=selected, added=added, removed=removed)


def parse_registered_param(registered: str) -> set[str] | None:
    """Parse the comma-separated registered query parameter into a bare-name set; an empty string → ``None`` (no
    filtering)."""
    if not registered:
        return None
    names = {n.strip() for n in registered.split(",") if n.strip()}
    return names or None
