import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from './client';

// Aligned with the backend /api/ecosystem/profiles response format
export interface EcosystemRepoProfile {
  id: string;
  repo_full_name: string;
  name: string;
  owner: string;
  description: string | null;
  stars: number;
  language: string | null;
  topics: string[];
  homepage: string | null;
  last_commit_at: string | null;
  needs_deep_review: boolean;
  /** @deprecated v1.6.1: the relevance_category heuristic classification is deprecated; the backend no longer returns it */
  relevance_category?: string | null;
  /** @deprecated v1.6.1: the hardcoded relevance_score has no reference value; the backend no longer returns it */
  relevance_score?: number;
  one_line_summary: string | null;
  last_scanned_at: string;
  first_seen_at: string;
  // Stage B extension fields (populated when the v2 API returns them)
  pushed_at?: string | null;
  is_archived?: boolean;
  scan_run_id?: string | null;
  description_excerpt?: string;
  // v1.5.0-A extension fields (relied on by the frontend stage badge / failed notice / active-set tab)
  shallow_summary?: string;
  last_shallow_refreshed_at?: string | null;
  is_deleted?: boolean;
  is_private_now?: boolean;
  last_fetch_error?: string;
  fetch_failure_count?: number;
  /** @deprecated v1.6.0 P1.A: use last_active_status instead ('active' / 'archived' / 'manual_archived' / 'pinned') */
  is_active?: boolean;
  /** @deprecated v1.6.0 P1.A: the ranking mechanism is retired; use last_active_status + manual_status to express activity */
  active_rank?: number | null;
  // v1.5.1: exposes the progressive-funnel stage state (from the latest deep_review; no review = "queued")
  stage_status?: string | null;
  // v1.5.1: research count (number of associated deep_review rows; 0 = never deep-reviewed)
  research_count?: number;
  // v1.6.0：last_active_status — 'active' / 'archived' / 'manual_archived' / 'pinned' / null
  last_active_status?: string | null;
  // v1.6.1 multi-source: sources list + primary_source
  sources?: { kind: string; id: string; url: string; [k: string]: unknown }[];
  primary_source?: string;
  canonical_id?: string | null;
  source_kind?: string;
}

// v1.5.0 funnel stage states
export const ECOSYSTEM_STAGE_STATUSES = [
  'queued',
  'shallow_done',
  'shallow_failed',
  'architecture_done',
  'architecture_failed',
  'debated',
  'debated_failed',
  'referenced',
  'integrated',
] as const;

export type EcosystemStageStatus = (typeof ECOSYSTEM_STAGE_STATUSES)[number];

/** Stage labels */
export const STAGE_STATUS_LABELS: Record<EcosystemStageStatus, string> = {
  queued: 'Pending Shallow Scan',
  shallow_done: 'Shallow Scan Done',
  shallow_failed: 'Shallow Scan Failed',
  architecture_done: 'Architecture Analyzed',
  architecture_failed: 'Architecture Failed',
  debated: 'Debated',
  debated_failed: 'Debate Failed',
  referenced: '✓ Referenced',
  integrated: '★ Integrated',
};

/** Stage colors (aligned with design §8.1: gray/blue/yellow/orange/green/purple/red) */
export const STAGE_STATUS_TONE: Record<
  EcosystemStageStatus,
  'gray' | 'blue' | 'yellow' | 'orange' | 'green' | 'purple' | 'red'
> = {
  queued: 'gray',
  shallow_done: 'blue',
  shallow_failed: 'red',
  architecture_done: 'yellow',
  architecture_failed: 'red',
  debated: 'orange',
  debated_failed: 'red',
  referenced: 'green',
  integrated: 'purple',
};

