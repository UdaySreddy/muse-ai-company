import { cn } from '@/lib/utils';
import { useT } from '@/i18n';
import type { MetricLabel } from '@/api/usage';

/**
 * Metric badge — every usage number on the page must carry one (design §4.4 / §5.3).
 *
 * This repo holds three orthogonal metrics that differ 5-25x in practice. A number detached from its metric is meaningless, and
 * "they all look like token counts, so let's compare them together" is exactly the class of incident this repo just had with timestamps.
 * Visually the three must be distinguishable at a glance: different background colors + different text, not tooltip-only.
 */
export interface MetricBadgeProps {
  metric: MetricLabel;
  className?: string;
  /** Compact mode: used when embedded next to numbers on existing pages; takes only a small corner badge */
  compact?: boolean;
}

const STYLES: Record<Exclude<MetricLabel, ''>, string> = {
  // usage_sum = consumption, the main attribution path; uses a neutral primary color
  usage_sum:
    'border-slate-300 bg-slate-100 text-slate-700 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200',
  // ctx_last = an instantaneous level snapshot, not consumption. Amber is a deliberate "look closely: this is not the same thing"
  ctx_last:
    'border-amber-300 bg-amber-100 text-amber-800 dark:border-amber-700 dark:bg-amber-950 dark:text-amber-200',
  // ctx_watermark = the reuse-governance domain, unrelated to usage attribution
  ctx_watermark:
    'border-sky-300 bg-sky-100 text-sky-800 dark:border-sky-700 dark:bg-sky-950 dark:text-sky-200',
};

export function MetricBadge({ metric, className, compact = false }: MetricBadgeProps) {
  const t = useT();
  if (!metric) return null;

  const copy: Record<Exclude<MetricLabel, ''>, { name: string; what: string }> = {
    usage_sum: { name: t.usage.legendUsageSum, what: t.usage.legendUsageSumWhat },
    ctx_last: { name: t.usage.legendCtxLast, what: t.usage.legendCtxLastWhat },
    ctx_watermark: { name: t.usage.legendCtxWatermark, what: t.usage.legendCtxWatermarkWhat },
  };
  const { name, what } = copy[metric];

  return (
    <span
      className={cn(
        'inline-flex w-fit shrink-0 items-center rounded border font-mono font-medium whitespace-nowrap',
        compact ? 'px-1 py-0 text-[10px] leading-4' : 'px-1.5 py-0.5 text-[11px] leading-4',
        STYLES[metric],
        className,
      )}
      title={`${name} — ${what}`}
      aria-label={`${name}: ${what}`}
    >
      {metric}
    </span>
  );
}
