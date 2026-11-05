import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';
import type { Project, Phase, APIResponse, APIListResponse } from '@/types';

export function useProjects() {
  return useQuery({
    queryKey: ['projects'],
    queryFn: () => apiFetch<APIListResponse<Project>>('/api/projects'),
  });
}

export function useProject(id: string) {
  return useQuery({
    queryKey: ['projects', id],
    queryFn: () => apiFetch<APIResponse<Project>>(`/api/projects/${id}`),
    enabled: !!id,
  });
}

export function useProjectPhases(projectId: string) {
  return useQuery({
    queryKey: ['projects', projectId, 'phases'],
    queryFn: () =>
      apiFetch<APIListResponse<Phase>>(`/api/projects/${projectId}/phases`),
    enabled: !!projectId,
  });
}

export function useCreateProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (data: {
      name: string;
      root_path?: string;
      description?: string;
      config?: Record<string, unknown>;
    }) =>
      apiFetch<APIResponse<Project>>('/api/projects', {
        method: 'POST',
        body: JSON.stringify(data),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export function useDeleteProject() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) =>
      apiFetch<APIResponse<null>>(`/api/projects/${id}`, { method: 'DELETE' }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['projects'] });
    },
  });
}

export interface SummaryLeader {
  name: string;
  model: string;
  status: string;
  session_id: string;
  current_task: string;
  last_active_at: string | null;
  /** Persisted within the 15min window (annotated per entry when multiple sessions are shown side by side) */
  live?: boolean;
  /** Main-session context watermark (fleet-layer P2 observability, see docs/fleet-layer-design.md §6).
   *  Null when probing is unavailable (DB fallback path), meaning "unknown" rather than 0. */
  ctx_tokens?: number | null;
  ctx_window?: number | null;
  ctx_pct?: number | null;
  /** Number of running tasks in the teams of agents under this session */
  in_flight_tasks?: number;
}

export interface SummaryWorktree {
  path: string;
  branch: string | null;
  /** short HEAD sha */
  head: string;
  /** Whether there are uncommitted/untracked changes */
  dirty: boolean;
  /** Whether the branch head has been merged into the main branch; null when probing fails / cannot be determined */
  merged: boolean | null;
  locked: boolean;
  locked_reason?: string | null;
}

export interface ProjectSummary {
  status: 'active' | 'inactive';
  active_teams: number;
  pending_tasks: number;
  running_tasks: number;
  /** Deduplicated count of CC sessions seen under this project (agents.session_id footprint) */
  session_count?: number;
  last_activity_at?: string | null;
  /** Latest Leader returned directly by project_id (not via the team chain — a Leader row may live on a cross-project workflow team) */
  leader?: SummaryLeader | null;
  /** All active CC sessions (one CEO-<English name> entry per session when running in parallel); the most recent one when idle */
  leaders?: SummaryLeader[] | null;
  /** Worktrees belonging to this project (scanned on demand, excluding the main checkout itself) */
  worktrees?: SummaryWorktree[] | null;
  top_tasks: { title: string; priority: string }[];
}

export function useProjectSummary(projectId: string) {
  return useQuery({
    queryKey: ['projects', projectId, 'summary'],
    queryFn: () => apiFetch<ProjectSummary>(`/api/projects/${projectId}/summary`),
    enabled: !!projectId,
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}