/** Stage badge className (Tailwind isomorphic colors) */
export function stageBadgeClass(stage: EcosystemStageStatus | string): string {
  const tone = STAGE_STATUS_TONE[stage as EcosystemStageStatus] ?? 'gray';
  switch (tone) {
    case 'gray':
      return 'bg-muted text-muted-foreground border-border';
    case 'blue':
      return 'bg-blue-500/10 text-blue-700 dark:text-blue-300 border-blue-500/30';
    case 'yellow':
      return 'bg-amber-500/10 text-amber-700 dark:text-amber-300 border-amber-500/30';
    case 'orange':
      return 'bg-orange-500/10 text-orange-700 dark:text-orange-300 border-orange-500/30';
    case 'green':
      return 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300 border-emerald-500/30';
    case 'purple':
      return 'bg-purple-500/10 text-purple-700 dark:text-purple-300 border-purple-500/30';
    case 'red':
      return 'bg-rose-500/10 text-rose-700 dark:text-rose-300 border-rose-500/30';
    default:
      return 'bg-muted text-muted-foreground border-border';
  }
}

export interface EcosystemFacetCounts {
  category: Record<string, number>;
  language: Record<string, number>;
  archived: Record<string, number>; // {"true": n, "false": n}
  // v1.5.1: progressive-funnel stage distribution (full statistics, not truncated by limit)
  // Example: {"queued": 162, "shallow_done": 100, "debated": 3}
  stage?: Record<string, number>;
  // v1.6.0 SST: full statistics over the GitHub topics dimension (replaces the heuristic-based category)
  // Example: {"mcp": 120, "claude-code": 80, "ai": 75, ...}
  topics?: Record<string, number>;
}

export interface EcosystemProfilesResponse {
  profiles: EcosystemRepoProfile[];
  total: number;
  limit?: number;
  offset?: number;
  has_more?: boolean;
  facet_counts?: EcosystemFacetCounts;
}

export interface EcosystemFilters {
  keyword?: string;
  topic?: string;
  /** @deprecated v1.6.0: the relevance_category heuristic classification is retired; the UI uses topics multi-select filtering instead */
  category?: string;
  /** v1.6.0: GitHub topics multi-select filter (client-side filter; intersected with profile.topics) */
  topics?: string[];
  minStars?: number;
  maxStars?: number;
  needsDeepReview?: boolean | null;
  limit?: number;
  offset?: number;
  facetCounts?: boolean;
  // v1.5.0-E addition: active/all/deleted tabs + stage-dimension filtering
  isActive?: boolean | null;
  isDeleted?: boolean | null;
  stageStatus?: string; // Multiple values separated by commas
}

/**
 * List query: search ecosystem repo profiles.
 * Backed by GET /api/ecosystem/profiles
 */
export function useEcosystemProfiles(filters: EcosystemFilters = {}) {
  const {
    keyword = '',
    topic = '',
    category = '',
    minStars = 0,
    maxStars = 0,
    needsDeepReview = null,
    limit = 100,
    offset = 0,
    facetCounts = false,
    isActive = null,
    isDeleted = null,
    stageStatus = '',
  } = filters;

  const params = new URLSearchParams();
  if (keyword) params.set('keyword', keyword);
  if (topic) params.set('topic', topic);
  if (category) params.set('category', category);
  if (minStars > 0) params.set('min_stars', String(minStars));
  if (maxStars > 0) params.set('max_stars', String(maxStars));
  if (needsDeepReview !== null) params.set('needs_deep_review', String(needsDeepReview));
  params.set('limit', String(limit));
  if (offset > 0) params.set('offset', String(offset));
  if (facetCounts) params.set('facet_counts', 'true');
  if (isActive !== null) params.set('is_active', String(isActive));
  if (isDeleted !== null) params.set('is_deleted', String(isDeleted));
  if (stageStatus) params.set('stage_status', stageStatus);

  return useQuery({
    queryKey: ['ecosystem', 'profiles', filters],
    queryFn: () => apiFetch<EcosystemProfilesResponse>(`/api/ecosystem/profiles?${params.toString()}`),
  });
}

/**
 * Single-repo profile hook (basic info) — for scenarios that do not need the full profile.
 * Falls back to lookup via the /profiles list (kept for backward compatibility).
 */
