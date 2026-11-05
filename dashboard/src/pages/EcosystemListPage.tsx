import { useState, useMemo, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { AlertCircle, Boxes, ChevronLeft, ChevronRight, Search as SearchIcon, Layers } from 'lucide-react';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { useEcosystemProfiles } from '@/api/ecosystem';
import type { EcosystemFilters } from '@/api/ecosystem';
import { RepoCard } from '@/components/ecosystem/RepoCard';
import { FilterBar } from '@/components/ecosystem/FilterBar';
import { EcosystemStatsBar } from '@/components/ecosystem/EcosystemStatsBar';
import { RecentScanRunsBar } from '@/components/ecosystem/RecentScanRunsBar';
import { EcosystemProjectFilter } from '@/components/ecosystem/EcosystemProjectFilter';
import { useProject } from '@/context/ProjectContext';

/**
 * Ecosystem list page — v1.6.0: removed the inactivity filter; all repos permanently participate in search.
 * The project filter is the "Ecosystem Project" dropdown in this page's header (EcosystemProjectFilter, isolating ecosystem libraries per project).
 * ⚠️ This is an ecosystem-only filter; never move it into the global Header (see EcosystemProjectFilter.tsx for the historic lesson).
 * "Show deleted" is controlled by the checkbox inside FilterBar (hidden by default).
 *
 * Route: /ecosystem
 * Data source: GET /api/ecosystem/profiles?facet_counts=true&is_deleted=...
 */
const PAGE_SIZE = 100;

export function EcosystemListPage() {
  const { projectId, projectName } = useProject();
  const [page, setPage] = useState(1);
  const [filters, setFilters] = useState<EcosystemFilters>({
    limit: PAGE_SIZE,
    facetCounts: true,
  });

  // Reset to page 1 when filters change (prevents page=3 with only 50 blank rows left)
  // Pagination reset driven by external input (filters); a legitimate effect-setState — a rule false positive, explicitly exempted by name
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setPage(1);
  }, [filters]);

  // v1.6.0 design philosophy: all repos permanently participate in search; deleted/inactive repos are no longer excluded by default.
  // When FilterBar's "Show deleted" is checked there is no change (already included); when unchecked, an explicit isDeleted=false shows active repos only.
  // - filters.isDeleted=null -> showDeleted checked -> no parameter sent (full set, 265)
  // - filters.isDeleted=undefined (default) -> no parameter sent (full set, 265; consistent with the v1.6.0 philosophy)
  // - filters.isDeleted=false -> not-deleted only
  const effectiveFilters = useMemo<EcosystemFilters>(() => {
    return { ...filters, offset: (page - 1) * PAGE_SIZE };
  }, [filters, page]);

  const { data, isLoading, error } = useEcosystemProfiles(effectiveFilters);
  const profiles = useMemo(() => data?.profiles ?? [], [data]);

  // Client-side secondary filtering: keyword matches owner/description; v1.6.0 topics multi-select intersects with OR semantics
  const filtered = useMemo(() => {
    const q = filters.keyword?.toLowerCase() ?? '';
    const selectedTopics = filters.topics ?? [];
    if (!q && selectedTopics.length === 0) return profiles;
    return profiles.filter((p) => {
      if (q) {
        const matchKw =
          p.repo_full_name.toLowerCase().includes(q) ||
          (p.owner ?? '').toLowerCase().includes(q) ||
          p.name.toLowerCase().includes(q) ||
          (p.description ?? p.description_excerpt ?? '').toLowerCase().includes(q) ||
          (p.one_line_summary ?? '').toLowerCase().includes(q);
        if (!matchKw) return false;
      }
      if (selectedTopics.length > 0) {
        const repoTopics = p.topics ?? [];
        // OR semantics: a profile passes when its topics intersect the selection
        const hit = selectedTopics.some((t) => repoTopics.includes(t));
        if (!hit) return false;
      }
      return true;
    });
  }, [profiles, filters.keyword, filters.topics]);

  // v1.6.0: global topic ranking map (shared by StatsBar + RepoCard)
  // Position after sorting by facet_counts.topics -> idx, so card tag colors use the global idx (not the card-internal idx)
  const topicRankMap = useMemo<Record<string, number>>(() => {
    const fc = data?.facet_counts?.topics ?? {};
    return Object.entries(fc)
      .sort(([, a], [, b]) => b - a)
      .reduce<Record<string, number>>((acc, [topic], idx) => {
        acc[topic] = idx;
        return acc;
      }, {});
  }, [data?.facet_counts?.topics]);

  return (
    <div className="flex h-full flex-col">
      {/* Page header */}
      <div className="border-b px-6 py-4 bg-background">
        <div className="flex items-center justify-between gap-4">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <Boxes className="h-5 w-5 text-primary" aria-hidden="true" />
              <h1 className="text-xl font-semibold">Ecosystem Repo Profiles</h1>
            </div>
            <p className="text-sm text-muted-foreground mt-1">
              A broad index view of open-source repos such as Claude Agent / MCP / Memory / Skill. Click a card for details.
              {projectName && (
                <>
                  {' '}Currently filtered by project <span className="text-primary font-medium">{projectName}</span>.
                </>
              )}
            </p>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span className="text-xs text-muted-foreground">Ecosystem Project</span>
            <EcosystemProjectFilter />
            <Button
              variant="outline"
              size="sm"
              className="shrink-0"
              nativeButton={false}
              render={<Link to="/ecosystem/batches" />}
            >
              <Layers className="mr-1 h-4 w-4" aria-hidden="true" />
              Shallow Scan Batches
            </Button>
          </div>
          <Button
            variant="outline"
            size="sm"
            className="shrink-0"
            nativeButton={false}
            render={<Link to="/ecosystem/research" />}
          >
            <SearchIcon className="mr-1 h-4 w-4" aria-hidden="true" />
            Find Candidates
          </Button>
        </div>
      </div>

      {/* Recent batch scan overview (v1.5.2: moved back from the single-repo detail page to the ecosystem-profile level) */}
      <RecentScanRunsBar />

      {/* Stats bar */}
      {!error && !isLoading && (
        <EcosystemStatsBar
          allProfiles={profiles}
          facetCounts={data?.facet_counts}
          total={data?.total}
        />
      )}

      {/* Filter bar */}
      <FilterBar
        filters={filters}
        onChange={setFilters}
        totalCount={filtered.length}
        facetCounts={data?.facet_counts}
      />

      {/* List area */}
      <div className="flex-1 overflow-y-auto p-4">
        {isLoading && (
          <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {Array.from({ length: 8 }).map((_, i) => (
              <Skeleton key={i} className="h-44 rounded-lg" />
            ))}
          </div>
        )}

        {error && (
          <div
            className="flex items-start gap-2 p-4 rounded-lg border border-destructive/50 bg-destructive/10 text-destructive"
            role="alert"
          >
            <AlertCircle className="h-5 w-5 mt-0.5 shrink-0" aria-hidden="true" />
            <div className="text-sm">
              <p className="font-medium">Failed to Load Ecosystem Profiles</p>
              <p className="mt-1 text-xs opacity-80">
                {error.message}
                {error.message.includes('Not Found') && (
                  <span className="block mt-1">
                    Hint: the API server may not have registered the /api/ecosystem routes yet; make sure the backend has been restarted.
                  </span>
                )}
              </p>
            </div>
          </div>
        )}

        {!isLoading && !error && filtered.length === 0 && (
          <div className="flex flex-col items-center justify-center h-64 text-center text-muted-foreground">
            <Boxes className="h-10 w-10 opacity-30 mb-2" aria-hidden="true" />
            <p className="text-sm">No matching repos</p>
            <p className="text-xs mt-1">
              {filters.keyword || filters.category || filters.minStars
                ? 'Try adjusting the filters or clearing them'
                : projectId
                  ? 'There are no ecosystem repo profiles under the current project yet. Switch to "All Projects", or run a scan task to populate them'
                  : 'Profiles will be populated after a scan task runs'}
            </p>
          </div>
        )}

        {!isLoading && !error && filtered.length > 0 && (
          <>
            <div className="grid gap-3 grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {filtered.map((repo) => (
                <RepoCard key={repo.id} repo={repo} topicRankMap={topicRankMap} />
              ))}
            </div>

            {/* Paginator: shown only when the total exceeds one page */}
            {(data?.total ?? 0) > PAGE_SIZE && (
              <div className="flex items-center justify-between mt-4 pt-4 border-t">
                <div className="text-sm text-muted-foreground">
                  {(page - 1) * PAGE_SIZE + 1} –
                  {' - '}
                  <span className="font-medium text-foreground">
                    {Math.min(page * PAGE_SIZE, data?.total ?? 0)}
                  </span>
                  {' '}of <span className="font-medium text-foreground">{data?.total ?? 0}</span> repos
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                    disabled={page === 1}
                  >
                    <ChevronLeft className="h-4 w-4 mr-1" aria-hidden="true" />
                    Previous
                  </Button>
                  <span className="text-sm text-muted-foreground px-2">
                    Page {page} / {Math.ceil((data?.total ?? 0) / PAGE_SIZE)}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setPage((p) => p + 1)}
                    disabled={!data?.has_more}
                  >
                    Next
                    <ChevronRight className="h-4 w-4 ml-1" aria-hidden="true" />
                  </Button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
