import { AlertTriangle } from 'lucide-react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import { MetricBadge } from '@/components/shared/MetricBadge';
import { cn } from '@/lib/utils';
import { useT } from '@/i18n';
import type { CoverageReport, CoverageRow, DispatchPath, EdgeCoverage } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// ① Coverage matrix — the first block of the first screen, not collapsible, not closable (design §5.2 ①).
//
// This is the page's **body**, not its header: first answer "how much was measured", then "who burned the measured part".
// Three hard rules are baked into this component:
//   1. Every row carries a metric column; ctx_last rows and usage_sum rows must be distinguishable at a glance (different background + badge);
//   2. **No total row** — the two metrics differ 5-25x in practice, so any cross-row total mixes metrics;
//   3. "Not collected by design" is a formal value, not a blank — a blank would be read as a bug.
// ─────────────────────────────────────────────────────────────────────────────

/** Coverage threshold colors: below 50% is the red zone, 50-90% is caution, above 90% is healthy. */
function coverageTone(pct: number): string {
  if (pct >= 90) return 'text-emerald-600 dark:text-emerald-400';
  if (pct >= 50) return 'text-amber-600 dark:text-amber-400';
  return 'text-red-600 dark:text-red-400';
}

/** Tints the whole row by metric. Visual partitioning is the most important thing about this table, not decoration. */
function rowTone(row: CoverageRow): string {
  if (!row.metric) return 'bg-muted/40';
  if (row.metric === 'ctx_last') return 'bg-amber-50/70 dark:bg-amber-950/20';
  return '';
}

function pctText(attributed: number, total: number): string {
  if (total <= 0) return '0.0%';
  return `${((attributed / total) * 100).toFixed(1)}%`;
}

/** A row's three numbers (the first two do not apply when the denominator is null). Shared by the table and the narrow-screen cards so the two never compute separately. */
function rowNumbers(row: CoverageRow) {
  const total = row.dispatches_total ?? 0;
  const attributed = row.dispatches_attributed ?? 0;
  return {
    // A null denominator = the question "how much was measured" does not apply on this path. Render it as a formal value, not a blank:
    // a blank would be read as a bug, and 0 would be read as "never measured once".
    notCollected: row.dispatches_total === null,
    total,
    attributed,
    unattributed: total - attributed,
  };
}

export interface CoverageMatrixProps {
  report: CoverageReport;
  /** When a row's unattributed number is clicked, switch the drawer to that path */
  onFocusPath?: (path: DispatchPath) => void;
}

