import { Activity, CheckCircle2, XCircle, Loader2, Clock } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import type { EcosystemScanRun } from '@/api/ecosystem';
import { formatDateTimeCompact, serverTimeMs } from '@/lib/datetime';

interface ScanRunSectionProps {
  scanRun: EcosystemScanRun | null;
}

function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case 'completed':
      return (
        <CheckCircle2 className="h-3.5 w-3.5 text-emerald-500" aria-label="Completed" />
      );
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
  // In progress: compute live elapsed time from the start time to now
  if (!end) {
    const ms = Date.now() - serverTimeMs(start);
    if (Number.isNaN(ms) || ms < 0) return 'In Progress';
    const sec = Math.floor(ms / 1000);
    if (sec < 60) return `In Progress · ${sec}s`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `In Progress · ${min}m ${sec % 60}s`;
    const hr = Math.floor(min / 60);
    return `In Progress · ${hr}h ${min % 60}m`;
  }
  const ms = serverTimeMs(end) - serverTimeMs(start);
  if (Number.isNaN(ms) || ms < 0) return '—';
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const remSec = sec % 60;
  if (min < 60) return `${min}m ${remSec}s`;
  const hr = Math.floor(min / 60);
  return `${hr}h ${min % 60}m`;
}

/**
 * Scan run section — shows the most recent scan record that brought this repo into the archive.
 */
export function ScanRunSection({ scanRun }: ScanRunSectionProps) {
  if (!scanRun) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base flex items-center gap-2">
            <Activity className="h-4 w-4" aria-hidden="true" />
            Scan Record
          </CardTitle>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          No associated scan run record (the repo profile may have been written by an old batch or imported manually).
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Activity className="h-4 w-4" aria-hidden="true" />
          Scan Record
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        <div className="flex items-center justify-between py-1 border-b border-border/40">
          <span className="text-muted-foreground text-xs flex items-center gap-1.5">
            <StatusIcon status={scanRun.status} />
            Status
          </span>
          <Badge variant="outline" className="text-xs">
            {scanRun.status}
          </Badge>
        </div>
        <div className="flex items-center justify-between py-1 border-b border-border/40">
          <span className="text-muted-foreground text-xs">Scan Type</span>
          <span className="font-medium text-xs">{scanRun.scan_type}</span>
        </div>
        <div className="flex items-center justify-between py-1 border-b border-border/40">
          <span className="text-muted-foreground text-xs">Started At</span>
          <span className="font-medium text-xs">{formatDateTime(scanRun.started_at)}</span>
        </div>
        <div className="flex items-center justify-between py-1 border-b border-border/40">
          <span className="text-muted-foreground text-xs">Elapsed</span>
          <span className="font-medium text-xs">
            {formatDuration(scanRun.started_at, scanRun.completed_at)}
          </span>
        </div>
        <div className="flex items-center justify-between py-1 border-b border-border/40">
          <span className="text-muted-foreground text-xs">Added This Run</span>
          <span className="font-medium text-xs">{scanRun.repos_added}</span>
        </div>
        <div className="flex items-center justify-between py-1 border-b border-border/40">
          <span className="text-muted-foreground text-xs">Updated This Run</span>
          <span className="font-medium text-xs">{scanRun.repos_updated}</span>
        </div>
        {scanRun.agent_id && (
          <div className="flex items-center justify-between py-1 border-b border-border/40">
            <span className="text-muted-foreground text-xs">Executing Agent</span>
            <span className="font-medium text-xs truncate max-w-[60%]" title={scanRun.agent_id}>
              {scanRun.agent_id}
            </span>
          </div>
        )}
        {scanRun.notes && (
          <div className="pt-2">
            <p className="text-muted-foreground text-xs mb-1">Notes</p>
            <p className="text-xs whitespace-pre-wrap text-foreground/80">{scanRun.notes}</p>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