export function useEcosystemRepoDetail(repoId: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'repo', repoId],
    queryFn: async (): Promise<EcosystemRepoProfile | null> => {
      if (!repoId) return null;
      const data = await apiFetch<EcosystemProfilesResponse>(`/api/ecosystem/profiles?limit=100`);
      return data.profiles.find((p) => p.id === repoId) ?? null;
    },
    enabled: !!repoId,
  });
}

// ============================================================
// v2 API: single-repo full detail (BUG-023 fix)
// ============================================================

/** Capability / maturity / risk tags */
export interface EcosystemTag {
  tag_id: string;
  name: string;
  category: string; // capability / maturity / risk / ...
  aliases: string[];
  description: string | null;
  confidence: number; // 0-1
  source: string; // github_topic / auto_rule / llm / manual
  agent_id: string | null;
  created_at: string;
}

/** Deep-review record */
export interface EcosystemDeepReview {
  id: string;
  repo_id: string;
  status: string; // Derived read-only view of stage_status: queued / completed / failed ('running' only on historic rows)
  agent_id: string | null;
  summary_md: string;
  architecture_md: string;
  demo_result: string | null;
  demo_log_excerpt: string | null;
  risks_md: string;
  learnings_md: string;
  integration_recommendation: string | null; // adopt / experiment / hold / avoid
  report_id: string | null;
  created_at: string;
  started_at?: string | null;
  completed_at?: string | null;
  duration_seconds?: number;
  // v1.5.0-A fields (relied on by the frontend timeline)
  stage_status?: EcosystemStageStatus | string;
  integration_md?: string;
  shallow_completed_at?: string | null;
  architecture_completed_at?: string | null;
  debated_at?: string | null;
  stage3_completed_at?: string | null;
  debate_meeting_id?: string | null;
  integration_task_id?: string | null;
}

/** Repo-to-repo relations */
export interface EcosystemRelation {
  id: string;
  source_repo_id: string;
  target_repo_id: string;
  source_repo_full_name?: string;
  target_repo_full_name?: string;
  relation_type: string; // depends_on / inspired_by / fork_of / replaces / ...
  confidence: number;
  evidence: string | null;
  created_at: string;
}

/** Scan run record (aligned with the backend /api/ecosystem/scan-runs response) */
export interface EcosystemScanRun {
  id: string;
  agent_id: string | null;
  /** Raw backend field (incremental / full / topic / trending, etc.) */
  strategy: string;
  /** Frontend-derived: same as strategy (legacy UI field name kept for compatibility) */
  scan_type: string;
  started_at: string;
  completed_at: string | null;
  /** Frontend-derived: completed_at != null -> 'completed', otherwise 'running'; non-empty errors -> 'failed' */
  status: 'completed' | 'running' | 'failed' | string;
  duration_seconds?: number | null;
  repos_added: number;
  repos_updated: number;
  repos_skipped?: number;
  /** v1.6.1 Phase 2: repos with actual metadata changes (topics/stars/desc/lang) */
  metadata_changed_count?: number;
  errors?: string[];
  triggered_by?: string;
  notes: string | null;
}

/** Raw response of the backend /api/ecosystem/scan-runs (fields are mapped in the hook) */
interface ScanRunsApiResponse {
  runs: Array<{
    id: string;
    agent_id: string | null;
    strategy: string;
    started_at: string;
    completed_at: string | null;
    duration_seconds?: number | null;
    repos_added: number;
    repos_updated: number;
    repos_skipped?: number;
    metadata_changed_count?: number;
    errors?: string[];
    triggered_by?: string;
    notes: string | null;
  }>;
  total: number;
}

/** v2 full response — aligned with the backend EcosystemRepoFullResponse */
export interface EcosystemRepoFullResponse {
  profile: EcosystemRepoProfile;
  tags: EcosystemTag[];
  deep_reviews: EcosystemDeepReview[];
  relations_from: EcosystemRelation[];
  relations_to: EcosystemRelation[];
  scan_run: EcosystemScanRun | null;
}

/** Deep-review list response — used to count the real completed/running/failed numbers */
export interface EcosystemDeepReviewListResponse {
  reviews: EcosystemDeepReview[];
  total: number;
}