export function CoverageMatrix({ report, onFocusPath }: CoverageMatrixProps) {
  const t = useT();

  const pathName: Record<DispatchPath, string> = {
    subagent: t.usage.pathSubagent,
    leader_session: t.usage.pathLeader,
    workflow_self_report: t.usage.pathWorkflowSelfReport,
    tool_call: t.usage.pathToolCall,
  };

  // The narrowest hop = the ceiling of end-to-end attribution. R5 requires it to be disclosed on the same screen as coverage; otherwise a pretty
  // overall coverage would hide the real bottleneck (in practice the bottleneck is never token collection, but agent->task).
  const narrowest = report.hops.reduce<EdgeCoverage | null>((worst, hop) => {
    if (hop.required <= 0) return worst;
    if (!worst) return hop;
    return hop.resolvable / hop.required < worst.resolvable / worst.required ? hop : worst;
  }, null);

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.usage.matrixTitle}</CardTitle>
        <CardDescription>{t.usage.matrixDesc}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Narrow screens: stacked cards. At 375px the table needs horizontal scrolling to reveal the numbers, and this block is the page's
            body — hiding the body inside an unhinted horizontal-scroll container means the first screen says nothing. */}
        <div className="space-y-2 md:hidden">
          {report.rows.map((row) => {
            const { notCollected, total, attributed, unattributed } = rowNumbers(row);
            return (
              <div key={row.path} className={cn('rounded border p-3', rowTone(row))}>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="font-medium">{pathName[row.path] ?? row.path}</span>
                  {row.metric ? (
                    <MetricBadge metric={row.metric} />
                  ) : (
                    <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
                      {t.usage.notCollected}
                    </span>
                  )}
                </div>
                {!notCollected && (
                  <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-1 text-xs">
                    <span
                      className={cn(
                        'text-base font-medium tabular-nums',
                        coverageTone((attributed / (total || 1)) * 100),
                      )}
                    >
                      {pctText(attributed, total)}
                    </span>
                    <span className="tabular-nums text-muted-foreground">
                      {t.usage.colMeasured} {attributed.toLocaleString()} / {t.usage.colDispatches}{' '}
                      {total.toLocaleString()}
                    </span>
                    {unattributed > 0 && (
                      <button
                        type="button"
                        onClick={() => onFocusPath?.(row.path)}
                        className="rounded text-muted-foreground underline underline-offset-2 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                      >
                        {unattributed.toLocaleString()} {t.usage.unattributedShort}
                      </button>
                    )}
                  </div>
                )}
                {row.note && (
                  <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{row.note}</p>
                )}
              </div>
            );
          })}
        </div>

        <div className="hidden md:block">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>{t.usage.colPath}</TableHead>
                <TableHead className="text-right">{t.usage.colDispatches}</TableHead>
                <TableHead className="text-right">{t.usage.colMeasured}</TableHead>
                <TableHead className="text-right">{t.usage.colCoverage}</TableHead>
                <TableHead>{t.usage.colMetric}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {report.rows.map((row) => {
                const { notCollected, total, attributed, unattributed } = rowNumbers(row);
                return (
                  <TableRow key={row.path} className={cn(rowTone(row), 'align-top')}>
                    <TableCell className="max-w-md">
                      <div className="font-medium">{pathName[row.path] ?? row.path}</div>
                      {row.note && (
                        <div className="mt-0.5 text-xs leading-relaxed text-muted-foreground">
                          {row.note}
                        </div>
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {notCollected ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        total.toLocaleString()
                      )}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {notCollected ? (
                        <span className="text-muted-foreground">—</span>
                      ) : (
                        attributed.toLocaleString()
                      )}
                    </TableCell>
                    <TableCell className="text-right">
                      {notCollected ? (
                        <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">
                          {t.usage.notCollected}
                        </span>
                      ) : (
                        <div className="space-y-0.5">
                          <div
                            className={cn(
                              'font-medium tabular-nums',
                              coverageTone((attributed / (total || 1)) * 100),
                            )}
                          >
                            {pctText(attributed, total)}
                          </div>
                          {unattributed > 0 && (
                            <button
                              type="button"
                              onClick={() => onFocusPath?.(row.path)}
                              className="rounded text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                            >
                              {unattributed.toLocaleString()} {t.usage.unattributedShort}
                            </button>
                          )}
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      {row.metric ? (
                        <MetricBadge metric={row.metric} />
                      ) : (
                        <span className="text-muted-foreground">—</span>
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>

        {/* The total row's slot deliberately carries this note — without it, readers would just assume it was forgotten */}
        <p className="rounded border border-dashed px-3 py-2 text-xs text-muted-foreground">
          {t.usage.noTotalRow}
        </p>

        <div className="space-y-2">
          <div>
            <h3 className="text-sm font-medium">{t.usage.hopsTitle}</h3>
            <p className="text-xs text-muted-foreground">{t.usage.hopsDesc}</p>
          </div>
          {narrowest && (
            <div className="flex items-start gap-2 rounded border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-200">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>
                {t.usage.narrowestHop(
                  narrowest.edge,
                  pctText(narrowest.resolvable, narrowest.required),
                )}
                {narrowest.note ? ` — ${narrowest.note}` : ''}
              </span>
            </div>
          )}
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {report.hops.map((hop) => {
              const pct = hop.required > 0 ? (hop.resolvable / hop.required) * 100 : 0;
              return (
                <div key={hop.edge} className="rounded border px-3 py-2" title={hop.note}>
                  <div className="flex items-baseline justify-between gap-2">
                    <code className="text-xs">{hop.edge}</code>
                    <span className={cn('text-sm font-medium tabular-nums', coverageTone(pct))}>
                      {pctText(hop.resolvable, hop.required)}
                    </span>
                  </div>
                  <div className="tabular-nums text-xs text-muted-foreground">
                    {hop.resolvable.toLocaleString()} / {hop.required.toLocaleString()}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
