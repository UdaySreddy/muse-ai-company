import { ChevronDown, ChevronRight } from 'lucide-react';
import { useState } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { cn } from '@/lib/utils';
import { formatDateTimeCompact } from '@/lib/datetime';
import { useT } from '@/i18n';
import type { CoverageReport, DispatchPath, UnattributedCode, UnattributedSamples } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// ② Unattributed drawer — directly below the matrix, expanded by default (design §5.2 ②).
//
// Its sole reason to exist: turn "coverage 78%" into an **actionable** statement. After reading this block, the user should
// know whether the remaining 22% can be recovered, and how. So "not recoverable" and "not yet recovered" must be labelled separately —
// merging the former into the latter would strip this block of all its value.
// ─────────────────────────────────────────────────────────────────────────────

type Recoverability = 'recoverable' | 'unrecoverable' | 'na';

const RECOVERABILITY: Record<UnattributedCode, Recoverability> = {
  no_transcript_path: 'unrecoverable',
  transcript_gone: 'unrecoverable',
  not_yet_measured: 'recoverable',
  by_design: 'na',
  self_report_absent: 'unrecoverable',
  multi_task_unsplittable: 'unrecoverable',
};

const TONE: Record<Recoverability, string> = {
  recoverable:
    'border-emerald-300 bg-emerald-50 text-emerald-800 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200',
  unrecoverable:
    'border-red-300 bg-red-50 text-red-800 dark:border-red-900 dark:bg-red-950/40 dark:text-red-200',
  na: 'border-border bg-muted text-muted-foreground',
};

export interface UnattributedDrawerProps {
  report: CoverageReport;
  samples: UnattributedSamples | undefined;
  path: DispatchPath;
  onPathChange: (path: DispatchPath) => void;
}