/**
 * List deep-review records, filterable by status (queued / completed / failed; 'running' only on historic rows).
 * Used by StatsBar to compute the real "deep-reviewed" count (semantics = number of rows with DeepReview.status='completed';
 * after the D5 convergence, status is a derived read-only view of stage_status, and 'completed' is equivalent to the stage having reached
 * shallow_done or above, so the definition corrects itself automatically).
 *
 * Note: the profile.needs_deep_review field means "needs a deep review"; false does not mean "deep review completed".
 * The real DeepReview rows from this endpoint are the source of truth.
 */
/**
 * GET /api/ecosystem/scan-runs?limit=N — the most recent N batch scan records (ecosystem-profile level)
 * Used at the top of EcosystemListPage to show the "recent batch scans" overview.
 * Returned in started_at descending order; the most recent is list[0].
 */
export function useEcosystemRecentScanRuns(limit: number = 5) {
  return useQuery({
    queryKey: ['ecosystem', 'scan-runs', limit],
    queryFn: async (): Promise<{ data: EcosystemScanRun[]; total: number }> => {
      const params = new URLSearchParams();
      params.set('limit', String(limit));
      const body = await apiFetch<ScanRunsApiResponse>(
        `/api/ecosystem/scan-runs?${params.toString()}`,
      );
      // The backend returns {runs, total}, while the frontend hook has historically expected {data, total}.
      // Also derives scan_type (= strategy) and status (inferred from completed_at + errors).
      const data: EcosystemScanRun[] = body.runs.map((r) => {
        const hasErrors = Array.isArray(r.errors) && r.errors.length > 0;
        const status: EcosystemScanRun['status'] = r.completed_at
          ? hasErrors
            ? 'failed'
            : 'completed'
          : 'running';
        return {
          ...r,
          scan_type: r.strategy,
          status,
        };
      });
      return { data, total: body.total };
    },
    staleTime: 30_000,
  });
}

export function useEcosystemDeepReviews(status: string = '') {
  return useQuery({
    queryKey: ['ecosystem', 'deep_reviews', status],
    queryFn: () => {
      const params = new URLSearchParams();
      if (status) params.set('status', status);
      params.set('limit', '100'); // Backend cap is 100; the current data volume (< 100) fits comfortably
      return apiFetch<EcosystemDeepReviewListResponse>(
        `/api/ecosystem/deep_reviews?${params.toString()}`,
      );
    },
    staleTime: 30_000,
  });
}

/**
 * v2: GET /api/ecosystem/profiles/{repo_full_name:path}/full — full detail
 *
 * Accepts a UUID or a repo_full_name. Given a UUID, the corresponding full_name is first looked up in the cached list.
 * Returns null on failure (never throws); the caller degrades to showing basic info.
 */
export function useEcosystemRepoFull(repoIdOrName: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'repo-full', repoIdOrName],
    queryFn: async (): Promise<EcosystemRepoFullResponse | null> => {
      if (!repoIdOrName) return null;

      // Decide whether it is a UUID or owner/name
      let repoFullName = repoIdOrName;
      const looksLikeUuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
        repoIdOrName,
      );
      if (looksLikeUuid) {
        // Backend limit cap is 200; fetch pages until a hit
        const pageSize = 200;
        let offset = 0;
        let hit: EcosystemRepoProfile | undefined;
        // Defensive cap: at most 10 pages (2000 repos) to avoid looping
        for (let page = 0; page < 10; page++) {
          const list = await apiFetch<EcosystemProfilesResponse & { offset?: number }>(
            `/api/ecosystem/profiles?limit=${pageSize}&offset=${offset}`,
          );
          hit = list.profiles.find((p) => p.id === repoIdOrName);
          if (hit) break;
          if (list.profiles.length < pageSize) break; // Last page
          offset += pageSize;
        }
        if (!hit) return null;
        repoFullName = hit.repo_full_name;
      }

      // The path parameter contains a slash: a single-segment encode suffices (the path converter accepts /)
      const encoded = repoFullName
        .split('/')
        .map((seg) => encodeURIComponent(seg))
        .join('/');
      try {
        return await apiFetch<EcosystemRepoFullResponse>(
          `/api/ecosystem/profiles/${encoded}/full`,
        );
      } catch {
        return null;
      }
    },
    enabled: !!repoIdOrName,
    retry: false,
  });
}

