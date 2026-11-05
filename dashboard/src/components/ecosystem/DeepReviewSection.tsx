import { useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  FileSearch,
  Building2,
  ShieldAlert,
  Lightbulb,
  Beaker,
  CheckCircle2,
  XCircle,
  Clock,
  Loader2,
  ExternalLink,
  ChevronDown,
  ChevronUp,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
  DEEP_REVIEW_STATUS_LABELS,
  INTEGRATION_RECOMMENDATION_LABELS,
  STAGE_STATUS_LABELS,
  stageBadgeClass,
} from '@/api/ecosystem';
import type { EcosystemDeepReview } from '@/api/ecosystem';
import { useReportDetail } from '@/api/reports';
import { formatDateTime, formatDurationMs, serverTimeDiffMs, serverTimeMs } from '@/lib/datetime';

interface DeepReviewSectionProps {
  reviews: EcosystemDeepReview[];
  /** Shallow-scan summary (profile level; shown when stage=shallow_done to avoid a pile of "no data") */
  shallowSummary?: string | null;
}

/** Status badge */
function StatusBadge({ status }: { status: string }) {
  const map: Record<string, { className: string; Icon: typeof CheckCircle2 }> = {
    completed: {
      className: 'bg-emerald-500/10 text-emerald-600 border-emerald-500/30',
      Icon: CheckCircle2,
    },
    running: { className: 'bg-blue-500/10 text-blue-600 border-blue-500/30', Icon: Loader2 },
    pending: { className: 'bg-muted text-muted-foreground border-border', Icon: Clock },
    failed: { className: 'bg-rose-500/10 text-rose-600 border-rose-500/30', Icon: XCircle },
    skipped: { className: 'bg-amber-500/10 text-amber-600 border-amber-500/30', Icon: XCircle },
  };
  const { className, Icon } = map[status] ?? map.pending;
  const label = DEEP_REVIEW_STATUS_LABELS[status] ?? status;
  return (
    <Badge variant="outline" className={`text-xs ${className}`}>
      <Icon className={`h-3 w-3 mr-1 ${status === 'running' ? 'animate-spin' : ''}`} aria-hidden="true" />
      {label}
    </Badge>
  );
}

/** Integration recommendation badge */
function RecommendationBadge({ rec }: { rec: string | null }) {
  if (!rec) return null;
  const styles: Record<string, string> = {
    adopt: 'bg-emerald-500/15 text-emerald-700 dark:text-emerald-300 border-emerald-500/40',
    experiment: 'bg-blue-500/15 text-blue-700 dark:text-blue-300 border-blue-500/40',
    hold: 'bg-amber-500/15 text-amber-700 dark:text-amber-300 border-amber-500/40',
    avoid: 'bg-rose-500/15 text-rose-700 dark:text-rose-300 border-rose-500/40',
  };
  const label = INTEGRATION_RECOMMENDATION_LABELS[rec] ?? rec;
  return (
    <Badge variant="outline" className={`text-xs font-medium ${styles[rec] ?? ''}`}>
      Recommendation: {label}
    </Badge>
  );
}

/** Renders a markdown subsection — collapsible/expandable. Empty fields show a "no data" placeholder instead of hiding the whole section. */
function MarkdownBlock({
  title,
  Icon,
  body,
  hideWhenEmpty = false,
}: {
  title: string;
  Icon: typeof Building2;
  body: string;
  /** When true, empty fields are not rendered at all (for optional blocks such as demo logs); defaults to false, which shows the placeholder */
  hideWhenEmpty?: boolean;
}) {
  const isEmpty = !body || !body.trim();
  if (isEmpty && hideWhenEmpty) return null;
  return (
    <section className="space-y-1.5">
      <h4 className="text-sm font-semibold flex items-center gap-1.5">
        <Icon className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
        {title}
      </h4>
      <div className="md-prose text-sm max-w-none pl-5">
        {isEmpty ? (
          <p className="text-xs text-muted-foreground italic">No data yet — this field will be filled once the corresponding stage completes</p>
        ) : (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
        )}
      </div>
    </section>
  );
}

