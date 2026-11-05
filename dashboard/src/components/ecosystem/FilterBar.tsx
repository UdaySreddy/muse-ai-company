import { useMemo, useRef, useState, useEffect } from 'react';
import { Search, Star, Tag, X, ChevronDown, Check } from 'lucide-react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from '@/components/ui/select';
import type { EcosystemFilters, EcosystemFacetCounts } from '@/api/ecosystem';

interface FilterBarProps {
  /** Current filter criteria */
  filters: EcosystemFilters;
  /** Filter-change callback */
  onChange: (next: EcosystemFilters) => void;
  /** Hit count (for display) */
  totalCount?: number;
  /** Backend facet aggregation (when enabled, category filter options are followed by counts) */
  facetCounts?: EcosystemFacetCounts;
}

const STAR_OPTIONS: { value: number; label: string }[] = [
  { value: 0, label: 'Any Stars' },
  { value: 100, label: '≥ 100' },
  { value: 1000, label: '≥ 1k' },
  { value: 5000, label: '≥ 5k' },
  { value: 15000, label: '≥ 15k' },
  { value: 50000, label: '≥ 50k' },
];

const ALL = '__all__';

/**
 * List-page filter bar — keyword search + topics multi-select + star threshold + deep-review status.
 * v1.6.0: removed the "category" single-select (heuristic classification retired) in favor of GitHub topics multi-select filtering (client-side filter).
 * Single-column stack on mobile, horizontal layout on desktop.
 */
