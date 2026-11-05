import { useState } from 'react';
import { Copy, Crosshair } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { MetricBadge } from '@/components/shared/MetricBadge';
import { formatTokenCount } from '@/components/usage/AttributionCard';
import { cn } from '@/lib/utils';
import { useT } from '@/i18n';
import { useSingleProbe } from '@/api/usage';

// ─────────────────────────────────────────────────────────────────────────────
// ④ Single-measurement card (dedicated channel for external examples) — design §5.2 ④.
//
// This card is **explicitly exempt from the coverage gate**: it verifies one measurement, not a full ledger; the two chains are decoupled.
// But the price of the exemption must be enforced — data may only come from `/api/usage/probe` (live-parsing one
// transcript); taking numbers from the aggregate view is forbidden. Otherwise the exemption becomes a back door around the gate: a number with no denominator
// could reach the page just by wearing the "single measurement" label.
//
// So this component imports exactly one data source, useSingleProbe. To verify the discipline, read the imports.
// ─────────────────────────────────────────────────────────────────────────────

function fmtDuration(ms: number | null): string {
  if (ms == null) return '—';
  if (ms < 1000) return `${ms}ms`;
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return s < 3600 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

export interface SingleProbeCardProps {
  agentId: string;
  onAgentIdChange: (id: string) => void;
}

export function SingleProbeCard({ agentId, onAgentIdChange }: SingleProbeCardProps) {
  const t = useT();
  const [draft, setDraft] = useState('');
  const probe = useSingleProbe(agentId);
  const data = probe.data;

  const layers = data
    ? ([
        { key: t.usage.layerOutput, value: data.output_tokens, primary: true },
        { key: t.usage.layerInput, value: data.input_tokens, primary: false },
        { key: t.usage.layerCacheCreation, value: data.cache_creation_tokens, primary: false },
        { key: t.usage.layerCacheRead, value: data.cache_read_tokens, primary: false },
      ] as const)
    : [];

  const copyable = data
    ? [
        `${data.agent_name} (${data.agent_id})`,
        `${t.usage.probeModel}: ${data.model || '—'}`,
        `${t.usage.probeDuration}: ${fmtDuration(data.duration_ms)}`,
        `${t.usage.probeApiCalls}: ${data.api_calls}`,
        `output_tokens=${data.output_tokens} input_tokens=${data.input_tokens} ` +
          `cache_creation_tokens=${data.cache_creation_tokens} cache_read_tokens=${data.cache_read_tokens} ` +
          `[${data.metric}]`,
        t.usage.probeStamp,
      ].join('\n')
    : '';

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex flex-wrap items-center gap-2">
          {t.usage.probeTitle}
          {/* Fixed stamp: this line does not change with the data and cannot be dismissed */}
          <span className="rounded border border-primary/40 bg-primary/10 px-1.5 py-0.5 text-[11px] font-normal">
            {t.usage.probeStamp}
          </span>
        </CardTitle>
        <CardDescription>{t.usage.probeExempt}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <form
          className="flex flex-wrap gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            onAgentIdChange(draft.trim());
          }}
        >
          <Input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder={t.usage.probePlaceholder}
            aria-label={t.usage.probePlaceholder}
            className="max-w-md flex-1 font-mono text-xs"
          />
          <Button type="submit" size="sm" disabled={!draft.trim()}>
            <Crosshair className="mr-1 h-3.5 w-3.5" aria-hidden />
            {t.usage.probeRun}
          </Button>
        </form>

        {!agentId && <p className="text-sm text-muted-foreground">{t.usage.probeEmpty}</p>}
        {agentId && probe.isLoading && <p className="text-sm text-muted-foreground">{t.common.loading}</p>}
        {agentId && probe.isError && (
          <p className="rounded border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            {t.common.loadFailed((probe.error as Error)?.message ?? '')}
          </p>
        )}

        {data && (
          <div className="space-y-3 rounded-lg border p-3">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="font-medium">{data.agent_name || data.agent_id}</span>
                  <MetricBadge metric={data.metric} compact />
                </div>
                <code className="text-[10px] text-muted-foreground">{data.agent_id}</code>
              </div>
              <Button
                variant="outline"
                size="sm"
                className="h-7 px-2 text-xs"
                onClick={() => navigator.clipboard?.writeText(copyable)}
              >
                <Copy className="mr-1 h-3 w-3" aria-hidden />
                {t.usage.copyCard}
              </Button>
            </div>

            <div className="grid grid-cols-2 gap-1.5 sm:grid-cols-4">
              {layers.map((layer) => (
                <div
                  key={layer.key}
                  className={cn(
                    'rounded border px-2 py-1.5',
                    layer.primary ? 'border-primary/40 bg-primary/5' : 'bg-muted/30',
                  )}
                >
                  <div className="truncate font-mono text-[10px] text-muted-foreground">{layer.key}</div>
                  <div
                    className={cn('tabular-nums', layer.primary ? 'text-base font-semibold' : 'text-sm')}
                    title={layer.value.toLocaleString()}
                  >
                    {formatTokenCount(layer.value)}
                  </div>
                </div>
              ))}
            </div>
            {/* The same deliberate gap as the attribution cards: four layers split, no fifth cell */}
            <p className="text-[10px] text-muted-foreground">{t.usage.noTotalField}</p>

            <div className="grid gap-2 text-xs sm:grid-cols-3">
              <div>
                <div className="text-[10px] text-muted-foreground">{t.usage.probeModel}</div>
                <div className="font-mono" title={t.usage.probeModelObserved}>
                  {data.model || '—'}
                </div>
              </div>
              <div>
                <div className="text-[10px] text-muted-foreground">{t.usage.probeDuration}</div>
                <div className="tabular-nums">{fmtDuration(data.duration_ms)}</div>
              </div>
              <div>
                <div className="text-[10px] text-muted-foreground">{t.usage.probeApiCalls}</div>
                <div className="tabular-nums">{data.api_calls.toLocaleString()}</div>
              </div>
            </div>

            <div className="space-y-2">
              <div>
                <div className="text-[10px] text-muted-foreground">{t.usage.probeInput}</div>
                <pre className="mt-0.5 max-h-40 overflow-auto rounded bg-muted/50 px-2 py-1.5 text-[11px] whitespace-pre-wrap">
                  {data.prompt_excerpt || '—'}
                </pre>
              </div>
              <div>
                <div className="text-[10px] text-muted-foreground">{t.usage.probeOutput}</div>
                <pre className="mt-0.5 max-h-40 overflow-auto rounded bg-muted/50 px-2 py-1.5 text-[11px] whitespace-pre-wrap">
                  {data.result_excerpt || '—'}
                </pre>
              </div>
            </div>

            <div className="truncate font-mono text-[10px] text-muted-foreground" title={data.transcript_path}>
              {t.usage.probeTranscript}: {data.transcript_path}
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