/** Review status labels — generic semantics (v1.5.2: wording unified to "In Review" because status does not distinguish shallow/deep stages) */
export const DEEP_REVIEW_STATUS_LABELS: Record<string, string> = {
  pending: 'Pending Review',
  running: 'In Review',
  completed: 'Completed',
  failed: 'Failed',
  skipped: 'Skipped',
};

/** Integration recommendation labels */
export const INTEGRATION_RECOMMENDATION_LABELS: Record<string, string> = {
  adopt: 'Adopt',
  experiment: 'Experiment',
  hold: 'Hold',
  avoid: 'Avoid',
};

/** Relation type labels */
export const RELATION_TYPE_LABELS: Record<string, string> = {
  depends_on: 'Depends On',
  inspired_by: 'Inspired By',
  fork_of: 'Fork Of',
  replaces: 'Replaces',
  similar_to: 'Similar To',
  extends: 'Extends',
  uses: 'Uses',
};

/**
 * Category options — aligned with the backend EcosystemRepoProfile.relevance_category.
 */
export const RELEVANCE_CATEGORIES = [
  'agent-framework',
  'mcp-server',
  'memory-system',
  'skill-system',
  'tooling',
] as const;

export type RelevanceCategory = (typeof RELEVANCE_CATEGORIES)[number];

/**
 * v1.6.0: dynamic topic-badge color palette (shared by StatsBar + RepoCard).
 * Cycles by position idx % length; when the top-N ranking changes, colors follow the position automatically.
 * Uses low saturation (secondary-variant base + soft border/text colors) to stay clean and easy on the eyes.
 */
export const TOPIC_COLOR_PALETTE: readonly string[] = [
  'border-blue-500/30 text-blue-700 dark:text-blue-300',
  'border-emerald-500/30 text-emerald-700 dark:text-emerald-300',
  'border-amber-500/30 text-amber-700 dark:text-amber-300',
  'border-purple-500/30 text-purple-700 dark:text-purple-300',
  'border-rose-500/30 text-rose-700 dark:text-rose-300',
  'border-cyan-500/30 text-cyan-700 dark:text-cyan-300',
  'border-orange-500/30 text-orange-700 dark:text-orange-300',
  'border-pink-500/30 text-pink-700 dark:text-pink-300',
] as const;

/**
 * @deprecated v1.6.0 SST: the relevance_category heuristic classification is retired; the UI uses real GitHub topics.
 * This mapping is kept only for legacy-data display compatibility. New code must not use it.
 */
export const CATEGORY_LABELS: Record<string, string> = {
  'agent-framework': 'Agent Framework',
  'mcp-server': 'MCP Server',
  'memory-system': 'Memory System',
  'skill-system': 'Skill System',
  tooling: 'Developer Tools',
};

// ============================================================
// v1.5.0-E: Project Settings (decision 12.1)
// ============================================================

/** Project-level ecosystem config — aligned with the backend EcosystemProjectSettings */
export interface EcosystemProjectSettings {
  project_id: string;
  min_stars: number;
  top_n: number;
  refresh_interval_days: number;
  auto_shallow_on_archive: boolean;
  focus_topics: string[];
  focus_languages: string[];
  shallow_concurrency: number;
  deep_concurrency: number;
  /** v1.6.1 Phase 2: maximum repos a single scan may add (exceeding it triggers an alert); migrated from scan_profile */
  alert_max_new_per_scan: number;
  created_at: string | null;
  updated_at: string | null;
}

/** Payload when submitting via PUT (without timestamps) */
export type EcosystemProjectSettingsInput = Omit<
  EcosystemProjectSettings,
  'project_id' | 'created_at' | 'updated_at'
>;