export function FilterBar({ filters, onChange, totalCount, facetCounts }: FilterBarProps) {
  const topicFacets = useMemo(() => facetCounts?.topics ?? {}, [facetCounts]);
  const update = (patch: Partial<EcosystemFilters>) => {
    onChange({ ...filters, ...patch });
  };

  const resetAll = () => {
    onChange({ limit: filters.limit ?? 200 });
  };

  // v1.6.0: defaults to the full set (including deleted, since they are very few); checking it switches to the "deleted only" view
  const onlyDeleted = filters.isDeleted === true;
  const selectedTopics = filters.topics ?? [];

  const hasActiveFilter = Boolean(
    filters.keyword ||
      filters.topic ||
      selectedTopics.length > 0 ||
      (filters.minStars && filters.minStars > 0) ||
      filters.stageStatus ||
      onlyDeleted,
  );

  // Sort topics by count descending and take the top 50 (avoid stuffing all 2425 into the dropdown)
  const sortedTopics = useMemo(() => {
    return Object.entries(topicFacets)
      .sort(([, a], [, b]) => b - a)
      .slice(0, 50);
  }, [topicFacets]);

  // Star trigger display text
  const minStarsValue = filters.minStars ?? 0;
  const starLabel = STAR_OPTIONS.find((o) => o.value === minStarsValue)?.label ?? 'Any Stars';

  // Research-stage trigger display text (v1.5.1: 3 mutually exclusive classes, semantics aligned with StatsBar)
  const STAGE_LABELS: Record<string, string> = {
    queued: 'Pending Shallow Scan',
    shallow_done: 'Shallow-Scanned, Not Researched',
    'architecture_done,debated,referenced,integrated': 'Researched',
  };
  const stageLabel = filters.stageStatus
    ? (STAGE_LABELS[filters.stageStatus] ?? filters.stageStatus)
     : 'All Repos';

  return (
    <div className="flex flex-col gap-3 p-4 border-b bg-muted/20">
      {/* First row: search box + hit count */}
      <div className="flex items-center gap-3">
        <div className="relative flex-1">
          <Search
            className="absolute left-2.5 top-1/2 -translate-y-1/2 h-4 w-4 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            placeholder="Search repo name / owner / description..."
            value={filters.keyword ?? ''}
            onChange={(e) => update({ keyword: e.target.value })}
            className="pl-9 h-9"
            aria-label="Search repos"
          />
        </div>
        {typeof totalCount === 'number' && (
          <div className="text-sm text-muted-foreground whitespace-nowrap">
            <span className="font-semibold text-foreground">{totalCount}</span> repos in total
          </div>
        )}
      </div>

      {/* Second row: dimension filters */}
      <div className="flex flex-wrap items-center gap-2">
        {/* v1.6.0: Topics multi-select filter (replaces the former relevance_category single-select) */}
        <TopicsMultiSelect
          options={sortedTopics}
          selected={selectedTopics}
          onChange={(next) => update({ topics: next.length > 0 ? next : undefined })}
        />

        {/* Star threshold */}
        <Select
          value={String(minStarsValue)}
          onValueChange={(v) => update({ minStars: Number(v) })}
        >
          <SelectTrigger className="h-8 min-w-[140px] text-sm" aria-label="Star threshold">
            <Star className="mr-1.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="truncate">{starLabel}</span>
          </SelectTrigger>
          <SelectContent>
            {STAR_OPTIONS.map((opt) => (
              <SelectItem key={opt.value} value={String(opt.value)}>
                {opt.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>

        {/* Research stage (v1.5.0 funnel; v1.5.1 added semantic notes)*/}
        <Select
          value={filters.stageStatus || ALL}
          onValueChange={(v) =>
            update({
              stageStatus: !v || v === ALL ? '' : v,
            })
          }
        >
          <SelectTrigger
            className="h-8 min-w-[170px] text-sm"
            aria-label="Research stage"
            title="Shallow scan = read README/CHANGELOG and summarize features and direction; Research = investigate code structure and design on demand, including relevance and adoption records"
          >
            <span className="truncate">{stageLabel}</span>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL}>All Repos</SelectItem>
            <SelectItem value="queued">
              <span className="flex flex-col">
                <span>Pending Shallow Scan</span>
                <span className="text-[10px] text-muted-foreground">README/CHANGELOG not yet read</span>
              </span>
            </SelectItem>
            <SelectItem value="shallow_done">
              <span className="flex flex-col">
                <span>Shallow-Scanned, Not Researched</span>
                <span className="text-[10px] text-muted-foreground">Features / design direction summarized</span>
              </span>
            </SelectItem>
            <SelectItem value="architecture_done,debated,referenced,integrated">
              <span className="flex flex-col">
                <span>Researched</span>
                <span className="text-[10px] text-muted-foreground">Investigated for a system change</span>
              </span>
            </SelectItem>
          </SelectContent>
        </Select>

        {/* v1.6.0: replaces the removed "Deleted" tab — toggles the "deleted only" view */}
        <label
          className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer select-none h-8 px-2 rounded-md hover:bg-muted/40"
          title="When checked, shows only repos detected as deleted / made private (the default view includes all repos)"
        >
          <input
            type="checkbox"
            checked={onlyDeleted}
            onChange={(e) =>
              // Checked -> isDeleted=true (deleted only); unchecked -> undefined (full set, default)
              update({ isDeleted: e.target.checked ? true : undefined })
            }
            className="h-3.5 w-3.5 rounded border-border accent-primary"
            aria-label="Deleted repos only"
          />
          Deleted Only
        </label>

        {/* TODO(Stage E v2): add has_deep_review / is_archived / tags multi-select filters */}

        {hasActiveFilter && (
          <Button
            variant="ghost"
            size="sm"
            onClick={resetAll}
            className="h-8 ml-auto text-muted-foreground"
            aria-label="Clear all filters"
          >
            <X className="mr-1 h-3.5 w-3.5" aria-hidden="true" />
            Clear
          </Button>
        )}
      </div>
    </div>
  );
}

/**
 * Topics multi-select dropdown — shows the topic list with counts and supports search filtering.
 * v1.6.0: replaces the former relevance_category single-select; uses facet_counts.topics as the option data source.
 */
function TopicsMultiSelect({
  options,
  selected,
  onChange,
}: {
  options: [string, number][];
  selected: string[];
  onChange: (next: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const rootRef = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    window.addEventListener('mousedown', onClick);
    return () => window.removeEventListener('mousedown', onClick);
  }, [open]);

  const filtered = useMemo(() => {
    if (!query.trim()) return options;
    const q = query.toLowerCase();
    return options.filter(([name]) => name.toLowerCase().includes(q));
  }, [options, query]);

  const toggle = (topic: string) => {
    const next = selected.includes(topic)
      ? selected.filter((t) => t !== topic)
      : [...selected, topic];
    onChange(next);
  };

  const triggerLabel =
    selected.length === 0
      ? 'All Topics'
      : selected.length === 1
        ? selected[0]
        : `${selected.length} Topics`;

  return (
    <div ref={rootRef} className="relative">
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="h-8 min-w-[170px] justify-between text-sm font-normal"
        onClick={() => setOpen((v) => !v)}
        aria-label="Filter Topics (multi-select)"
        aria-expanded={open}
      >
        <span className="flex items-center gap-1.5 truncate">
          <Tag className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
          <span className="truncate">{triggerLabel}</span>
        </span>
        <ChevronDown className="h-3.5 w-3.5 shrink-0 opacity-60" aria-hidden="true" />
      </Button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-72 rounded-md border bg-popover shadow-md">
          <div className="p-2 border-b">
            <Input
              autoFocus
              placeholder="Search topics..."
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              className="h-7 text-xs"
            />
            {selected.length > 0 && (
              <div className="flex items-center justify-between mt-1.5">
                <span className="text-[10px] text-muted-foreground">
                  {selected.length} selected
                </span>
                <button
                  type="button"
                  className="text-[10px] text-primary hover:underline"
                  onClick={() => onChange([])}
                >
                  Clear
                </button>
              </div>
            )}
          </div>
          <div className="max-h-72 overflow-y-auto py-1">
            {filtered.length === 0 ? (
              <p className="text-xs text-muted-foreground text-center py-3">No matching topics</p>
            ) : (
              filtered.map(([topic, count]) => {
                const isSelected = selected.includes(topic);
                return (
                  <button
                    key={topic}
                    type="button"
                    onClick={() => toggle(topic)}
                    className={`w-full flex items-center justify-between gap-2 px-2.5 py-1.5 text-xs text-left hover:bg-accent ${
                      isSelected ? 'bg-accent/50' : ''
                    }`}
                  >
                    <span className="flex items-center gap-1.5 min-w-0">
                      <span
                        className={`inline-flex h-3.5 w-3.5 items-center justify-center rounded border ${
                          isSelected
                            ? 'bg-primary border-primary text-primary-foreground'
                            : 'border-border'
                        }`}
                      >
                        {isSelected && <Check className="h-2.5 w-2.5" aria-hidden="true" />}
                      </span>
                      <span className="truncate">{topic}</span>
                    </span>
                    <span className="text-[10px] text-muted-foreground shrink-0">{count}</span>
                  </button>
                );
              })
            )}
          </div>
        </div>
      )}
    </div>
  );
}
