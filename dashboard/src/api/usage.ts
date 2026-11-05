import { useQueries, useQuery } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIResponse } from '@/types';

// ─────────────────────────────────────────────────────────────────────────────
// Token usage attribution (docs/token-attribution-v1-design.md §5) — read-only consumption layer.
//
// The types here deliberately mirror the backend aiteam.types structure verbatim, including the **no total field** rule:
// In practice 95.6% of the four layers is cache_read, so any "total" is a synonym for "cache reads"; comparisons across models / across
// dispatch paths would be systematically skewed. Anyone who wants a total must add it themselves and own the explanation.
//
// Likewise, a usage number always lives and dies with its denominator (dispatches_total) and its metric:
// a token count detached from those two is meaningless — this repo holds two orthogonal metrics that differ 5-25x in practice.
// ─────────────────────────────────────────────────────────────────────────────

/** Metric label. The three never add together: usage_sum is consumption; the other two are instantaneous level snapshots. */
export type MetricLabel = 'usage_sum' | 'ctx_last' | 'ctx_watermark' | '';

/** Attribution level. task is a side branch, not a fifth level — its confidence is an order of magnitude below the first four. */
export type ScopeLevel = 'project' | 'session' | 'workflow_run' | 'agent' | 'task';

/** Dispatch path. Leader sessions and sub-agents differ by two orders of magnitude; they are shown separately and not merged by default. */
export type DispatchPath = 'subagent' | 'leader_session' | 'workflow_self_report' | 'tool_call';

/** How this attribution number was obtained. alias_fallback = some rows went through the alias fallback, so the whole answer is labelled degraded. */
export type AttributionMethod = 'transcript_parse' | 'self_report' | 'alias_fallback';

/** Unattributed reason code. The first three are dispatch-level and drillable; by_design is tool-call-level and appears only as a one-line label. */
export type UnattributedCode =
  | 'no_transcript_path'
  | 'transcript_gone'
  | 'not_yet_measured'
  | 'by_design'
  | 'self_report_absent'
  | 'multi_task_unsplittable';

/**
 * The complete answer to one attribution query. Four layers split, no total field; numerator, denominator and metric at the same level.
 * The name deliberately avoids a "Token" prefix: identifiers carrying the token stem must be declared in
 * scripts/usage_surface.py with a dimension, and a type name is not a value of any dimension.
 */
export interface AttributionPayload {
  scope: ScopeLevel;
  scope_id: string;
  metric: MetricLabel;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  /** Numerator: dispatches in this scope with measured usage */
  dispatches_attributed: number;
  /** Denominator: total dispatches in this scope (including rows with no data) */
  dispatches_total: number;
  /** Unattributed dispatches counted by reason code, in the same unit as the denominator */
  unattributed_reasons: Record<string, number>;
  measured_window: [string, string] | null;
  method: AttributionMethod;
}

/** One row of the coverage matrix. Deliberately zero token-value fields — this table answers "what proportion was measured". */
export interface CoverageRow {
  path: DispatchPath;
  metric: MetricLabel;
  /** null = the question "how much was measured" does not apply on this path (not collected by design); it is neither 0 nor blank */
  dispatches_total: number | null;
  dispatches_attributed: number | null;
  unattributed_reasons: Record<string, number>;
  note: string;
}

/** The resolvability rate of one hop in the attribution chain. End-to-end coverage is the product of the hops; a single scalar hides the real bottleneck. */
export interface EdgeCoverage {
  edge: string;
  resolvable: number;
  required: number;
  note: string;
}

export interface CoverageReport {
  rows: CoverageRow[];
  hops: EdgeCoverage[];
  window: [string, string] | null;
  generated_at: string;
}

/** Drill-down candidate. Dispatch counts only, no usage at all — usage must be queried per scope to be obtained. */
export interface ScopeCandidate {
  scope: ScopeLevel;
  scope_id: string;
  label: string;
  dispatches_total: number;
  dispatches_attributed: number;
}

export interface UnattributedSample {
  agent_id: string;
  name: string;
  project_id: string;
  transcript_path: string;
  created_at: string | null;
}

