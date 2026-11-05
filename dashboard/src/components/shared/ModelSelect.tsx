import { useState } from 'react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Input } from '@/components/ui/input';
import { useAvailableModels } from '@/api/models';
import { useT } from '@/i18n';

/**
 * Model selector — the dropdown list is auto-discovered from the file source of truth (models actually used on this machine),
 * replacing all hardcoded stale lists (claude-opus-4-7, etc.). Supports free-text input as a fallback
 * (new models / models never used); full names shown directly with no alias mapping.
 */
export function ModelSelect({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const t = useT();
  const ph = placeholder ?? t.modelSelect.placeholder;
  const { data } = useAvailableModels();
  const models = (data?.data ?? []).filter((m) => !m.alias);
  const [custom, setCustom] = useState(false);
  const inList = models.some((m) => m.model === value);
  // When the current value is not in the scanned list (e.g. an alias / new model in settings.json), it is shown as an extra item
  // at the top of the dropdown — it must never degrade into a plain input box because of this (user feedback, tested 2026-07-10).
  const options = value && !inList
    ? [{ model: value, file_count: 0, last_seen_ts: 0, alias: false }, ...models]
    : models;

  if (custom || models.length === 0) {
    return (
      <div className="flex gap-2">
        <Input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={ph}
          className="flex-1"
        />
        {models.length > 0 && (
          <button
            type="button"
            className="text-xs text-muted-foreground hover:text-foreground whitespace-nowrap"
            onClick={() => setCustom(false)}
          >
            {t.modelSelect.pickFromList}
          </button>
        )}
      </div>
    );
  }

  return (
    <div className="flex gap-2">
      <Select value={value} onValueChange={(v) => v && onChange(v)}>
        <SelectTrigger className="flex-1">
          <SelectValue placeholder={ph} />
        </SelectTrigger>
        <SelectContent>
          {options.map((m) => (
            <SelectItem key={m.model} value={m.model}>
              {m.model}
              {m.file_count > 0 && (
                <span className="ml-2 text-[10px] text-muted-foreground">
                  {t.modelSelect.usedInSessions(m.file_count)}
                </span>
              )}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      <button
        type="button"
        className="text-xs text-muted-foreground hover:text-foreground whitespace-nowrap"
        onClick={() => setCustom(true)}
      >
        {t.modelSelect.freeInput}
      </button>
    </div>
  );
}