/** GET /api/ecosystem/projects/{project_id}/settings */
export function useEcosystemProjectSettings(projectId: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'project-settings', projectId],
    queryFn: () =>
      apiFetch<EcosystemProjectSettings>(
        `/api/ecosystem/projects/${projectId}/settings`,
      ),
    enabled: !!projectId,
  });
}

/** PUT /api/ecosystem/projects/{project_id}/settings */
export function useUpdateProjectSettings(projectId: string | null) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (input: EcosystemProjectSettingsInput) => {
      if (!projectId) throw new Error('projectId required');
      return await apiFetch<EcosystemProjectSettings>(
        `/api/ecosystem/projects/${projectId}/settings`,
        {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(input),
        },
      );
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'project-settings', projectId] });
    },
  });
}

// ============================================================
// v1.5.0-E: Failed repo retry
// ============================================================

/** POST /api/ecosystem/profiles/{repo_id}/retry — immediately retry a failed repo */
export function useRetryFailedRepo() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (repoId: string) =>
      apiFetch<{ success: boolean; repo_full_name: string; next_action: string }>(
        `/api/ecosystem/profiles/${repoId}/retry`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ reason: 'manual_retry' }),
        },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'profiles'] });
      qc.invalidateQueries({ queryKey: ['ecosystem', 'repo-full'] });
    },
  });
}

// ============================================================
// v1.5.0-E: Lifecycle batch dispatch (deep_review_request_batch)
// ============================================================

export interface LifecycleBatchIntent {
  repo_id: string;
  repo_full_name: string;
  deep_review_id: string;
  prompt: string;
  timeout_seconds: number;
  project_id: string | null;
}

export interface LifecycleBatchResponse {
  success: boolean;
  dispatched: number;
  intents: LifecycleBatchIntent[];
}

export interface LifecycleBatchInput {
  tags: string[];
  min_stars?: number | null;
  limit?: number;
  research_goal?: string;
}

export function useLifecycleRequestBatch() {
  return useMutation({
    mutationFn: async (input: LifecycleBatchInput) =>
      apiFetch<LifecycleBatchResponse>(`/api/ecosystem/lifecycle/request_batch`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          tags: input.tags,
          min_stars: input.min_stars ?? null,
          limit: input.limit ?? 20,
          research_goal: input.research_goal ?? '',
        }),
      }),
  });
}

// v1.6.0 event sourcing

export interface RepoEvent {
  id: string;
  event_type: string;
  payload_json: Record<string, unknown>;
  source: string;
  scan_run_id: string | null;
  from_status: string | null;
  to_status: string | null;
  reason: string | null;
  triggered_at: string;
}

export interface RepoEventsResponse {
  success: boolean;
  repo_id: string;
  events: RepoEvent[];
  total: number;
}

/** GET /api/ecosystem/repos/{repoId}/events */
export function useRepoEvents(repoId: string | null, limit = 50) {
  return useQuery({
    queryKey: ['ecosystem', 'repo-events', repoId, limit],
    queryFn: () =>
      apiFetch<RepoEventsResponse>(`/api/ecosystem/repos/${repoId}/events?limit=${limit}`),
    enabled: !!repoId,
    staleTime: 30_000,
  });
}

// ============================================================
// v1.6.0: scan/research history — merged timeline of events + deep_reviews
// ============================================================

/** scan_history entry — merges events and deep_reviews, in reverse chronological order */
export interface ScanHistoryEntry {
  kind: 'event' | 'deep_review';
  /** Event type / 'deep_review_<stage_status>' */
  type: string;
  timestamp: string;
  /** One human-readable sentence */
  summary: string;
  source?: string;
  scan_run_id?: string | null;
  /** Event detail payload */
  payload?: Record<string, unknown>;
  /** deep_review five-section markdown */
  expandable_md?: {
    summary?: string;
    architecture?: string;
    risks?: string;
    learnings?: string;
    integration?: string;
  };
  integration_recommendation?: string | null;
  stage_status?: string;
  review_id?: string;
}

export interface ScanHistoryResponse {
  success: boolean;
  repo_id: string;
  total: number;
  entries: ScanHistoryEntry[];
}

