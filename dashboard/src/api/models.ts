import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { apiFetch } from '@/api/client';

/** Model governance — available models are auto-discovered (file source of truth: models that have actually appeared in this machine's transcripts). */

export interface AvailableModel {
  model: string;
  file_count: number;
  last_seen_ts: number;
  /** Tier alias (e.g. "opus"); not included in the main default dropdown list */
  alias: boolean;
}

export function useAvailableModels() {
  return useQuery({
    queryKey: ['models', 'available'],
    queryFn: () => apiFetch<{ data: AvailableModel[] }>('/api/models/available'),
    staleTime: 60_000,
  });
}

export function useDefaultModel() {
  return useQuery({
    queryKey: ['models', 'default'],
    queryFn: () => apiFetch<{ data: { model: string } }>('/api/models/default'),
  });
}

export function useSetDefaultModel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (model: string) =>
      apiFetch<{ success: boolean; data: { ok: boolean; error?: string } }>(
        '/api/models/default',
        { method: 'PUT', body: JSON.stringify({ model }) },
      ),
    onSuccess: () => void qc.invalidateQueries({ queryKey: ['models'] }),
  });
}
