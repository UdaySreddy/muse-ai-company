/**
 * The single entry point for parsing and formatting server times.
 *
 * The backend uses UTC uniformly across the whole store (see docs/utc-unification-design.md); time strings in responses carry
 * a `+00:00` offset. The browser's `new Date(s)` handles strings **with an offset** completely correctly; the problem
 * is with **bare strings without an offset** — they are interpreted in the browser's local timezone, so a UTC
 * `2026-07-28T09:00:00` is read as local 09:00, a full 8 hours early in UTC+8.
 *
 * That is exactly why the ecosystem page once displayed times 8 hours early, and also
 * where the `parseAsUtc` patch in `DeepReviewSection` — which fixed only one component — came from. This module
 * promotes it to the **single site-wide parsing layer**: bare strings are always read as UTC (= the storage convention); strings with an offset are parsed as-is.
 *
 * Why bare strings are read as UTC instead of raising an error: the store still contains bare strings until the legacy migration completes, and browsers may hold
 * cached pre-change responses. Reading them as UTC is the only interpretation consistent with the storage convention, and it makes old and new strings yield the same answer under the same
 * function.
 *
 * Display always uses the browser's local timezone — transport speaks UTC, display speaks the reader's clock.
 */

/** Whether the string carries its own timezone info (ends with `Z` or `±HH:MM` / `±HHMM`). */
function hasOffset(s: string): boolean {
  return /([zZ]|[+-]\d{2}:?\d{2})$/.test(s.trim());
}

/**
 * Parse a server time string into a Date. Returns null when unparseable (including null/empty),
 * leaving the fallback to the caller — deliberately never returns an Invalid Date, which would silently propagate downstream.
 */
export function parseServerTime(value: string | null | undefined): Date | null {
  if (!value) return null;
  const s = String(value).trim();
  if (!s) return null;

  // With offset: the string states its own convention; hand it to the engine as-is.
  if (hasOffset(s)) {
    const d = new Date(s);
    return Number.isNaN(d.getTime()) ? null : d;
  }

  // Bare string: read as UTC. Also normalize "YYYY-MM-DD HH:MM:SS.ffffff" into ISO form
  // (space becomes T; Date only accepts 3-digit milliseconds, so extra microsecond digits are truncated).
  const iso = s.replace(' ', 'T').replace(/(\.\d{3})\d+$/, '$1');
  const d = new Date(`${iso}Z`);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** Parse into a millisecond timestamp for sorting / differences; returns NaN when unparseable. */
export function serverTimeMs(value: string | null | undefined): number {
  return parseServerTime(value)?.getTime() ?? NaN;
}

/** Millisecond difference between two server time strings (end − start); returns NaN if either is unparseable. */
export function serverTimeDiffMs(
  start: string | null | undefined,
  end: string | null | undefined,
): number {
  return serverTimeMs(end) - serverTimeMs(start);
}

const DEFAULT_LOCALE = 'en-US';

/** Date + time, in the browser's local timezone. When unparseable, echoes the input as-is to aid debugging. */
export function formatDateTime(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = {},
  fallback = '—',
): string {
  const d = parseServerTime(value);
  if (!d) return value ? String(value) : fallback;
  return d.toLocaleString(DEFAULT_LOCALE, options);
}

/** Date only. */
export function formatDate(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = {},
  fallback = '—',
): string {
  const d = parseServerTime(value);
  if (!d) return value ? String(value) : fallback;
  return d.toLocaleDateString(DEFAULT_LOCALE, options);
}

/** Time only (24-hour clock by default — a 12-hour clock would introduce ambiguity in logs/timelines). */
export function formatTime(
  value: string | null | undefined,
  options: Intl.DateTimeFormatOptions = { hour12: false },
  fallback = '—',
): string {
  const d = parseServerTime(value);
  if (!d) return value ? String(value) : fallback;
  return d.toLocaleTimeString(DEFAULT_LOCALE, options);
}

/**
 * The compact form most commonly used in tables/timelines: `2026/07/28 17:01`.
 * It exists as a preset because five components previously each copied the same options literal.
 */
export function formatDateTimeCompact(
  value: string | null | undefined,
  fallback = '—',
): string {
  return formatDateTime(
    value,
    {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    },
    fallback,
  );
}

/** Follows the system locale (does not force en-US); used at call sites that originally used `toLocaleString()`. */
export function formatDateTimeSystemLocale(
  value: string | null | undefined,
  fallback = '—',
): string {
  const d = parseServerTime(value);
  if (!d) return value ? String(value) : fallback;
  return d.toLocaleString();
}

/** Millisecond duration -> "12s" / "3m 5s" / "2h 7m". Negative or NaN returns the fallback. */
export function formatDurationMs(ms: number, fallback = '—'): string {
  if (!Number.isFinite(ms) || ms < 0) return fallback;
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ${sec % 60}s`;
  const hr = Math.floor(min / 60);
  return `${hr}h ${min % 60}m`;
}

/** Elapsed time between two server time strings. A missing end means "until now". */
export function formatElapsed(
  start: string | null | undefined,
  end: string | null | undefined,
  fallback = '—',
): string {
  const from = serverTimeMs(start);
  if (Number.isNaN(from)) return fallback;
  const to = end ? serverTimeMs(end) : Date.now();
  if (Number.isNaN(to)) return fallback;
  return formatDurationMs(to - from, fallback);
}

/** How long ago (milliseconds). Returns NaN when unparseable. */
export function ageMs(value: string | null | undefined): number {
  return Date.now() - serverTimeMs(value);
}