export interface UnattributedSamples {
  /** How many rows were actually scanned — samples are samples, not the denominator; counts come from the coverage matrix's full scan */
  scanned: number;
  scan_limit: number;
  samples: Record<string, UnattributedSample[]>;
}

/** Payload of the single-measurement card. Parsed live from one transcript; not a ledger value, and no total field. */
export interface SingleProbePayload {
  agent_id: string;
  agent_name: string;
  metric: MetricLabel;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  api_calls: number;
  model: string;
  model_source: string;
  transcript_path: string;
  prompt_excerpt: string;
  result_excerpt: string;
  duration_ms: number | null;
  started_at: string | null;
  ended_at: string | null;
}

// ─────────────────────────────────────────────────────────────────────────────
// hooks
// ─────────────────────────────────────────────────────────────────────────────

export function useUsageCoverage(days: number) {
  return useQuery({
    queryKey: ['usage', 'coverage', days],
    queryFn: async () => {
      const res = await apiFetch<APIResponse<CoverageReport>>(`/api/usage/coverage?days=${days}`);
      return res.data;
    },
  });
}

function attributionUrl(scope: ScopeLevel, scopeId: string, path: DispatchPath, days: number): string {
  const q = new URLSearchParams({
    metric: 'usage_sum',
    scope,
    scope_id: scopeId,
    population: path,
    days: String(days),
  });
  return `/api/usage/attribution?${q}`;
}

export function useAttribution(
  scope: ScopeLevel,
  scopeId: string,
  path: DispatchPath,
  days: number,
  enabled = true,
) {
  return useQuery({
    queryKey: ['usage', 'attribution', scope, scopeId, path, days],
    queryFn: async () => {
      const res = await apiFetch<APIResponse<AttributionPayload>>(
        attributionUrl(scope, scopeId, path, days),
      );
      return res.data;
    },
    enabled,
  });
}

/**
 * Fetch the attribution results for a group of same-level scopes in one go, for "one card per level, sorted by output_tokens".
 * Query one by one rather than having the backend return a batch: every card must come back carrying its own denominator and unattributed breakdown;
 * a batch endpoint easily degenerates into a table of bare numbers.
 */
export function useAttributionBatch(
  scope: ScopeLevel,
  scopeIds: string[],
  path: DispatchPath,
  days: number,
) {
  return useQueries({
    queries: scopeIds.map((id) => ({
      queryKey: ['usage', 'attribution', scope, id, path, days],
      queryFn: async () => {
        const res = await apiFetch<APIResponse<AttributionPayload>>(
          attributionUrl(scope, id, path, days),
        );
        return res.data;
      },
    })),
  });
}

export function useScopeCandidates(
  scope: ScopeLevel,
  parentScope: ScopeLevel | '',
  parentId: string,
  path: DispatchPath,
  days: number,
  limit: number,
) {
  return useQuery({
    queryKey: ['usage', 'scopes', scope, parentScope, parentId, path, days, limit],
    queryFn: async () => {
      const q = new URLSearchParams({
        scope,
        parent_scope: parentScope,
        parent_id: parentId,
        population: path,
        days: String(days),
        limit: String(limit),
      });
      const res = await apiFetch<APIResponse<ScopeCandidate[]>>(`/api/usage/scopes?${q}`);
      return res.data;
    },
  });
}

export function useUnattributedSamples(path: DispatchPath, days: number) {
  return useQuery({
    queryKey: ['usage', 'unattributed', path, days],
    queryFn: async () => {
      const q = new URLSearchParams({ population: path, days: String(days), per_reason: '5' });
      const res = await apiFetch<APIResponse<UnattributedSamples>>(`/api/usage/unattributed?${q}`);
      return res.data;
    },
  });
}

/** Single measurement. Only requested after the user explicitly picks one dispatch — it reads one transcript live. */
export function useSingleProbe(agentId: string) {
  return useQuery({
    queryKey: ['usage', 'probe', agentId],
    queryFn: async () => {
      const res = await apiFetch<APIResponse<SingleProbePayload>>(
        `/api/usage/probe?agent_id=${encodeURIComponent(agentId)}`,
      );
      return res.data;
    },
    enabled: Boolean(agentId),
    retry: false,
    staleTime: Infinity,
  });
}
