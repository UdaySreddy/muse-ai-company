import { useMemo, useState } from 'react';
import {
  Activity,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleAlert,
  Hourglass,
  Sparkles,
  Users,
  Wrench,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  type EcosystemDeepReview,
  type EcosystemRepoProfile,
  STAGE_STATUS_LABELS,
  stageBadgeClass,
} from '@/api/ecosystem';
import { formatDateTimeCompact, serverTimeMs } from '@/lib/datetime';

interface ResearchTimelineProps {
  profile: EcosystemRepoProfile;
  reviews: EcosystemDeepReview[];
}

interface TimelineEntry {
  key: string;
  stage: string;
  title: string;
  at: string | null;
  agent: string | null;
  body?: string;
  meta?: Record<string, string | null | undefined>;
  icon: typeof Sparkles;
  isFailure: boolean;
}

function fmtDate(iso: string | null | undefined): string {
  return formatDateTimeCompact(iso);
}

function buildTimeline(
  profile: EcosystemRepoProfile,
  reviews: EcosystemDeepReview[],
): TimelineEntry[] {
  const entries: TimelineEntry[] = [];

  // Stage 0 — shallow scan (profile level; may have no corresponding deep_review)
  if (profile.shallow_summary && profile.shallow_summary.trim().length > 0) {
    entries.push({
      key: 'shallow',
      stage: 'shallow_done',
      title: 'Stage 0 · Shallow Scan Done',
      at: profile.last_shallow_refreshed_at ?? null,
      agent: null,
      body: profile.shallow_summary,
      icon: Sparkles,
      isFailure: false,
    });
  } else if ((profile.fetch_failure_count ?? 0) >= 3) {
    entries.push({
      key: 'shallow_failed',
      stage: 'shallow_failed',
      title: 'Stage 0 · Shallow Scan Failed',
      at: null,
      agent: null,
      body: profile.last_fetch_error || 'Fetch failed multiple times',
      icon: CircleAlert,
      isFailure: true,
    });
  } else {
    entries.push({
      key: 'queued',
      stage: 'queued',
      title: 'Stage 0 · Pending Shallow Scan',
      at: null,
      agent: null,
      icon: Hourglass,
      isFailure: false,
    });
  }

  // Sort all reviews by created_at
  const sortedReviews = [...reviews].sort(
    (a, b) => serverTimeMs(a.created_at) - serverTimeMs(b.created_at),
  );

  for (const r of sortedReviews) {
    const stage = (r.stage_status as string) ?? 'queued';
    const isFail = stage.endsWith('_failed');

    // Stage 1 — architecture analysis
    if (r.architecture_completed_at || r.architecture_md) {
      entries.push({
        key: `arch-${r.id}`,
        stage: 'architecture_done',
        title: 'Stage 1 · Architecture Analyzed',
        at: r.architecture_completed_at ?? r.created_at,
        agent: r.agent_id,
        body: r.architecture_md,
        meta: { 'review id': r.id },
        icon: Wrench,
        isFailure: false,
      });
    } else if (stage === 'architecture_failed') {
      entries.push({
        key: `arch-fail-${r.id}`,
        stage: 'architecture_failed',
        title: 'Stage 1 · Architecture Analysis Failed',
        at: r.created_at,
        agent: r.agent_id,
        body: r.risks_md || 'Architecture analysis failed',
        icon: CircleAlert,
        isFailure: true,
      });
    }

    // Stage 2 — debate
    if (r.debated_at || stage === 'debated') {
      entries.push({
        key: `debate-${r.id}`,
        stage: 'debated',
        title: 'Stage 2 · Multi-Angle Debate Finished',
        at: r.debated_at ?? r.created_at,
        agent: r.agent_id,
        body: r.integration_md || r.learnings_md || r.risks_md,
        meta: {
          'Meeting ID': r.debate_meeting_id,
          Risks: r.risks_md ? 'Recorded' : null,
          Learnings: r.learnings_md ? 'Recorded' : null,
          'Integration Recommendation': r.integration_recommendation,
        },
        icon: Users,
        isFailure: isFail,
      });
    }

    // Stage 3 — referenced / integrated
    if (stage === 'referenced' || r.stage3_completed_at) {
      entries.push({
        key: `ref-${r.id}`,
        stage: stage === 'integrated' ? 'integrated' : 'referenced',
        title: stage === 'integrated' ? 'Stage 3 · ★ Integrated' : 'Stage 3 · ✓ Marked as Reference',
        at: r.stage3_completed_at ?? r.created_at,
        agent: r.agent_id,
        meta: {
          'Integration Task ID': r.integration_task_id,
        },
        icon: CircleCheck,
        isFailure: false,
      });
    } else if (stage === 'integrated') {
      entries.push({
        key: `intg-${r.id}`,
        stage: 'integrated',
        title: 'Stage 3 · ★ Integrated',
        at: r.stage3_completed_at ?? r.created_at,
        agent: r.agent_id,
        meta: { 'Integration Task ID': r.integration_task_id },
        icon: CircleCheck,
        isFailure: false,
      });
    }
  }

  return entries;
}

