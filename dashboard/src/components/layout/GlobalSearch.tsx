import { useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, FileText, ClipboardList, StickyNote } from 'lucide-react';
import { apiFetch } from '@/api/client';
import { useT } from '@/i18n';

interface SearchHit {
  kind: 'task' | 'task_memo' | 'report' | (string & {});
  id: string;
  title: string;
  snippet: string;
  score: number;
  project_id: string;
}

const KIND_ICON: Record<string, typeof FileText> = {
  task: ClipboardList,
  task_memo: StickyNote,
  report: FileText,
};

/** Knowledge layer P1b — global unified search box (three-arm RRF: BM25 native / citation graph / exact ID). */
export function GlobalSearch() {
  const t = useT();
  const [q, setQ] = useState('');
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const navigate = useNavigate();
  const boxRef = useRef<HTMLDivElement>(null);

  // Debounced query, 300ms
  useEffect(() => {
    if (!q.trim()) {
      setHits([]);
      setOpen(false);
      return;
    }
    const timer = setTimeout(async () => {
      setLoading(true);
      try {
        const res = await apiFetch<{ data: SearchHit[] }>(
          `/api/search?q=${encodeURIComponent(q.trim())}&limit=8`,
        );
        setHits(res.data ?? []);
        setOpen(true);
      } catch {
        setHits([]);
      } finally {
        setLoading(false);
      }
    }, 300);
    return () => clearTimeout(timer);
  }, [q]);

  // Close on outside click
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, []);

  const jump = (h: SearchHit) => {
    setOpen(false);
    setQ('');
    if (h.kind === 'report') navigate('/reports');
    else if (h.kind === 'task' || h.kind === 'task_memo') navigate('/tasks');
    else navigate('/');
  };

  return (
    <div ref={boxRef} className="relative ml-auto w-64 max-w-[40vw]">
      <div className="flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-sm bg-background">
        <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onFocus={() => hits.length > 0 && setOpen(true)}
          placeholder={t.search.placeholder}
          className="w-full bg-transparent outline-none placeholder:text-muted-foreground/60"
        />
        {loading && <span className="text-[10px] text-muted-foreground shrink-0">…</span>}
      </div>
      {open && hits.length > 0 && (
        <div className="absolute right-0 top-full z-50 mt-1 w-[26rem] max-w-[80vw] overflow-hidden rounded-lg border bg-popover shadow-lg">
          {hits.map((h) => {
            const Icon = KIND_ICON[h.kind] ?? FileText;
            return (
              <button
                key={`${h.kind}:${h.id}`}
                onClick={() => jump(h)}
                className="flex w-full items-start gap-2.5 border-b px-3 py-2.5 text-left last:border-0 hover:bg-accent/60"
              >
                <Icon className="mt-0.5 h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                <span className="min-w-0">
                  <span className="block truncate text-sm font-medium">{h.title}</span>
                  <span className="block truncate text-xs text-muted-foreground">
                    {h.snippet}
                  </span>
                </span>
              </button>
            );
          })}
        </div>
      )}
      {open && hits.length === 0 && !loading && q.trim() && (
        <div className="absolute right-0 top-full z-50 mt-1 w-64 rounded-lg border bg-popover px-3 py-2.5 text-sm text-muted-foreground shadow-lg">
          {t.search.noResults}
        </div>
      )}
    </div>
  );
}
