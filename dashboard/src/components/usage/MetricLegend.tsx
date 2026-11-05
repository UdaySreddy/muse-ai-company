import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { MetricBadge } from '@/components/shared/MetricBadge';
import { useT } from '@/i18n';
import type { MetricLabel } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// ⑤ Metric definitions (permanent footer) — design §5.2 ⑤.
//
// It lives on the page rather than in docs for a plain reason: people reading numbers will not go dig through documentation. The three metrics'
// definitions share one source with TOKEN_METRIC_SPECS in src/aiteam/types.py (that constant exists precisely so that
// "the same explanation is not written once each in the page, the docs and the comments, then drifts apart"); change one place, change both.
// ─────────────────────────────────────────────────────────────────────────────

export function MetricLegend() {
  const t = useT();

  const entries: { metric: Exclude<MetricLabel, ''>; name: string; who: string; what: string }[] = [
    {
      metric: 'usage_sum',
      name: t.usage.legendUsageSum,
      who: t.usage.legendUsageSumWho,
      what: t.usage.legendUsageSumWhat,
    },
    {
      metric: 'ctx_last',
      name: t.usage.legendCtxLast,
      who: t.usage.legendCtxLastWho,
      what: t.usage.legendCtxLastWhat,
    },
    {
      metric: 'ctx_watermark',
      name: t.usage.legendCtxWatermark,
      who: t.usage.legendCtxWatermarkWho,
      what: t.usage.legendCtxWatermarkWhat,
    },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle>{t.usage.legendTitle}</CardTitle>
        <CardDescription>{t.usage.legendLead}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <dl className="grid gap-3 md:grid-cols-3">
          {entries.map((entry) => (
            <div key={entry.metric} className="rounded border p-3">
              <dt className="flex flex-wrap items-center gap-2">
                <MetricBadge metric={entry.metric} />
                <span className="text-sm font-medium">{entry.name}</span>
              </dt>
              <dd className="mt-1 space-y-1">
                <p className="text-xs leading-relaxed">{entry.what}</p>
                <p className="font-mono text-[10px] break-all text-muted-foreground">{entry.who}</p>
              </dd>
            </div>
          ))}
        </dl>
        <div className="rounded border border-dashed p-3">
          <h3 className="text-sm font-medium">{t.usage.legendWhyNoSum}</h3>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{t.usage.legendWhyNoSumBody}</p>
        </div>
      </CardContent>
    </Card>
  );
}
