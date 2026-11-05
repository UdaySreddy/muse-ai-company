import { useEffect, useState } from 'react';
import { Save, Settings2, Loader2, CheckCircle2, AlertCircle, Tag as TagIcon } from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import {
  useEcosystemProjectSettings,
  useUpdateProjectSettings,
} from '@/api/ecosystem';
import { formatDateTime } from '@/lib/datetime';

interface EcosystemSettingsPanelProps {
  projectId: string;
}

interface FormState {
  min_stars: number;
  top_n: number;
  refresh_interval_days: number;
  auto_shallow_on_archive: boolean;
  focus_topics: string[];
  focus_languages: string[];
  shallow_concurrency: number;
  deep_concurrency: number;
  alert_max_new_per_scan: number;
}

const DEFAULTS: FormState = {
  min_stars: 1000,
  top_n: 200,
  refresh_interval_days: 7,
  auto_shallow_on_archive: true,
  focus_topics: [],
  focus_languages: [],
  shallow_concurrency: 5,
  deep_concurrency: 3,
  alert_max_new_per_scan: 50,
};

/**
 * Project-level ecosystem settings panel (v1.5.0-E §8.4 / decision 12.1).
 * Embedded into ProjectDetailPage as the "Ecosystem Settings" tab.
 */
export function EcosystemSettingsPanel({ projectId }: EcosystemSettingsPanelProps) {
  const { data, isLoading, error } = useEcosystemProjectSettings(projectId);
  const update = useUpdateProjectSettings(projectId);

  const [form, setForm] = useState<FormState>(DEFAULTS);
  const [topicInput, setTopicInput] = useState('');
  const [languageInput, setLanguageInput] = useState('');
  const [savedAt, setSavedAt] = useState<number | null>(null);

  // Populate the form after data loads (initializing the editable form when async data arrives; a legitimate effect-setState)
  useEffect(() => {
    if (data) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setForm({
        min_stars: data.min_stars,
        top_n: data.top_n,
        refresh_interval_days: data.refresh_interval_days,
        auto_shallow_on_archive: data.auto_shallow_on_archive,
        focus_topics: data.focus_topics ?? [],
        focus_languages: data.focus_languages ?? [],
        shallow_concurrency: data.shallow_concurrency,
        deep_concurrency: data.deep_concurrency,
        alert_max_new_per_scan: data.alert_max_new_per_scan ?? 50,
      });
    }
  }, [data]);

  const onSave = () => {
    update.mutate(form, {
      onSuccess: () => {
        setSavedAt(Date.now());
        setTimeout(() => setSavedAt(null), 3000);
      },
    });
  };

  const addTopic = () => {
    const v = topicInput.trim();
    if (!v || form.focus_topics.includes(v)) return;
    setForm({ ...form, focus_topics: [...form.focus_topics, v] });
    setTopicInput('');
  };

  const removeTopic = (t: string) => {
    setForm({ ...form, focus_topics: form.focus_topics.filter((x) => x !== t) });
  };

  const addLanguage = () => {
    const v = languageInput.trim();
    if (!v || form.focus_languages.includes(v)) return;
    setForm({ ...form, focus_languages: [...form.focus_languages, v] });
    setLanguageInput('');
  };

  const removeLanguage = (l: string) => {
    setForm({ ...form, focus_languages: form.focus_languages.filter((x) => x !== l) });
  };

  if (isLoading) {
    return (
      <div className="space-y-3">
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-10 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <Card>
        <CardContent className="p-4 flex items-start gap-2 text-destructive">
          <AlertCircle className="h-4 w-4 mt-0.5 shrink-0" aria-hidden="true" />
          <div className="text-sm">
            <p className="font-medium">Failed to Load Project Settings</p>
            <p className="text-xs mt-1 opacity-80">{error.message}</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base flex items-center gap-2">
          <Settings2 className="h-4 w-4" aria-hidden="true" />
          Ecosystem Settings
        </CardTitle>
        <p className="text-xs text-muted-foreground mt-1">
          Project-level ecosystem repo settings: thresholds, active-set size, refresh interval, watched topics/languages. Changes affect this project's scan/shallow-scan behavior.
        </p>
      </CardHeader>
      <CardContent className="space-y-5">
        {/* Numeric fields grid */}
        <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
          <div className="space-y-1">
            <Label htmlFor="min-stars">Archive Stars Threshold</Label>
            <Input
              id="min-stars"
              type="number"
              min={0}
              step={500}
              value={form.min_stars}
              onChange={(e) =>
                setForm({ ...form, min_stars: Math.max(0, Number(e.target.value) || 0) })
              }
            />
            <p className="text-[10px] text-muted-foreground">Repos with stars below this value are not archived</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="top-n">Active Set Size (top_n)</Label>
            <Input
              id="top-n"
              type="number"
              min={1}
              max={1000}
              step={10}
              value={form.top_n}
              onChange={(e) =>
                setForm({ ...form, top_n: Math.max(1, Number(e.target.value) || 1) })
              }
            />
            <p className="text-[10px] text-muted-foreground">The top N repos by stars enter the active set</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="refresh-interval">Auto-Check Interval (days)</Label>
            <Input
              id="refresh-interval"
              type="number"
              min={1}
              max={90}
              step={1}
              value={form.refresh_interval_days}
              onChange={(e) =>
                setForm({
                  ...form,
                  refresh_interval_days: Math.max(1, Number(e.target.value) || 1),
                })
              }
            />
            <p className="text-[10px] text-muted-foreground">
              Auto-check interval (every N days, cron triggers a new batch scan to detect new repos + updates to archived repos)
            </p>
            <p className="text-[10px] text-muted-foreground/70 mt-0.5">
              Note: this is the cron schedule interval, not an expiry threshold. Re-scanning old repos is decided by the GitHub repo's push time (pushed_at &gt; last_shallow_refreshed_at)
            </p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="shallow-conc">Shallow Scan Concurrency</Label>
            <Input
              id="shallow-conc"
              type="number"
              min={1}
              max={20}
              value={form.shallow_concurrency}
              onChange={(e) =>
                setForm({
                  ...form,
                  shallow_concurrency: Math.max(1, Number(e.target.value) || 1),
                })
              }
            />
            <p className="text-[10px] text-muted-foreground">Stage 0 worker concurrency (rate-limit tuning)</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="deep-conc">Deep Review Concurrency</Label>
            <Input
              id="deep-conc"
              type="number"
              min={1}
              max={10}
              value={form.deep_concurrency}
              onChange={(e) =>
                setForm({
                  ...form,
                  deep_concurrency: Math.max(1, Number(e.target.value) || 1),
                })
              }
            />
            <p className="text-[10px] text-muted-foreground">Stage 1+ batch dispatch concurrency</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="alert-max-new">Alert Threshold: Max New Repos per Scan</Label>
            <Input
              id="alert-max-new"
              type="number"
              min={1}
              max={10000}
              step={10}
              value={form.alert_max_new_per_scan}
              onChange={(e) =>
                setForm({
                  ...form,
                  alert_max_new_per_scan: Math.max(1, Number(e.target.value) || 1),
                })
              }
            />
            <p className="text-[10px] text-muted-foreground">When one scan adds more repos than this, an alert fires (results are not submitted)</p>
          </div>

          <div className="space-y-1">
            <Label htmlFor="auto-shallow">Auto Shallow Scan on Archive</Label>
            <div className="flex items-center gap-2 pt-2">
              <Switch
                id="auto-shallow"
                checked={form.auto_shallow_on_archive}
                onCheckedChange={(checked) =>
                  setForm({ ...form, auto_shallow_on_archive: checked })
                }
              />
              <span className="text-xs text-muted-foreground">
                {form.auto_shallow_on_archive ? 'Auto-dispatch Stage 0' : 'Off, Triggered On Demand Only'}
              </span>
            </div>
          </div>
        </div>

        {/* focus_topics */}
        <div className="space-y-1">
          <Label htmlFor="topic-input">Watched Topics (allowlist; empty = all topics)</Label>
          <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-background p-2">
            {form.focus_topics.map((t) => (
              <Badge
                key={t}
                variant="secondary"
                className="gap-1 cursor-pointer"
                onClick={() => removeTopic(t)}
                title="Click to remove"
              >
                <TagIcon className="h-3 w-3" aria-hidden="true" />
                {t} ×
              </Badge>
            ))}
            <Input
              id="topic-input"
              className="flex-1 min-w-[120px] border-0 shadow-none focus-visible:ring-0 p-0 h-7"
              placeholder={form.focus_topics.length === 0 ? 'e.g. claude-code / mcp / agent-framework' : 'Add more…'}
              value={topicInput}
              onChange={(e) => setTopicInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  addTopic();
                }
              }}
            />
          </div>
        </div>

        {/* focus_languages */}
        <div className="space-y-1">
          <Label htmlFor="language-input">Watched Languages (allowlist; empty = all languages)</Label>
          <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-background p-2">
            {form.focus_languages.map((l) => (
              <Badge
                key={l}
                variant="secondary"
                className="gap-1 cursor-pointer"
                onClick={() => removeLanguage(l)}
                title="Click to remove"
              >
                <TagIcon className="h-3 w-3" aria-hidden="true" />
                {l} ×
              </Badge>
            ))}
            <Input
              id="language-input"
              className="flex-1 min-w-[120px] border-0 shadow-none focus-visible:ring-0 p-0 h-7"
              placeholder={form.focus_languages.length === 0 ? 'e.g. Python / TypeScript / Rust' : 'Add more…'}
              value={languageInput}
              onChange={(e) => setLanguageInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault();
                  addLanguage();
                }
              }}
            />
          </div>
        </div>

        {/* Save bar */}
        <div className="flex items-center justify-between pt-2 border-t">
          <div className="text-xs text-muted-foreground">
            {data?.updated_at && (
              <>Last updated: {formatDateTime(data.updated_at)}</>
            )}
          </div>
          <div className="flex items-center gap-2">
            {savedAt && (
              <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
                <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
                Saved
              </span>
            )}
            <Button onClick={onSave} disabled={update.isPending}>
              {update.isPending ? (
                <>
                  <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden="true" />
                  Saving…
                </>
              ) : (
                <>
                  <Save className="mr-1 h-4 w-4" aria-hidden="true" />
                  Save Settings
                </>
              )}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
