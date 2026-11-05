import { createElement } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import {
  Sparkles,
  RefreshCcw,
  Tag as TagIcon,
  TrendingUp,
  Archive,
  CircleAlert,
  Pin,
  PinOff,
  XCircle,
  Microscope,
  FlaskConical,
  FileSearch,
  Building2,
  ShieldAlert,
  Lightbulb,
  Beaker,
} from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import {
  INTEGRATION_RECOMMENDATION_LABELS,
  STAGE_STATUS_LABELS,
  type ScanHistoryEntry,
} from '@/api/ecosystem';
import { formatDateTimeCompact } from '@/lib/datetime';

interface ScanHistoryTimelineProps {
  entries: ScanHistoryEntry[];
}

/** event_type → labels */
const EVENT_LABELS: Record<string, string> = {
  discovered: 'First Discovered',
  rescanned: 'Rescanned',
  topics_changed: 'Topics Changed',
  stars_jumped: 'Stars Jumped',
  status_changed: 'Status Changed',
  archived: 'Archived',
  manual_pinned: 'Manually Pinned',
  manual_unpinned: 'Unpinned',
  removed_from_query: 'Dropped from Query',
  shallow_done: 'Shallow Scan Done',
  shallow_failed: 'Shallow Scan Failed',
};

/** event_type → icon */
function getIcon(type: string) {
  if (type.startsWith('deep_review_')) return Microscope;
  switch (type) {
    case 'discovered':
      return Sparkles;
    case 'rescanned':
      return RefreshCcw;
    case 'topics_changed':
      return TagIcon;
    case 'stars_jumped':
      return TrendingUp;
    case 'archived':
      return Archive;
    case 'manual_pinned':
      return Pin;
    case 'manual_unpinned':
      return PinOff;
    case 'removed_from_query':
      return XCircle;
    case 'status_changed':
      return CircleAlert;
    case 'shallow_done':
      return FlaskConical;
    default:
      return CircleAlert;
  }
}

/** event_type → color tone */
function getToneClass(type: string): string {
  if (type.startsWith('deep_review_')) {
    return 'text-purple-700 dark:text-purple-300 bg-purple-500/10 border-purple-500/30';
  }
  switch (type) {
    case 'discovered':
      return 'text-emerald-700 dark:text-emerald-300 bg-emerald-500/10 border-emerald-500/30';
    case 'rescanned':
    case 'topics_changed':
    case 'shallow_done':
      return 'text-amber-700 dark:text-amber-300 bg-amber-500/10 border-amber-500/30';
    case 'stars_jumped':
      return 'text-blue-700 dark:text-blue-300 bg-blue-500/10 border-blue-500/30';
    case 'archived':
    case 'status_changed':
      return 'text-sky-700 dark:text-sky-300 bg-sky-500/10 border-sky-500/30';
    case 'manual_pinned':
    case 'manual_unpinned':
      return 'text-orange-700 dark:text-orange-300 bg-orange-500/10 border-orange-500/30';
    case 'removed_from_query':
      return 'text-rose-700 dark:text-rose-300 bg-rose-500/10 border-rose-500/30';
    default:
      return 'text-muted-foreground bg-muted border-border';
  }
}

function getLabel(type: string): string {
  if (type.startsWith('deep_review_')) {
    const stage = type.replace('deep_review_', '');
    const stageLabel = STAGE_STATUS_LABELS[stage as keyof typeof STAGE_STATUS_LABELS] ?? stage;
    return `Deep Review · ${stageLabel}`;
  }
  return EVENT_LABELS[type] ?? type;
}

function formatTime(iso: string): string {
  return formatDateTimeCompact(iso);
}

/** Single markdown section — title + collapsible body */
function MarkdownBlock({
  title,
  Icon,
  body,
}: {
  title: string;
  Icon: typeof Building2;
  body: string;
}) {
  if (!body || !body.trim()) return null;
  return (
    <section className="space-y-1.5">
      <h4 className="text-xs font-semibold flex items-center gap-1.5 text-muted-foreground uppercase tracking-wide">
        <Icon className="h-3.5 w-3.5" aria-hidden="true" />
        {title}
      </h4>
      <div className="md-prose text-sm max-w-none pl-5">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
      </div>
    </section>
  );
}