/** GET /api/ecosystem/repos/{repoId}/scan_history */
export function useScanHistory(repoId: string | null, limit = 50) {
  return useQuery({
    queryKey: ['ecosystem', 'scan-history', repoId, limit],
    queryFn: () =>
      apiFetch<ScanHistoryResponse>(`/api/ecosystem/repos/${repoId}/scan_history?limit=${limit}`),
    enabled: !!repoId,
    staleTime: 30_000,
  });
}

// ============================================================
// v1.7.0: shallow-scan batch management
// ============================================================

/** Shallow-scan batch — aligned with the backend EcosystemShallowBatch */
export interface ShallowBatch {
  id: string;
  project_id: string | null;
  triggered_by: string;
  trigger_reason: string | null;
  candidates_count: number;
  status: string; // pending_approval / approved / running / completed / cancelled
  approved_by: string | null;
  approved_at: string | null;
  completed_at: string | null;
  new_repos_count: number;
  updated_repos_count: number;
  metadata_changed_count: number;
  failed_count: number;
  created_at: string;
  updated_at: string;
}

/** Batch candidate repo entry */
export interface ShallowBatchItem {
  repo_id: string;
  repo_full_name: string;
  stars: number;
  stage_status: string;
  shallow_summary_excerpt: string;
  last_commit_at: string | null;
}

export interface ShallowBatchesResponse {
  batches: ShallowBatch[];
  total: number;
}

export interface ShallowBatchResponse {
  batch: ShallowBatch;
}

export interface ShallowBatchItemsResponse {
  batch_id: string;
  items: ShallowBatchItem[];
  total: number;
}

/** GET /api/ecosystem/shallow_batches */
export function useShallowBatches(limit = 50) {
  return useQuery({
    queryKey: ['ecosystem', 'shallow-batches', limit],
    queryFn: () =>
      apiFetch<ShallowBatchesResponse>(`/api/ecosystem/shallow_batches?limit=${limit}`),
    staleTime: 10_000,
  });
}

/** GET /api/ecosystem/shallow_batches/{batchId} */
export function useShallowBatch(batchId: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'shallow-batch', batchId],
    queryFn: () =>
      apiFetch<ShallowBatchResponse>(`/api/ecosystem/shallow_batches/${batchId}`),
    enabled: !!batchId,
    staleTime: 10_000,
  });
}

/** GET /api/ecosystem/shallow_batches/{batchId}/items */
export function useShallowBatchItems(batchId: string | null) {
  return useQuery({
    queryKey: ['ecosystem', 'shallow-batch-items', batchId],
    queryFn: () =>
      apiFetch<ShallowBatchItemsResponse>(`/api/ecosystem/shallow_batches/${batchId}/items`),
    enabled: !!batchId,
    staleTime: 15_000,
  });
}

/** POST /api/ecosystem/shallow_batches */
export function useCreateShallowBatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: { triggered_by: string; trigger_reason?: string }) =>
      apiFetch<{ success: boolean; batch_id: string; candidates_count: number; status: string; message: string }>(
        `/api/ecosystem/shallow_batches`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(input),
        },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batches'] });
    },
  });
}

/** POST /api/ecosystem/shallow_batches/{batchId}/approve */
export function useApproveBatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ batchId, approvedBy }: { batchId: string; approvedBy: string }) =>
      apiFetch<{ success: boolean; batch_id: string; status: string; dr_created: number; message: string }>(
        `/api/ecosystem/shallow_batches/${batchId}/approve`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ approved_by: approvedBy }),
        },
      ),
    onSuccess: (_data, { batchId }) => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batches'] });
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batch', batchId] });
    },
  });
}

/** POST /api/ecosystem/shallow_batches/{batchId}/cancel */
export function useCancelBatch() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (batchId: string) =>
      apiFetch<{ success: boolean; batch_id: string; status: string; message: string }>(
        `/api/ecosystem/shallow_batches/${batchId}/cancel`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
      ),
    onSuccess: (_data, batchId) => {
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batches'] });
      qc.invalidateQueries({ queryKey: ['ecosystem', 'shallow-batch', batchId] });
    },
  });
}