export function UnattributedDrawer({ report, samples, path, onPathChange }: UnattributedDrawerProps) {
  const t = useT();
  // Expanded by default: this block's value lies in being read; collapsed, it might as well not exist.
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const label: Record<UnattributedCode, { name: string; hint: string }> = {
    no_transcript_path: { name: t.usage.reasonNoPath, hint: t.usage.reasonNoPathHint },
    transcript_gone: { name: t.usage.reasonGone, hint: t.usage.reasonGoneHint },
    not_yet_measured: { name: t.usage.reasonNotMeasured, hint: t.usage.reasonNotMeasuredHint },
    by_design: { name: t.usage.reasonByDesign, hint: t.usage.reasonByDesignHint },
    self_report_absent: {
      name: t.usage.reasonSelfReportAbsent,
      hint: t.usage.reasonSelfReportAbsentHint,
    },
    multi_task_unsplittable: { name: t.usage.reasonMultiTask, hint: t.usage.reasonMultiTaskHint },
  };
  const recoverLabel: Record<Recoverability, string> = {
    recoverable: t.usage.recoverable,
    unrecoverable: t.usage.unrecoverable,
    na: t.usage.notApplicable,
  };
  const pathName: Record<DispatchPath, string> = {
    subagent: t.usage.pathSubagent,
    leader_session: t.usage.pathLeader,
    workflow_self_report: t.usage.pathWorkflowSelfReport,
    tool_call: t.usage.pathToolCall,
  };

  const row = report.rows.find((r) => r.path === path);
  const counts = { ...(row?.unattributed_reasons ?? {}) } as Record<string, number>;
  // by_design describes the **tool-call level** (activity records have no token fields), which is not the same unit as a dispatch.
  // So it never enters the backend dict that shares the denominator's unit. But it must **have a row** in this block —
  // otherwise "not collected by design" appears only once, in the matrix, and readers would assume it was an omission.
  const toolCallRow = report.rows.find((r) => r.path === 'tool_call');
  const codes = Object.keys(counts) as UnattributedCode[];
  const total = codes.reduce((n, code) => n + (counts[code] ?? 0), 0);

  // Only the two paths on the agents table can drill down to sample rows (the other two are not agents rows)
  const drillable = path === 'subagent' || path === 'leader_session';

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.usage.unattributedTitle}</CardTitle>
        <CardDescription>{t.usage.unattributedDesc}</CardDescription>
        <div className="flex flex-wrap gap-1.5 pt-1" role="group" aria-label={t.usage.colPath}>
          {report.rows.map((r) => (
            <button
              key={r.path}
              type="button"
              onClick={() => onPathChange(r.path)}
              aria-pressed={r.path === path}
              className={cn(
                'rounded border px-2 py-1 text-xs transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                r.path === path
                  ? 'border-primary bg-primary text-primary-foreground'
                  : 'hover:bg-muted',
              )}
            >
              {pathName[r.path] ?? r.path}
            </button>
          ))}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {codes.length === 0 && !toolCallRow && (
          <p className="text-sm text-muted-foreground">{t.common.noData}</p>
        )}

        {codes.map((code) => {
          const count = counts[code] ?? 0;
          const kind = RECOVERABILITY[code] ?? 'unrecoverable';
          const rows = drillable ? (samples?.samples?.[code] ?? []) : [];
          const open = !collapsed[code];
          return (
            <div key={code} className="rounded border">
              <button
                type="button"
                onClick={() => setCollapsed((prev) => ({ ...prev, [code]: !collapsed[code] }))}
                aria-expanded={open}
                className="flex w-full items-start gap-2 px-3 py-2 text-left focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              >
                {open ? (
                  <ChevronDown className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                ) : (
                  <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden />
                )}
                <span className="min-w-0 flex-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{label[code]?.name ?? code}</span>
                    <code className="rounded bg-muted px-1 py-0.5 text-[11px] text-muted-foreground">
                      {code}
                    </code>
                    <span className={cn('rounded border px-1.5 py-0.5 text-[11px]', TONE[kind])}>
                      {recoverLabel[kind]}
                    </span>
                  </span>
                  <span className="mt-0.5 block text-xs text-muted-foreground">
                    {label[code]?.hint ?? ''}
                  </span>
                </span>
                <span className="shrink-0 text-right">
                  <span className="block text-lg leading-tight font-semibold tabular-nums">
                    {count.toLocaleString()}
                  </span>
                  <span className="block text-[11px] text-muted-foreground tabular-nums">
                    {total > 0 ? `${((count / total) * 100).toFixed(1)}%` : '—'}
                  </span>
                </span>
              </button>
              {open && (
                <div className="border-t px-3 py-2">
                  {rows.length === 0 ? (
                    <p className="text-xs text-muted-foreground">{t.usage.noSamples}</p>
                  ) : (
                    <>
                      <p className="mb-1.5 text-[11px] text-muted-foreground">
                        {t.usage.samplesOf(rows.length)}
                      </p>
                      <ul className="space-y-1">
                        {rows.map((s) => (
                          <li key={s.agent_id} className="rounded bg-muted/50 px-2 py-1 text-xs">
                            <div className="flex flex-wrap items-baseline gap-x-2">
                              <span className="font-medium">{s.name || s.agent_id}</span>
                              <code className="text-[10px] text-muted-foreground">{s.agent_id}</code>
                              {s.created_at && (
                                <span className="text-[10px] text-muted-foreground">
                                  {formatDateTimeCompact(s.created_at)}
                                </span>
                              )}
                            </div>
                            {s.transcript_path && (
                              <div className="mt-0.5 truncate font-mono text-[10px] text-muted-foreground">
                                {s.transcript_path}
                              </div>
                            )}
                          </li>
                        ))}
                      </ul>
                    </>
                  )}
                </div>
              )}
            </div>
          );
        })}

        {/* by_design: one formal-value row with no samples to open — it counts tool calls, not dispatches */}
        {toolCallRow && (
          <div className="rounded border border-dashed px-3 py-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="font-medium">{t.usage.reasonByDesign}</span>
              <code className="rounded bg-muted px-1 py-0.5 text-[11px] text-muted-foreground">
                by_design
              </code>
              <span className={cn('rounded border px-1.5 py-0.5 text-[11px]', TONE.na)}>
                {t.usage.notApplicable}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground">{t.usage.reasonByDesignHint}</p>
          </div>
        )}

        {drillable && samples && (
          <p className="text-[11px] text-muted-foreground">{t.usage.sampleScanNote(samples.scanned)}</p>
        )}
      </CardContent>
    </Card>
  );
}
