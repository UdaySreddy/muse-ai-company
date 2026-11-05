import { useMemo } from 'react';
import { Boxes, FileSearch, Archive, Tag, FolderOpen, Folder } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import type { EcosystemRepoProfile, EcosystemFacetCounts } from '@/api/ecosystem';
import { TOPIC_COLOR_PALETTE } from '@/api/ecosystem';
import { useProject } from '@/context/ProjectContext';
import { serverTimeMs } from '@/lib/datetime';

interface EcosystemStatsBarProps {
  /** The full list (before filtering), used to compute pending-deep-review / inactive / totals */
  allProfiles: EcosystemRepoProfile[];
  /** Backend facet_counts (returned when facetCounts=true) */
  facetCounts?: EcosystemFacetCounts;
  /** The total reported by the backend (result count after backend filtering) */
  total?: number;
}

/** A single stat card */
function StatCard({
  Icon,
  label,
  value,
  hint,
  tone = 'default',
}: {
  Icon: typeof Boxes;
  label: string;
  value: string | number;
  hint?: string;
  tone?: 'default' | 'primary' | 'warning' | 'info';
}) {
  const toneClass: Record<string, string> = {
    default: 'text-foreground',
    primary: 'text-primary',
    warning: 'text-amber-600 dark:text-amber-400',
    info: 'text-blue-600 dark:text-blue-400',
  };
  return (
    <Card className="px-3 py-2 flex flex-col gap-0.5 min-w-0 flex-1">
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground flex items-center gap-1">
        <Icon className="h-3 w-3" aria-hidden="true" />
        {label}
      </span>
      <span className={`text-xl font-semibold leading-none ${toneClass[tone]}`}>
        {value}
      </span>
      {hint && (
        <span className="text-[10px] text-muted-foreground truncate" title={hint}>
          {hint}
        </span>
      )}
    </Card>
  );
}

/**
 * Stats bar — the metrics view at the top of the ecosystem list page (statistics for the currently selected project's ecosystem library).
 *
 * Semantics (release-blocker fix):
 *   - Deep-reviewed = the real row count with EcosystemDeepReview.status='completed' (not needs_deep_review=False)
 *   - Pending deep review = number of repos with profile.needs_deep_review=True
 *   - Inactive = is_archived=True or last_commit_at > 365 days
 *   - Coverage = deep-reviewed / total repos
 *
 * Previous bug: treating `profile.needs_deep_review === false` as "deep-reviewed" displayed 163,
 * while in reality there were only 3 completed DeepReviews. "needs_deep_review" means "needs a deep review";
 * false covers both "auto-skipped for low relevance" and "completed", so it cannot be equated with deep-reviewed.
 */
export function EcosystemStatsBar({
  allProfiles,
  facetCounts,
  total,
}: EcosystemStatsBarProps) {
  const { projectName } = useProject();

  const stats = useMemo(() => {
    const totalCount = total ?? allProfiles.length;
    // v1.5.1: the pending-shallow-scan total uses the backend facet full value (not truncated by limit=200);
    // when facets are unavailable, fall back to a list estimate (marked as a lower bound).
    const stageFacet = facetCounts?.stage ?? {};
    const needsDeepCount =
      facetCounts?.stage !== undefined
        ? stageFacet.queued ?? 0
        : allProfiles.filter((p) => (p.stage_status ?? 'queued') === 'queued').length;
    // v1.6.0 "Archived": prefer last_active_status (GitHub archived / manually marked worthless);
    // when the field is missing, fall back to the old rule (is_archived || last_commit > 365 days).
    const now = Date.now();
    const hasStatusField = allProfiles.some((p) => p.last_active_status != null);
    const archivedCount = hasStatusField
      ? allProfiles.filter(
          (p) =>
            p.last_active_status === 'archived' ||
            p.last_active_status === 'manual_archived',
        ).length
      : allProfiles.filter((p) => {
          if (p.is_archived) return true;
          if (!p.last_commit_at) return false;
          const days = (now - serverTimeMs(p.last_commit_at)) / (1000 * 60 * 60 * 24);
          return days > 365;
        }).length;
    // Total deep-reviewed = the full count whose stage has reached architecture_done+
    const deepDoneCount =
      (stageFacet.architecture_done ?? 0) +
      (stageFacet.debated ?? 0) +
      (stageFacet.referenced ?? 0) +
      (stageFacet.integrated ?? 0);
    const isFacetAvailable = facetCounts?.stage !== undefined;
    return { totalCount, needsDeepCount, archivedCount, deepDoneCount, isFacetAvailable };
  }, [allProfiles, facetCounts, total]);

  // v1.6.0 SST: Top 8 popular topics (GitHub-native topics dimension, replacing the heuristic-based category)
  // Uses full facet_counts.topics statistics, not truncated by limit
  const topTopics = useMemo(() => {
    if (!facetCounts?.topics) return [];
    return Object.entries(facetCounts.topics)
      .filter(([, n]) => n > 0)
      .sort(([, a], [, b]) => b - a)
      .slice(0, 8);
  }, [facetCounts]);

  return (
    <div className="flex flex-col gap-2 px-4 pt-3">
      {/* Current-project chip — so the user always knows which project's data they are viewing */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-xs text-muted-foreground">Current view:</span>
        <Badge
          variant="outline"
          className={`text-xs gap-1 ${projectName ? 'border-primary/40 bg-primary/5 text-primary' : ''}`}
        >
          {projectName ? (
            <FolderOpen className="h-3 w-3" aria-hidden="true" />
          ) : (
            <Folder className="h-3 w-3" aria-hidden="true" />
          )}
          {projectName ?? 'All Projects'}
        </Badge>
        {topTopics.length > 0 && (
          <>
            <span className="text-xs text-muted-foreground ml-2">Popular Topics:</span>
            <div className="flex items-center gap-1 flex-wrap">
              {topTopics.map(([topic, n], idx) => (
                <Badge
                  key={topic}
                  variant="secondary"
                  className={`text-[10px] gap-1 ${TOPIC_COLOR_PALETTE[idx % TOPIC_COLOR_PALETTE.length]}`}
                >
                  <Tag className="h-2.5 w-2.5" aria-hidden="true" />
                  {topic}
                  <span className="ml-0.5 opacity-70">{n}</span>
                </Badge>
              ))}
            </div>
          </>
        )}
      </div>

      {/* Numeric card row (v1.5.1: uses full facet stage statistics, not truncated by limit)*/}
      <div className="flex flex-wrap gap-2">
        <StatCard
          Icon={Boxes}
          label="Current View"
          value={stats.totalCount}
          hint="All repos permanently participate in search; check 'Show deleted' to view deleted / made-private repos"
          tone="default"
        />
        <StatCard
          Icon={FileSearch}
          label="Pending Shallow Scan"
          value={stats.needsDeepCount}
          hint="Shallow scan = read README/CHANGELOG/releases and summarize features and design direction (automatic, batched)"
          tone="info"
        />
        <StatCard
          Icon={FileSearch}
          label="Researched"
          value={stats.deepDoneCount}
          hint="This repo has been included in a system-change investigation (cards/details show research count and history)"
          tone="primary"
        />
        <StatCard
          Icon={Archive}
          label="Archived"
          value={stats.archivedCount}
          hint="GitHub-archived or manually marked worthless"
          tone="warning"
        />
      </div>
    </div>
  );
}
