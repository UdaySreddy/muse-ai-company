import { useQuery } from '@tanstack/react-query';
import { apiFetch } from '@/api/client';

/** Source of truth for the backend version: /api/health returns aiteam.__version__ directly (never hardcode a version in the frontend). */
export function useApiVersion() {
  return useQuery({
    queryKey: ['health', 'version'],
    queryFn: () => apiFetch<{ status: string; version: string }>('/api/health'),
    staleTime: 5 * 60_000,
    refetchOnWindowFocus: false,
  });
}
