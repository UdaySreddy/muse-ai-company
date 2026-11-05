import { useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useQueryClient } from '@tanstack/react-query';
import { Button } from '@/components/ui/button';
import { CoverageMatrix } from '@/components/usage/CoverageMatrix';
import { UnattributedDrawer } from '@/components/usage/UnattributedDrawer';
import { AttributionDrill } from '@/components/usage/AttributionDrill';
import { SingleProbeCard } from '@/components/usage/SingleProbeCard';
import { MetricLegend } from '@/components/usage/MetricLegend';
import { cn } from '@/lib/utils';
import { formatDateTimeSystemLocale } from '@/lib/datetime';
import { useT } from '@/i18n';
import { useUnattributedSamples, useUsageCoverage, type DispatchPath } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// /usage — token usage attribution (docs/token-attribution-v1-design.md §5).
//
// Five-block structure; **the top-to-bottom order is the priority order** and must not be rearranged:
//   ① Coverage matrix (first block of the first screen, not collapsible, not closable) — this is the page body, not the header
//   ② Unattributed drawer (directly below the matrix, expanded by default)
//   ③ Attributed detail (drillable; leader sessions and sub-agents listed separately, not merged by default)
//   ④ Single-measurement card (exempt from the coverage gate; the price is that it may only come from a single transcript parse)
//   ⑤ Metric definitions (permanent footer)
//
// This is an **internal diagnostics page**, not first-screen narrative material — it sits low in the sidebar, and is deliberately not crammed into
// /analytics (that page is about team output efficiency — a different reader mindset from usage attribution; mixing them would dilute the coverage
// labelling).
// ─────────────────────────────────────────────────────────────────────────────

const WINDOWS = [0, 7, 30, 90];

export function UsagePage() {
  const t = useT();
  const queryClient = useQueryClient();
  const [days, setDays] = useState(0);
  const [drawerPath, setDrawerPath] = useState<DispatchPath>('subagent');
  const [probeAgentId, setProbeAgentId] = useState('');

  const coverage = useUsageCoverage(days);
  const samples = useUnattributedSamples(
    drawerPath === 'subagent' || drawerPath === 'leader_session' ? drawerPath : 'subagent',
    days,
  );

  // agent->task is the narrowest hop in the whole chain, and the side-branch card's title must print it truthfully — end-to-end task-level
  // attribution can never exceed that number; hiding it would make the side-branch card look more credible than it is.
  const taskHop = coverage.data?.hops.find((h) => h.edge === 'agent->task');
  const taskPct =
    taskHop && taskHop.required > 0
      ? `${((taskHop.resolvable / taskHop.required) * 100).toFixed(1)}%`
      : '—';

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold">{t.usage.title}</h1>
          <p className="text-sm text-muted-foreground">{t.usage.subtitle}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex items-center gap-1" role="group" aria-label={t.usage.windowLabel}>
            {WINDOWS.map((d) => (
              <button
                key={d}
                type="button"
                onClick={() => setDays(d)}
                aria-pressed={d === days}
                className={cn(
                  'rounded border px-2 py-1 text-xs transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none',
                  d === days ? 'border-primary bg-primary text-primary-foreground' : 'hover:bg-muted',
                )}
              >
                {d === 0 ? t.usage.windowAll : t.usage.windowDays(d)}
              </button>
            ))}
          </div>
          <Button
            variant="outline"
            size="sm"
            onClick={() => queryClient.invalidateQueries({ queryKey: ['usage'] })}
          >
            <RefreshCw className="mr-1.5 h-3.5 w-3.5" aria-hidden />
            {t.usage.refresh}
          </Button>
        </div>
      </header>

      {coverage.isLoading && <p className="text-sm text-muted-foreground">{t.common.loading}</p>}
      {coverage.isError && (
        <p className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {t.common.loadFailed((coverage.error as Error)?.message ?? '')}
        </p>
      )}

      {coverage.data && (
        <>
          {/* ① First block of the first screen — not collapsible, not closable */}
          <CoverageMatrix report={coverage.data} onFocusPath={setDrawerPath} />

          {/* ② Directly below the matrix, expanded by default */}
          <UnattributedDrawer
            report={coverage.data}
            samples={samples.data}
            path={drawerPath}
            onPathChange={setDrawerPath}
          />

          {/* ③ Attributed detail */}
          <AttributionDrill days={days} taskCoveragePct={taskPct} onProbe={setProbeAgentId} />

          {/* ④ Single measurement */}
          <SingleProbeCard agentId={probeAgentId} onAgentIdChange={setProbeAgentId} />

          <p className="text-right text-[11px] text-muted-foreground">
            {t.usage.generatedAt(formatDateTimeSystemLocale(coverage.data.generated_at))}
          </p>
        </>
      )}

      {/* ⑤ Permanent footer — must be present even when fetching fails; people reading numbers will not go dig through docs */}
      <MetricLegend />
    </div>
  );
}