function TimelineNode({ entry, isLast }: { entry: TimelineEntry; isLast: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const Icon = entry.icon;
  const dotClass = entry.isFailure
    ? 'bg-rose-500/15 text-rose-600 border-rose-500/40'
    : entry.stage === 'queued'
      ? 'bg-muted text-muted-foreground border-border'
      : 'bg-primary/15 text-primary border-primary/30';

  const hasDetail =
    !!entry.body || !!Object.values(entry.meta ?? {}).find((v) => v != null && v !== '');

  return (
    <div className="flex gap-3">
      <div className="flex flex-col items-center flex-shrink-0">
        <div
          className={`h-7 w-7 rounded-full flex items-center justify-center border ${dotClass}`}
        >
          <Icon className="h-3.5 w-3.5" aria-hidden="true" />
        </div>
        {!isLast && <div className="w-px flex-1 bg-border mt-1 mb-1" />}
      </div>

      <div className="pb-4 min-w-0 flex-1">
        <button
          type="button"
          onClick={() => hasDetail && setExpanded(!expanded)}
          className="w-full text-left flex items-start justify-between gap-2 group"
        >
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 flex-wrap">
              <span
                className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${stageBadgeClass(entry.stage)}`}
              >
                {STAGE_STATUS_LABELS[entry.stage as keyof typeof STAGE_STATUS_LABELS] ?? entry.stage}
              </span>
              <span className="text-sm font-medium">{entry.title}</span>
            </div>
            <div className="mt-1 text-xs text-muted-foreground flex items-center gap-3 flex-wrap">
              <span>{fmtDate(entry.at)}</span>
              {entry.agent && <span>by {entry.agent}</span>}
            </div>
          </div>
          {hasDetail && (
            expanded
              ? <ChevronDown className="h-3.5 w-3.5 text-muted-foreground mt-1 shrink-0" />
              : <ChevronRight className="h-3.5 w-3.5 text-muted-foreground mt-1 shrink-0 opacity-60 group-hover:opacity-100" />
          )}
        </button>

        {expanded && hasDetail && (
          <div className="mt-2 space-y-2 rounded bg-muted/40 px-3 py-2">
            {entry.body && (
              <div className="text-xs text-foreground whitespace-pre-wrap leading-relaxed">
                {entry.body.length > 800 ? entry.body.slice(0, 800) + '…' : entry.body}
              </div>
            )}
            {entry.meta && Object.entries(entry.meta).filter(([, v]) => v != null && v !== '').length > 0 && (
              <div className="flex flex-wrap gap-2">
                {Object.entries(entry.meta).map(([k, v]) =>
                  v == null || v === '' ? null : (
                    <Badge key={k} variant="outline" className="text-[10px]">
                      {k}: {v}
                    </Badge>
                  ),
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

/**
 * Research history timeline — shows one repo's progression through the funnel stages.
 * v1.5.0-E §8.2: Stage 0 shallow scan → Stage 1 architecture → Stage 2 debate → Stage 3 reference/integrate.
 */
export function ResearchTimeline({ profile, reviews }: ResearchTimelineProps) {
  const entries = useMemo(() => buildTimeline(profile, reviews), [profile, reviews]);

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Activity className="h-4 w-4" aria-hidden="true" />
            Research History ({entries.length} events)
          </CardTitle>
        </CardHeader>
        <CardContent>
          {entries.length === 0 ? (
            <p className="text-sm text-muted-foreground">No stage events yet</p>
          ) : (
            <div>
              {entries.map((e, i) => (
                <TimelineNode key={e.key} entry={e} isLast={i === entries.length - 1} />
              ))}
            </div>
          )}
        </CardContent>
      </Card>

      {/* shallow_summary history snapshot (currently shows the current value; will be provided by EcosystemRepoStatusSnapshot in the future) */}
      {profile.shallow_summary && (
        <Card>
          <CardHeader>
            <CardTitle className="text-base flex items-center gap-2">
              <Sparkles className="h-4 w-4" aria-hidden="true" />
              Shallow Scan Summary Snapshot
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-2">
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <span>Last refreshed: {fmtDate(profile.last_shallow_refreshed_at)}</span>
              </div>
              <p className="text-sm whitespace-pre-wrap leading-relaxed">
                {profile.shallow_summary}
              </p>
              <p className="text-[10px] text-muted-foreground italic mt-2">
                History snapshots are kept append-only in the EcosystemRepoStatusSnapshot table; a future version will expand them here.
              </p>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
