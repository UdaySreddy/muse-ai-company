import { Activity, CheckCircle2, XCircle, Loader2, Clock, ChevronDown, ChevronUp } from 'lucide-react';
import { useState } from 'react';
import { Card, CardContent } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { useEcosystemRecentScanRuns } from '@/api/ecosystem';
import type { EcosystemScanRun } from '@/api/ecosystem';
import { formatDateTimeCompact, serverTimeMs } from '@/lib/datetime';

function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'completed':
      return <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" aria-label="Completed" />;
    case 'running':
      return <Loader2 className="h-3.5 w-3.5 text-blue-500 animate-spin" aria-label="In Progress" />;
    case 'failed':
      return <XCircle className="h-3.5 w-3.5 text-rose-500" aria-label="Failed" />;
    default:
      return <Clock className="h-3.5 w-3.5 text-muted-foreground" aria-label={status} />;
  }
}

function formatDateTime(iso: string | null): string {
  return formatDateTimeCompact(iso);
}

function formatDuration(start: string, end: string | null): string {
  if (!end) {
    const ms = Date.now() - serverTimeMs(start);
    if (Number.isNaN(ms) || ms < 0) return 'In Progress';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return `In Progress · ${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `In Progress · ${min}m ${sec % 60}s`;
    return `In Progress · ${Math.floor(min / 60)}h ${min % 60}m`;
  }
  const ms = serverTimeMs(end) - serverTimeMs(start);
  if (Number.isNaN(ms) || ms < 0) return '—';
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ${sec % 60}s`;
  return `${Math.floor(min / 60)}h ${min % 60}m`;
}

function ScanRunRow({ run, isLatest }: { run: EcosystemScanRun; isLatest?: boolean }) {
  return (
    <div className="flex items-center gap-3 text-xs py-1.5 border-b border-border/30 last:border-0">
      <StatusIcon status={run.status} />
      <span className="font-medium min-w-[120px]">{formatDateTime(run.started_at)}</span>
      <Badge variant="outline" className="text-[10px]">
        {run.scan_type}
      </Badge>
      <span className="text-muted-foreground" title="Elapsed (computed live from started_at to completed_at)">
        {formatDuration(run.started_at, run.completed_at)}
      </span>
      <span className="text-muted-foreground">
        <span title="Repos archived for the first time (not previously in the DB)">
          Added <span className="text-foreground font-medium">{run.repos_added}</span>
        </span>
        {' · '}
        <span
          title={
            run.metadata_changed_count != null && run.metadata_changed_count > 0
              ? `Repos already in the DB had their metadata refetched. ${run.metadata_changed_count} actually changed topics/stars/description; the rest only refreshed last_scanned_at`
              : 'Repos already in the DB had their metadata refetched (no real changes)'
          }
        >
          Updated <span className="text-foreground font-medium">{run.repos_updated}</span>
          {run.metadata_changed_count != null && run.metadata_changed_count > 0 && (
            <span className="text-amber-600 dark:text-amber-400">
              {' '}(incl. {run.metadata_changed_count} real changes)
            </span>
          )}
        </span>
      </span>
      {isLatest && (
        <Badge variant="outline" className="text-[10px] bg-primary/10 text-primary border-primary/30 ml-auto">
          Latest
        </Badge>
      )}
    </div>
  );
}

/**
 * Top of the ecosystem profile home — recent batch scan overview (shows the latest 1 by default; expandable to N).
 * Data: GET /api/ecosystem/scan-runs?limit=5
 *
 * v1.5.2 refactor: batch scan records moved from the single-repo detail page to the ecosystem profile page (ecosystem-level data ≠ single-repo-level).
 */
export function RecentScanRunsBar() {
  const { data, isLoading } = useEcosystemRecentScanRuns(5);
  const [expanded, setExpanded] = useState(false);

  if (isLoading) {
    return (
      <Card className="mx-4 mt-2 mb-1">
        <CardContent className="py-2 text-xs text-muted-foreground">Loading batch scan records…</CardContent>
      </Card>
    );
  }

  const runs = data?.data ?? [];
  if (runs.length === 0) {
    return (
      <Card className="mx-4 mt-2 mb-1">
        <CardContent className="py-2 text-xs text-muted-foreground flex items-center gap-2">
          <Activity className="h-3.5 w-3.5" aria-hidden="true" />
          No batch scan records yet
        </CardContent>
      </Card>
    );
  }

  const latest = runs[0];
  const rest = runs.slice(1);

  return (
    <Card className="mx-4 mt-2 mb-1">
      <CardContent className="py-2 px-3">
        <div className="flex items-center gap-2 mb-1">
          <Activity className="h-3.5 w-3.5 text-muted-foreground" aria-hidden="true" />
          <span className="text-xs font-medium text-muted-foreground">Batch Scan Records</span>
          {rest.length > 0 && (
            <Button
              variant="ghost"
              size="sm"
              className="h-6 px-2 text-xs ml-auto"
              onClick={() => setExpanded((v) => !v)}
              aria-label={expanded ? 'Collapse history' : 'Expand history'}
            >
              {expanded ? (
                <>
                  <ChevronUp className="h-3 w-3 mr-1" aria-hidden="true" />
                  Collapse ({rest.length} earlier)
                </>
              ) : (
                <>
                  <ChevronDown className="h-3 w-3 mr-1" aria-hidden="true" />
                  Expand ({rest.length} earlier)
                </>
              )}
            </Button>
          )}
        </div>
        <ScanRunRow run={latest} isLatest />
        {expanded && rest.map((run) => <ScanRunRow key={run.id} run={run} />)}
      </CardContent>
    </Card>
  );
}
