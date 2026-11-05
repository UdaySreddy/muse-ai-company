import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { APIListResponse, APIResponse } from '@/types';

// ─────────────────────────────────────────────────────────────────────────────
// Types: aligned with the table structures in docs/workflow-observability-design.md §2.1 / §2.2.
// The new observability tables workflow_runs / workflow_agents are a "rebuildable cache of immutable files";
// the frontend only displays them read-only and performs no write projection.
// ─────────────────────────────────────────────────────────────────────────────

// killed/failed are real terminal states (about 10% of runs on this machine end this way);
// the backend status is a free-form string — the (string & {}) fallback keeps unknown new states from breaking the types.
export type WorkflowStatus =
  | 'planned'
  | 'running'
  | 'completed'
  | 'interrupted'
  | 'killed'
  | 'failed'
  | (string & {});
export type WorkflowAgentState = 'queued' | 'running' | 'done' | (string & {});

export interface WorkflowPhase {
  index: number;
  title: string;
}

export interface WorkflowRun {
  id: string;
  wf_id: string;
  project_id?: string | null;
  team_id?: string | null;
  session_id?: string | null;
  cc_task_id?: string | null;
  name: string;
  status: WorkflowStatus;
  /** Data-plane provenance: hook (skeleton only) / file (file reconcile) / hook+file */
  source: string;
  phases: WorkflowPhase[];
  planned_agent_count: number;
  dynamic_nodes: number;
  agent_count: number;
  total_tokens: number;
  total_tool_calls: number;
  duration_ms?: number | null;
  summary?: string | null;
  result?: unknown;
  script_path?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  created_at: string;
  updated_at: string;
  // ── Phase 2 live observability columns (runtime approximations; terminal values are overwritten by the wf_<id>.json file values, design §10.1)──
  /** Runtime token estimate = Σ agent lastCtx (cached agents count 0); the terminal UI switches back to total_tokens */
  live_tokens?: number | null;
  /** max(mtime of the journal and all agent-*.jsonl); basis for the interrupted judgement + the UI "last activity" */
  last_activity_at?: string | null;
  /** Consumed-byte watermark of journal.jsonl (used by the server-side tail; the UI only passes it through, never displays it) */
  journal_offset?: number | null;
  /** mtime_ns:size fingerprint of wf_<id>.json (cheap reconcile skip; the UI only passes it through, never displays it) */
  source_fingerprint?: string | null;
}

export interface WorkflowAgent {
  id: string;
  run_id: string;
  wf_id: string;
  project_id?: string | null;
  cc_agent_id: string;
  os_agent_id?: string | null;
  label: string;
  phase_index?: number | null;
  phase_title?: string | null;
  model?: string | null;
  state: WorkflowAgentState;
  tokens: number;
  tool_calls: number;
  duration_ms?: number | null;
  last_tool_name?: string | null;
  last_tool_summary?: string | null;
  prompt_preview?: string | null;
  result_preview?: string | null;
  started_at?: string | null;
  queued_at?: string | null;
  created_at: string;
  updated_at: string;
  /** Phase 2: mtime of this agent's jsonl; right-end anchor of the swim-lane running bar */
  last_activity_at?: string | null;
}

export interface WorkflowFilters {
  status?: string;
  project_id?: string;
  limit?: number;
}

export interface ReconcileResult {
  ingested: number;
  updated: number;
}

// Supports both endpoint contracts: APIListResponse<T> ({data,total,message}, aligned with events/teams)
// or a bare array T[] (the apiFetch<WorkflowRun[]> shorthand in frontendPlan). The API is still being developed in parallel;
// both shapes are normalized to an array so the real call path works as soon as it lands.
function toList<T>(res: APIListResponse<T> | T[] | null | undefined): T[] {
  if (!res) return [];
  if (Array.isArray(res)) return res;
  return res.data ?? [];
}

// The detail endpoint contract is a bare WorkflowRun (design §4), but an APIResponse-wrapped shape is also accepted.
function unwrapRun(res: APIResponse<WorkflowRun> | WorkflowRun): WorkflowRun {
  if (res && typeof res === 'object' && 'data' in res && !('wf_id' in res)) {
    return (res as APIResponse<WorkflowRun>).data;
  }
  return res as WorkflowRun;
}

/**
 * Run list. Lists containing running runs use refetchInterval ≈15s (aligned with decisions.ts:26),
 * so a lost completed event is covered by polling + file reconcile; when everything is terminal, polling stops to save traffic.
 */
export function useWorkflows(filters?: WorkflowFilters) {
  const params = new URLSearchParams();
  if (filters?.status) params.set('status', filters.status);
  if (filters?.project_id) params.set('project_id', filters.project_id);
  if (filters?.limit) params.set('limit', String(filters.limit));
  const qs = params.toString();
  const url = `/api/workflows${qs ? `?${qs}` : ''}`;

  return useQuery({
    queryKey: ['workflows', filters ?? {}],
    queryFn: async (): Promise<WorkflowRun[]> => {
      try {
        const res = await apiFetch<APIListResponse<WorkflowRun> | WorkflowRun[]>(url);
        return toList(res);
      } catch (err) {
        // Fall back to an empty list to avoid a hard crash, but always leave a trace — silently swallowing a 422 (limit above the backend cap) once made
        // the project page's summary strip disappear entirely with no way to notice (recorded 2026-07-08).
        console.warn('[workflows] list fetch failed, rendering empty:', err);
        return [];
      }
    },
    // With nothing running, keep a slow 60s polling fallback: new runs depend on WS invalidation, and a disconnect once caused "never updates"
    refetchInterval: (query) =>
      (query.state.data ?? []).some((r) => r.status === 'running') ? 15000 : 60000,
  });
}

/** Run detail. The running state polls as a completion-detection fallback (file reconcile happens server-side). */
export function useWorkflow(wfId: string) {
  return useQuery({
    queryKey: ['workflows', wfId],
    enabled: !!wfId,
    queryFn: async (): Promise<WorkflowRun> => {
      const res = await apiFetch<APIResponse<WorkflowRun> | WorkflowRun>(
        `/api/workflows/${encodeURIComponent(wfId)}`,
      );
      return unwrapRun(res);
    },
    refetchInterval: (query) => (query.state.data?.status === 'running' ? 15000 : false),
  });
}

/**
 * Per-agent telemetry (for the detail table / phase swim lane).
 * When live=true (run is running/interrupted), poll every 15s, aligned with the existing cadence of useWorkflows/useWorkflow;
 * terminal states stop polling to save traffic. No local per-second ticker (design §10.8).
 */
export function useWorkflowAgents(wfId: string, live = false) {
  return useQuery({
    queryKey: ['workflows', wfId, 'agents'],
    enabled: !!wfId,
    queryFn: async (): Promise<WorkflowAgent[]> => {
      try {
        const res = await apiFetch<APIListResponse<WorkflowAgent> | WorkflowAgent[]>(
          `/api/workflows/${encodeURIComponent(wfId)}/agents`,
        );
        return toList(res);
      } catch {
        return [];
      }
    },
    refetchInterval: live ? 15000 : false,
  });
}

/** Manual reconcile: pull in the rich data of runs that completed while the OS was offline (POST /reconcile). */
export function useReconcileWorkflows() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body?: { session_id?: string; project_dir?: string }) =>
      apiFetch<ReconcileResult>('/api/workflows/reconcile', {
        method: 'POST',
        body: JSON.stringify(body ?? {}),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['workflows'] });
    },
  });
}