/** A single timeline row — collapsible details, git commit log style */
function TimelineRow({ entry }: { entry: ScanHistoryEntry }) {
  // getIcon returns an existing Lucide component reference; use createElement rather than an <Icon/> local variable
  // to avoid react-hooks/static-components (a capitalized local component must not appear as JSX inside a render body).
  const iconNode = createElement(getIcon(entry.type), {
    className: 'h-3.5 w-3.5',
    'aria-hidden': 'true',
  });
  const toneClass = getToneClass(entry.type);
  const label = getLabel(entry.type);
  const hasDetails =
    (entry.payload && Object.keys(entry.payload).length > 0) ||
    (entry.expandable_md &&
      Object.values(entry.expandable_md).some((v) => v && v.trim()));

  return (
    <details className="group rounded-md border border-border/60 bg-card hover:border-border transition-colors">
      <summary className="cursor-pointer list-none flex items-start gap-2.5 p-3">
        {/* Icon */}
        <span
          className={`inline-flex items-center justify-center h-7 w-7 rounded-full border shrink-0 mt-0.5 ${toneClass}`}
        >
          {iconNode}
        </span>
        {/* Body */}
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge variant="outline" className={`text-xs ${toneClass} border-current/30`}>
              {label}
            </Badge>
            <span className="text-xs text-muted-foreground">{formatTime(entry.timestamp)}</span>
            {entry.source && entry.source !== 'scanner' && (
              <span className="text-xs text-muted-foreground/70">· {entry.source}</span>
            )}
            {entry.integration_recommendation && (
              <Badge variant="outline" className="text-xs">
                Recommendation:{' '}
                {INTEGRATION_RECOMMENDATION_LABELS[entry.integration_recommendation] ??
                  entry.integration_recommendation}
              </Badge>
            )}
          </div>
          <p className="text-sm mt-1 text-foreground/90 break-words">{entry.summary}</p>
        </div>
        {/* Expand hint */}
        {hasDetails && (
          <span className="shrink-0 text-xs text-muted-foreground self-center select-none">
            <span className="group-open:hidden">Expand</span>
            <span className="hidden group-open:inline">Collapse</span>
          </span>
        )}
      </summary>

      {/* Expanded body */}
      {hasDetails && (
        <div className="px-3 pb-3 pt-1 ml-9 space-y-3 border-t border-border/40">
          {/* event payload */}
          {entry.kind === 'event' && entry.payload && Object.keys(entry.payload).length > 0 && (
            <pre className="text-[11px] bg-muted/40 border rounded p-2 max-h-60 overflow-auto whitespace-pre-wrap break-words">
              {JSON.stringify(entry.payload, null, 2)}
            </pre>
          )}

          {/* deep_review markdown, 5 sections */}
          {entry.expandable_md && (
            <div className="space-y-3">
              <MarkdownBlock title="Summary" Icon={FileSearch} body={entry.expandable_md.summary ?? ''} />
              <MarkdownBlock title="Architecture" Icon={Building2} body={entry.expandable_md.architecture ?? ''} />
              <MarkdownBlock title="Risks" Icon={ShieldAlert} body={entry.expandable_md.risks ?? ''} />
              <MarkdownBlock title="Learnings" Icon={Lightbulb} body={entry.expandable_md.learnings ?? ''} />
              <MarkdownBlock title="Integration Recommendation" Icon={Beaker} body={entry.expandable_md.integration ?? ''} />
            </div>
          )}
        </div>
      )}
    </details>
  );
}

/**
 * Scan/research history timeline — git commit log style.
 * v1.6.0: merged ResearchTimeline + EventTimeline into 1 tab, in reverse chronological order.
 */
export function ScanHistoryTimeline({ entries }: ScanHistoryTimelineProps) {
  if (!entries || entries.length === 0) {
    return (
      <Card className="border-dashed">
        <CardContent className="p-6 text-center text-sm text-muted-foreground">
          No scan or research records yet — entries appear automatically once the repo has been scanned or deep-reviewed
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-2.5">
      {entries.map((entry, i) => (
        <TimelineRow key={`${entry.kind}-${entry.timestamp}-${i}`} entry={entry} />
      ))}
    </div>
  );
}
