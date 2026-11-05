import { Link } from 'react-router-dom';
import { useState } from 'react';
import {
  Star,
  GitBranch,
  Calendar,
  AlertCircle,
  Archive,
  RefreshCcw,
  AlertTriangle,
  FlaskConical,
} from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useToast } from '@/components/shared/useToast';
import type { EcosystemRepoProfile } from '@/api/ecosystem';
import { useRetryFailedRepo, TOPIC_COLOR_PALETTE } from '@/api/ecosystem';
import { serverTimeMs } from '@/lib/datetime';

interface RepoCardProps {
  /** Repo profile data */
  repo: EcosystemRepoProfile;
  /** Explicit stage (inferred from profile fields when omitted) */
  stage?: string;
  /** Global topic ranking map (from facet_counts.topics); decides topic badge color positions */
  topicRankMap?: Record<string, number>;
}

/**
 * Format a star count: 1234 -> 1.2k, 12345 -> 12.3k, 1234567 -> 1.2M
 */
function formatStars(stars: number): string {
  if (stars >= 1_000_000) return `${(stars / 1_000_000).toFixed(1)}M`;
  if (stars >= 1_000) return `${(stars / 1_000).toFixed(1)}k`;
  return String(stars);
}

/**
 * Compute days since today — used for the last_commit_at display.
 */
function daysSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const then = serverTimeMs(iso);
  if (Number.isNaN(then)) return null;
  return Math.floor((Date.now() - then) / (1000 * 60 * 60 * 24));
}

/**
 * Single-repo card — the basic unit of the list view. Click to open the detail page.
 * v1.5.0-E: added the stage badge + failed red highlight + immediate-retry button.
 * v1.6.0 SST: the stage is derived entirely from the backend stage_status; the frontend no longer infers a fallback.
 */