/** A single review record card (spans multiple stages: shallow scan / architecture deep review / debate / integration) */
function ReviewCard({
  review,
  shallowSummary,
}: {
  review: EcosystemDeepReview;
  shallowSummary?: string | null;
}) {
  const [expanded, setExpanded] = useState(true);
  const [showFullReport, setShowFullReport] = useState(false);

  const { data: reportDetail, isLoading: reportLoading } = useReportDetail(
    showFullReport && review.report_id ? review.report_id : null,
  );

  const formatDate = (iso: string): string =>
    formatDateTime(iso, {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });

  // Actual review elapsed time: based on the difference started_at -> (shallow_completed_at | completed_at).
  // Historic rows mostly have duration_seconds = 0, so the frontend computes it itself.
  const formatElapsed = (
    start: string | null | undefined,
    end: string | null | undefined,
  ): string => {
    if (!start || !end) return '—';
    const ms = serverTimeDiffMs(start, end);
    return ms > 0 ? formatDurationMs(ms) : '—';
  };

  const stage = (review.stage_status as string) ?? '';
  const isShallowOnly = stage === 'shallow_done' || stage === 'queued' || stage === 'shallow_failed';
  const stageLabel = stage
    ? STAGE_STATUS_LABELS[stage as keyof typeof STAGE_STATUS_LABELS] ?? stage
    : null;
  const stageClass = stage ? stageBadgeClass(stage) : '';

  // v1.5.2: at the shallow-scan stage, summary/architecture/risks/learnings are always empty; shallow_summary lives on the profile.
  // Use the stage to decide which fields to show, avoiding the jarring "completed" + "a pile of no-data" combination.
  const showDeepFields = !isShallowOnly;
  const hasDeepContent = useMemo(
    () =>
      Boolean(
        review.summary_md ||
          review.architecture_md ||
          review.risks_md ||
          review.learnings_md ||
          review.demo_log_excerpt,
      ),
    [review],
  );

  // For shallow-scan completion, the elapsed-time endpoint prefers shallow_completed_at (when the backend did not write completed_at)
  const elapsedEnd = isShallowOnly
    ? review.shallow_completed_at ?? review.completed_at
    : review.completed_at;

  return (
    <div className="border rounded-md p-3 space-y-3 bg-card">
      <div className="flex flex-wrap items-center gap-2 justify-between">
        <div className="flex flex-wrap items-center gap-2">
          {/* v1.5.2: when stage_status exists, show only the stage badge, avoiding the semantic clash of status='running' + stage='shallow_done'
              showing both "Deep review in progress (spinner)" and "Shallow scan done" at once. In terminal states such as failed/integrated, fall back to the status badge. */}
          {stageLabel ? (
            <Badge variant="outline" className={`text-xs ${stageClass}`} title="Current stage progress">
              {stageLabel}
            </Badge>
          ) : (
            <StatusBadge status={review.status} />
          )}
          <RecommendationBadge rec={review.integration_recommendation} />
          <span className="text-xs text-muted-foreground" title="Created at">
            {formatDate(review.created_at)}
          </span>
          <span className="text-xs text-muted-foreground" title="started_at → current stage completion time">
            Elapsed {formatElapsed(review.started_at, elapsedEnd)}
          </span>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="h-7 px-2 text-xs"
          onClick={() => setExpanded((v) => !v)}
          aria-label={expanded ? 'Collapse review details' : 'Expand review details'}
        >
          {expanded ? (
            <>
              <ChevronUp className="h-3.5 w-3.5 mr-1" aria-hidden="true" />
              Collapse
            </>
          ) : (
            <>
              <ChevronDown className="h-3.5 w-3.5 mr-1" aria-hidden="true" />
              Expand
            </>
          )}
        </Button>
      </div>

      {expanded && (
        <div className="space-y-3">
          {/* Shallow-scan stage: show profile.shallow_summary directly; do not show deep-review-only empty fields */}
          {isShallowOnly && shallowSummary && (
            <MarkdownBlock title="Shallow Scan Summary" Icon={FileSearch} body={shallowSummary} />
          )}
          {isShallowOnly && stage !== 'queued' && (
            <p className="text-xs text-muted-foreground italic">
              Currently at the shallow-scan stage. Fields such as architecture / risks / learnings will be filled after entering the deep review.
            </p>
          )}

          {/* D5 convergence: the in-flight placeholder gate is judged uniformly on the authoritative axis stage==='queued'; status no longer acts as
              an authoritative signal ('running' exists only on historic rows, and the orphan value 'pending' is never produced by the backend enum;
              the two former status branches have been merged and deleted). Note showDeepFields must not be layered on top —
              queued is shallow-only, so layering it makes the condition permanently false dead code (V2 review finding). */}
          {!hasDeepContent && !review.report_id && stage === 'queued' && (
            <div className="flex items-start gap-2 text-xs text-blue-600 dark:text-blue-400 bg-blue-500/10 border border-blue-500/30 rounded p-2">
              <Loader2 className="h-3.5 w-3.5 mt-0.5 shrink-0 animate-spin" aria-hidden="true" />
              <span>Review in progress — once the current stage completes, this card's fields will be filled automatically. Refresh this page later to check.</span>
            </div>
          )}
          {showDeepFields && (
            <>
              <MarkdownBlock title="Summary" Icon={FileSearch} body={review.summary_md} />
              <MarkdownBlock title="Architecture" Icon={Building2} body={review.architecture_md} />
              <MarkdownBlock title="Risks" Icon={ShieldAlert} body={review.risks_md} />
              <MarkdownBlock title="Learnings" Icon={Lightbulb} body={review.learnings_md} />
            </>
          )}

          {(review.demo_result || review.demo_log_excerpt) && (
            <section className="space-y-1.5">
              <h4 className="text-sm font-semibold flex items-center gap-1.5">
                <Beaker className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
                Demo Run
                {review.demo_result && (
                  <Badge variant="outline" className="text-[10px] ml-1">
                    {review.demo_result}
                  </Badge>
                )}
              </h4>
              {review.demo_log_excerpt && (
                <pre className="text-[11px] bg-muted/50 border rounded p-2 max-h-60 overflow-auto whitespace-pre-wrap break-words">
                  {review.demo_log_excerpt}
                </pre>
              )}
            </section>
          )}

          {review.report_id && (
            <div className="pt-2 border-t border-border/50">
              {!showFullReport ? (
                <Button
                  variant="outline"
                  size="sm"
                  className="text-xs"
                  onClick={() => setShowFullReport(true)}
                >
                  <ExternalLink className="h-3.5 w-3.5 mr-1.5" aria-hidden="true" />
                  View Full Report
                </Button>
              ) : (
                <div className="space-y-2">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium">Full Report Content</span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-6 px-2 text-xs"
                      onClick={() => setShowFullReport(false)}
                    >
                      Collapse
                    </Button>
                  </div>
                  {reportLoading ? (
                    <p className="text-xs text-muted-foreground">Loading report...</p>
                  ) : reportDetail ? (
                    <div className="md-prose text-sm max-w-none border rounded p-3 bg-muted/30 max-h-96 overflow-auto">
                      <ReactMarkdown remarkPlugins={[remarkGfm]}>
                        {reportDetail.content}
                      </ReactMarkdown>
                    </div>
                  ) : (
                    <p className="text-xs text-muted-foreground">Failed to load report</p>
                  )}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Review records section — shows all review records for this repo (shallow/deep/debate/integration stages share one row, newest first).
 */
export function DeepReviewSection({ reviews, shallowSummary }: DeepReviewSectionProps) {
  if (!reviews || reviews.length === 0) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <FileSearch className="h-4 w-4" aria-hidden="true" />
            Review Records
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No review records have been generated for this repo yet. A shallow scan first produces a short summary, then deep-review scheduling follows by relevance.
        </CardContent>
      </Card>
    );
  }

  // In reverse chronological order
  const sorted = [...reviews].sort(
    (a, b) => serverTimeMs(b.created_at) - serverTimeMs(a.created_at),
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <FileSearch className="h-4 w-4" aria-hidden="true" />
          Review Records ({reviews.length})
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {sorted.map((review) => (
          <ReviewCard key={review.id} review={review} shallowSummary={shallowSummary} />
        ))}
      </CardContent>
    </Card>
  );
}
