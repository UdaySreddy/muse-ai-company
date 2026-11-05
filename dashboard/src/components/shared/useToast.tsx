import { useCallback, useRef, useState } from 'react';

/**
 * Lightweight toast hook: surfaces the honest hints already present in backend responses (e.g. message/_hint/next_action)
 * in the UI, instead of letting users assume "I clicked the button = the AI is already on it".
 *
 * Each page holds its own state (reusing SettingsPage's existing inline toast visual style);
 * no global Context is introduced — these hints are feedback for one-off in-page actions and need no cross-page sharing.
 */
export function useToast() {
  const [message, setMessage] = useState<string | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const showToast = useCallback((msg: string, durationMs = 4000) => {
    if (timerRef.current) clearTimeout(timerRef.current);
    setMessage(msg);
    timerRef.current = setTimeout(() => setMessage(null), durationMs);
  }, []);

  const toastNode = message ? (
    <div className="fixed top-4 right-4 z-50 max-w-sm rounded-lg border bg-background px-4 py-3 text-sm shadow-lg ring-1 ring-foreground/10 animate-in fade-in slide-in-from-top-2">
      {message}
    </div>
  ) : null;

  return { showToast, toastNode };
}