export function RepoCard({ repo, stage: stageProp, topicRankMap }: RepoCardProps) {
  const lastCommitDays = daysSince(repo.last_commit_at);
  const isStale = lastCommitDays !== null && lastCommitDays > 180;
  const summary =
    repo.shallow_summary || repo.one_line_summary || repo.description_excerpt || repo.description || 'No description yet';

  // v1.6.0 SST: use the stage_status exposed by the backend; defaults to queued
  const stage = stageProp ?? repo.stage_status ?? 'queued';
  const researchCount = repo.research_count ?? 0;
  const isFailed = stage.endsWith('_failed') || (repo.fetch_failure_count ?? 0) >= 3;
  const isDeleted = !!repo.is_deleted;
  const isPrivate = !!repo.is_private_now;

  const retry = useRetryFailedRepo();
  const [retryDone, setRetryDone] = useState(false);
  const { showToast, toastNode } = useToast();

  const onRetry = (e: React.MouseEvent) => {
    e.preventDefault();
    e.stopPropagation();
    if (retry.isPending) return;
    retry.mutate(repo.id, {
      onSuccess: (res) => {
        setRetryDone(true);
        // Clear the success notice after 2 seconds
        setTimeout(() => setRetryDone(false), 2000);
        // Surface the backend's honest hint — resetting the counter does not mean an immediate refetch; it waits for the next tick
        showToast(res.next_action);
      },
    });
  };

  // Failed cards use a red border + light background
  const cardBorder = isFailed
    ? 'border-rose-300/60 bg-rose-50/40 dark:border-rose-700/40 dark:bg-rose-950/20 hover:border-rose-400'
    : 'hover:border-primary/50 hover:bg-accent/30';

  return (
    <>
      {toastNode}
      <Link
        to={`/ecosystem/${repo.id}`}
        className="block focus:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-lg"
        aria-label={`View ${repo.repo_full_name} details`}
      >
        <Card className={`h-full transition-colors ${cardBorder}`}>
          <CardContent className="p-4 space-y-2.5">
            {/* Header: repo name + star */}
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0 flex-1">
                <h3 className="font-semibold text-sm leading-snug truncate">
                  {repo.repo_full_name}
                </h3>
                {repo.language && (
                  <p className="text-xs text-muted-foreground mt-0.5">{repo.language}</p>
                )}
              </div>
              <div className="flex items-center gap-1 text-xs text-muted-foreground shrink-0">
                <Star className="h-3.5 w-3.5 text-yellow-500" aria-hidden="true" />
                <span className="font-medium">{formatStars(repo.stars)}</span>
              </div>
            </div>

            {/* Badge row: research count + abnormal states (v1.5.1: removed the stage text badge; stage details live in the research history)*/}
            {(researchCount > 0 || isDeleted || isPrivate) && (
              <div className="flex flex-wrap items-center gap-1">
                {researchCount > 0 && (
                  <span
                    className="inline-flex items-center gap-0.5 rounded border border-primary/30 bg-primary/10 px-1.5 py-0.5 text-[10px] font-medium text-primary"
                    title={`Researched ${researchCount} times (click for details and research history: system changes involved / relevance / adopted or not)`}
                  >
                    <FlaskConical className="h-2.5 w-2.5" aria-hidden="true" />
                    Researched ×{researchCount}
                  </span>
                )}
                {isDeleted && (
                  <span className="inline-flex items-center gap-0.5 rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:text-rose-300">
                    <AlertTriangle className="h-2.5 w-2.5" aria-hidden="true" />
                    Deleted
                  </span>
                )}
                {isPrivate && (
                  <span className="inline-flex items-center gap-0.5 rounded border border-rose-500/30 bg-rose-500/10 px-1.5 py-0.5 text-[10px] font-medium text-rose-700 dark:text-rose-300">
                    <AlertTriangle className="h-2.5 w-2.5" aria-hidden="true" />
                    Made Private
                  </span>
                )}
              </div>
            )}

            {/* One-line summary (prefers shallow_summary) */}
            <p className="text-xs text-muted-foreground line-clamp-2 min-h-[2rem]">
              {summary}
            </p>

            {/* Failure error notice + retry button */}
            {isFailed && (
              <div className="rounded border border-rose-300/40 bg-rose-100/50 dark:bg-rose-950/30 px-2 py-1.5">
                <div className="flex items-start gap-1.5">
                  <AlertTriangle className="h-3 w-3 text-rose-600 mt-0.5 shrink-0" aria-hidden="true" />
                  <div className="min-w-0 flex-1">
                    <p
                      className="text-[10px] text-rose-700 dark:text-rose-300 line-clamp-2"
                      title={repo.last_fetch_error || ''}
                    >
                      {repo.last_fetch_error || 'Fetch failed (attempts ≥ 3)'}
                    </p>
                    <p className="text-[10px] text-rose-600/70 mt-0.5">
                      Failures {repo.fetch_failure_count ?? 0}
                    </p>
                  </div>
                  <Button
                    variant="outline"
                    size="xs"
                    className="shrink-0 border-rose-300 text-rose-700 hover:bg-rose-100 dark:border-rose-700/40 dark:text-rose-300"
                    onClick={onRetry}
                    disabled={retry.isPending || retryDone}
                    aria-label="Retry now"
                  >
                    <RefreshCcw className={`h-3 w-3 ${retry.isPending ? 'animate-spin' : ''}`} aria-hidden="true" />
                    {retryDone ? 'Queued' : 'Retry'}
                  </Button>
                </div>
              </div>
            )}

            {/* Tag row: topics (v1.6.0: removed the relevance_category heuristic classification display; colors cycle by position via TOPIC_COLOR_PALETTE) */}
            {repo.topics && repo.topics.length > 0 && (
              <div className="flex flex-wrap items-center gap-1">
                {repo.topics.slice(0, 4).map((topic, idx) => {
                  // Prefer the global ranking (topicRankMap); fall back to the local idx by default
                  const globalIdx = topicRankMap?.[topic] ?? idx;
                  return (
                    <Badge
                      key={topic}
                      variant="outline"
                      className={`text-[10px] px-1.5 py-0 h-4 ${TOPIC_COLOR_PALETTE[globalIdx % TOPIC_COLOR_PALETTE.length]}`}
                    >
                      {topic}
                    </Badge>
                  );
                })}
                {repo.topics.length > 4 && (
                  <span className="text-[10px] text-muted-foreground">
                    +{repo.topics.length - 4}
                  </span>
                )}
              </div>
            )}

            {/* Bottom status bar */}
            <div className="flex items-center gap-2 text-[10px] text-muted-foreground pt-1 border-t">
              {lastCommitDays !== null && (
                <span className="flex items-center gap-1">
                  <GitBranch className="h-3 w-3" aria-hidden="true" />
                  {lastCommitDays === 0 ? 'Today' : `${lastCommitDays}d ago`}
                </span>
              )}
              {repo.is_archived && (
                <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                  <Archive className="h-3 w-3" aria-hidden="true" />
                  Archived
                </span>
              )}
              {isStale && !repo.is_archived && (
                <span className="flex items-center gap-1 text-amber-600 dark:text-amber-400">
                  <Calendar className="h-3 w-3" aria-hidden="true" />
                  Dormant
                </span>
              )}
              {repo.needs_deep_review && !isFailed && (
                <span className="ml-auto flex items-center gap-1 text-blue-600 dark:text-blue-400">
                  <AlertCircle className="h-3 w-3" aria-hidden="true" />
                  Pending Deep Review
                </span>
              )}
              {/* v1.6.1: removed "Relevance X/10" — the hardcoded relevance_score has no reference value */}
            </div>
          </CardContent>
        </Card>
      </Link>
    </>
  );
}
