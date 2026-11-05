import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowLeft,
  Search,
  Sparkles,
  CheckCircle2,
  Loader2,
  Send,
  Filter,
  Tag as TagIcon,
} from 'lucide-react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  useEcosystemProfiles,
  useLifecycleRequestBatch,
  STAGE_STATUS_LABELS,
  stageBadgeClass,
} from '@/api/ecosystem';

/**
 * /ecosystem/research — candidate screening page (v1.5.0-E §8.3).
 *
 * Flow:
 *   1. The user enters a research goal + tag keywords
 *   2. The system finds candidates by tag (topic match) + shows shallow-scan summaries
 *   3. The user multi-selects candidates -> triggers a Stage 1 batch (deep_review_request_batch)
 *   4. Navigation/hints: subsequent finalists are driven by the meeting system
 */
export function EcosystemResearchPage() {
  const [researchGoal, setResearchGoal] = useState('');
  const [tagInput, setTagInput] = useState('');
  const [tags, setTags] = useState<string[]>([]);
  const [minStars, setMinStars] = useState(1000);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchResult, setBatchResult] = useState<{
    dispatched: number;
    repo_full_names: string[];
  } | null>(null);

  // Candidate retrieval: use tags as topic keywords (combined with OR)
  const filters = useMemo(
    () => ({
      keyword: tagInput.trim(),
      topic: tags[0] ?? '', // The backend supports a single topic only — multiple tags fall back to keyword
      minStars,
      limit: 50,
      isActive: true as boolean | null,
    }),
    [tagInput, tags, minStars],
  );
  const { data, isLoading } = useEcosystemProfiles(filters);
  const candidates = useMemo(() => data?.profiles ?? [], [data]);

  // Apply client-side secondary filtering to candidates with the tag list (topic ∩ tags)
  const filteredCandidates = useMemo(() => {
    if (tags.length === 0) return candidates;
    return candidates.filter((c) => {
      const repoTopics = c.topics?.map((t) => t.toLowerCase()) ?? [];
      const repoText = `${c.repo_full_name} ${c.description ?? ''} ${c.shallow_summary ?? ''}`.toLowerCase();
      return tags.every((t) => {
        const lower = t.toLowerCase();
        return repoTopics.includes(lower) || repoText.includes(lower);
      });
    });
  }, [candidates, tags]);

  const addTag = (t: string) => {
    const v = t.trim();
    if (!v || tags.includes(v)) return;
    setTags([...tags, v]);
    setTagInput('');
  };

  const removeTag = (t: string) => {
    setTags(tags.filter((x) => x !== t));
  };

  const toggleSelect = (id: string) => {
    const next = new Set(selectedIds);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setSelectedIds(next);
  };

  const batch = useLifecycleRequestBatch();

  const onDispatch = () => {
    if (selectedIds.size === 0) return;
    if (tags.length === 0) {
      alert('Add at least one tag for backend candidate screening');
      return;
    }
    batch.mutate(
      {
        tags,
        min_stars: minStars,
        limit: selectedIds.size,
        research_goal: researchGoal,
      },
      {
        onSuccess: (resp) => {
          setBatchResult({
            dispatched: resp.dispatched,
            repo_full_names: resp.intents.map((i) => i.repo_full_name),
          });
          setSelectedIds(new Set());
        },
      },
    );
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="max-w-5xl mx-auto p-6 space-y-4">
        <div>
          <Button variant="ghost" size="sm" nativeButton={false} render={<Link to="/ecosystem" />}>
            <ArrowLeft className="mr-1 h-4 w-4" aria-hidden="true" />
            Back to List
          </Button>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Search className="h-5 w-5 text-primary" aria-hidden="true" />
              Candidate Research — Stage 1 Dispatch
            </CardTitle>
            <p className="text-sm text-muted-foreground mt-1">
              Enter a research goal + tag keywords; the system screens candidates against the active set, and multi-selecting triggers Stage 1 architecture analysis.
            </p>
          </CardHeader>
          <CardContent className="space-y-4">
            {/* Research Goal */}
            <div className="space-y-1">
              <Label htmlFor="research-goal">Research Goal</Label>
              <Input
                id="research-goal"
                placeholder="e.g. Upgrade our memory system and compare against current mainstream ecosystem approaches"
                value={researchGoal}
                onChange={(e) => setResearchGoal(e.target.value)}
              />
            </div>

            {/* Tags */}
            <div className="space-y-1">
              <Label htmlFor="tag-input">Candidate Tags (press Enter to add)</Label>
              <div className="flex flex-wrap items-center gap-2 rounded-lg border bg-background p-2">
                {tags.map((t) => (
                  <Badge
                    key={t}
                    variant="secondary"
                    className="gap-1 cursor-pointer"
                    onClick={() => removeTag(t)}
                    title="Click to remove"
                  >
                    <TagIcon className="h-3 w-3" aria-hidden="true" />
                    {t} ×
                  </Badge>
                ))}
                <Input
                  id="tag-input"
                  className="flex-1 min-w-[120px] border-0 shadow-none focus-visible:ring-0 p-0 h-7"
                  placeholder={tags.length === 0 ? 'e.g. memory / mcp / agent' : 'Add more…'}
                  value={tagInput}
                  onChange={(e) => setTagInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') {
                      e.preventDefault();
                      addTag(tagInput);
                    }
                  }}
                />
              </div>
            </div>

            {/* min_stars */}
            <div className="space-y-1 max-w-xs">
              <Label htmlFor="min-stars">Minimum Stars</Label>
              <Input
                id="min-stars"
                type="number"
                min={0}
                step={500}
                value={minStars}
                onChange={(e) => setMinStars(Math.max(0, Number(e.target.value) || 0))}
              />
            </div>
          </CardContent>
        </Card>

        {/* Candidate List */}
        <Card>
          <CardHeader>
            <div className="flex items-center justify-between gap-3 flex-wrap">
              <CardTitle className="text-base flex items-center gap-2">
                <Filter className="h-4 w-4" aria-hidden="true" />
                Candidate List
                <Badge variant="outline" className="font-normal">
                  {filteredCandidates.length}
                </Badge>
                {selectedIds.size > 0 && (
                  <Badge className="bg-primary/10 text-primary border-primary/30">
                    {selectedIds.size} selected
                  </Badge>
                )}
              </CardTitle>
              <Button
                onClick={onDispatch}
                disabled={selectedIds.size === 0 || batch.isPending}
                size="sm"
              >
                {batch.isPending ? (
                  <>
                    <Loader2 className="mr-1 h-4 w-4 animate-spin" aria-hidden="true" />
                    Dispatching…
                  </>
                ) : (
                  <>
                    <Send className="mr-1 h-4 w-4" aria-hidden="true" />
                    Trigger Stage 1 ({selectedIds.size})
                  </>
                )}
              </Button>
            </div>
            {batchResult && (
              <div className="mt-2 rounded border border-emerald-500/40 bg-emerald-500/10 px-3 py-2">
                <div className="flex items-start gap-2">
                  <CheckCircle2 className="h-4 w-4 text-emerald-600 mt-0.5 shrink-0" aria-hidden="true" />
                  <div className="text-xs text-emerald-700 dark:text-emerald-300">
                    Dispatched {batchResult.dispatched} deep_review intents;
                    the Leader must actually dispatch a backend-architect via the Agent tool.
                    {batchResult.repo_full_names.length > 0 && (
                      <p className="mt-1">Candidate repos: {batchResult.repo_full_names.join(', ')}</p>
                    )}
                  </div>
                </div>
              </div>
            )}
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="space-y-2">
                {Array.from({ length: 4 }).map((_, i) => (
                  <Skeleton key={i} className="h-20 w-full" />
                ))}
              </div>
            ) : filteredCandidates.length === 0 ? (
              <div className="py-8 text-center text-sm text-muted-foreground">
                <Sparkles className="h-8 w-8 mx-auto opacity-30 mb-2" aria-hidden="true" />
                {tags.length === 0
                  ? 'Enter tags to start retrieving candidates'
                  : 'No active candidates under the current tag combination; lower min_stars or switch tags'}
              </div>
            ) : (
              <div className="space-y-2">
                {filteredCandidates.map((c) => {
                  const stage = c.stage_status ?? 'queued';
                  const checked = selectedIds.has(c.id);
                  return (
                    <button
                      type="button"
                      key={c.id}
                      onClick={() => toggleSelect(c.id)}
                      className={`w-full text-left rounded-lg border p-3 transition-colors ${
                        checked
                          ? 'border-primary/60 bg-primary/5'
                          : 'border-border bg-background hover:border-primary/30 hover:bg-accent/30'
                      }`}
                      aria-pressed={checked}
                    >
                      <div className="flex items-start gap-3">
                        <input
                          type="checkbox"
                          className="mt-1 h-4 w-4 accent-primary"
                          checked={checked}
                          onChange={() => toggleSelect(c.id)}
                          onClick={(e) => e.stopPropagation()}
                          aria-label={`Select ${c.repo_full_name}`}
                        />
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="font-medium text-sm">{c.repo_full_name}</span>
                            <span
                              className={`inline-flex items-center rounded border px-1.5 py-0.5 text-[10px] font-medium ${stageBadgeClass(stage)}`}
                            >
                              {STAGE_STATUS_LABELS[stage as keyof typeof STAGE_STATUS_LABELS] ?? stage}
                            </span>
                            <span className="text-xs text-muted-foreground">
                              ★ {c.stars.toLocaleString()}
                            </span>
                            {c.language && (
                              <span className="text-xs text-muted-foreground">{c.language}</span>
                            )}
                          </div>
                          <p className="text-xs text-muted-foreground mt-1 line-clamp-2">
                            {c.shallow_summary || c.one_line_summary || c.description || 'No description yet'}
                          </p>
                          {c.topics && c.topics.length > 0 && (
                            <div className="flex flex-wrap gap-1 mt-1.5">
                              {c.topics.slice(0, 5).map((t) => (
                                <Badge key={t} variant="outline" className="text-[10px] px-1 py-0 h-4">
                                  {t}
                                </Badge>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    </button>
                  );
                })}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
