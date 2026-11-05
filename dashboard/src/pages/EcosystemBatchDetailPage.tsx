import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  CheckCircle2, XCircle, ArrowLeft, Loader2, RefreshCw, Play, Star,
} from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import {
  useShallowBatch,
  useShallowBatchItems,
  useApproveBatch,
  useCancelBatch,
} from '@/api/ecosystem';
import { formatDateTime } from '@/lib/datetime';

function formatDatetime(iso: string | null) {
  if (!iso) return '—';
  return formatDateTime(iso, { hour12: false });
}

function StatusBadge({ status }: { status: string }) {
  const map: Record<string, string> = {
    pending_approval: 'Pending Approval',
    approved: 'Approved',
    running: 'Running',
    completed: 'Completed',
    cancelled: 'Cancelled',
  };
  const label = map[status] ?? status;
  if (status === 'pending_approval') return <Badge variant="destructive">{label}</Badge>;
  if (status === 'completed') return <Badge variant="outline" className="border-green-400 text-green-600">{label}</Badge>;
  if (status === 'cancelled') return <Badge variant="secondary">{label}</Badge>;
  if (status === 'running') return <Badge variant="outline" className="border-orange-400 text-orange-600">{label}</Badge>;
  return <Badge variant="outline">{label}</Badge>;
}

/**
 * Shallow-scan batch detail page — shows batch metadata + the candidate repo list, with approve/cancel actions.
 * Route: /ecosystem/batches/:batchId
 */
export function EcosystemBatchDetailPage() {
  const { batchId } = useParams<{ batchId: string }>();
  const navigate = useNavigate();
  const { data: batchData, isLoading: batchLoading, refetch } = useShallowBatch(batchId ?? null);
  const { data: itemsData, isLoading: itemsLoading } = useShallowBatchItems(batchId ?? null);
  const approveBatch = useApproveBatch();
  const cancelBatch = useCancelBatch();
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const batch = batchData?.batch;
  const items = itemsData?.items ?? [];
  const canApprove = batch?.status === 'pending_approval';
  const canCancel = batch?.status === 'pending_approval' || batch?.status === 'running';

  const handleApprove = () => {
    if (!batchId) return;
    approveBatch.mutate(
      { batchId, approvedBy: 'user' },
      {
        onSuccess: (res) => {
          // worker.tick() only enqueues the shallow-scan tasks — the real deep-review summaries still require an active CC session to claim
          // the dispatch intent before they execute; the backend message is surfaced here together with this honest note (user decision, 2026-07-14).
          setActionMsg(`${res.message}; Deep reviews execute only once an active CC session claims the dispatch intent.`);
          refetch();
          setTimeout(() => setActionMsg(null), 7000);
        },
      },
    );
  };

  const handleCancel = () => {
    if (!batchId) return;
    cancelBatch.mutate(batchId, {
      onSuccess: () => {
        setActionMsg('Batch cancelled');
        refetch();
        setTimeout(() => setActionMsg(null), 3000);
      },
    });
  };

  if (batchLoading) {
    return (
      <div className="flex items-center justify-center py-24 text-muted-foreground">
        <Loader2 className="w-5 h-5 animate-spin mr-2" />
        Loading...
      </div>
    );
  }

  if (!batch) {
    return (
      <div className="p-6 text-center text-muted-foreground">
        <p>This batch does not exist or has been deleted</p>
        <Button variant="link" onClick={() => navigate('/ecosystem/batches')}>
          Back to Batch List
        </Button>
      </div>
    );
  }

  return (
    <div className="p-6 space-y-4 max-w-5xl mx-auto">
      <div className="flex items-center gap-3">
        <Button variant="ghost" size="sm" onClick={() => navigate('/ecosystem/batches')}>
          <ArrowLeft className="w-4 h-4 mr-1" />
          Back to List
        </Button>
        <h1 className="text-xl font-bold">Batch Details</h1>
        <StatusBadge status={batch.status} />
        {batch.status === 'pending_approval' && (
          <Badge variant="destructive" className="animate-pulse ml-1">Approval Required</Badge>
        )}
      </div>

      {actionMsg && (
        <div className="bg-green-50 dark:bg-green-950/30 border border-green-200 dark:border-green-800 rounded-md px-4 py-2 text-sm text-green-700 dark:text-green-400">
          {actionMsg}
        </div>
      )}

      {/* Batch Metadata */}
      <Card>
        <CardHeader className="pb-2">
          <div className="flex items-center justify-between">
            <CardTitle className="text-base">Batch Info</CardTitle>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" onClick={() => refetch()}>
                <RefreshCw className="w-4 h-4 mr-1" />
                Refresh
              </Button>
              {canApprove && (
                <Button size="sm" onClick={handleApprove} disabled={approveBatch.isPending}>
                  {approveBatch.isPending ? (
                    <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                  ) : (
                    <CheckCircle2 className="w-4 h-4 mr-1" />
                  )}
                  Approve
                </Button>
              )}
              {batch.status === 'running' && (
                <Button size="sm" variant="outline" onClick={() => navigate('/ecosystem/shallow-queue')}>
                  <Play className="w-4 h-4 mr-1" />
                  View Progress
                </Button>
              )}
              {canCancel && (
                <Button size="sm" variant="ghost" className="text-muted-foreground" onClick={handleCancel} disabled={cancelBatch.isPending}>
                  <XCircle className="w-4 h-4 mr-1" />
                  Cancel Batch
                </Button>
              )}
            </div>
          </div>
        </CardHeader>
        <CardContent>
          <dl className="grid grid-cols-2 md:grid-cols-3 gap-4 text-sm">
            <div>
              <dt className="text-muted-foreground text-xs">Batch ID</dt>
              <dd className="font-mono text-xs mt-0.5">{batch.id}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Trigger Source</dt>
              <dd className="font-medium mt-0.5">{batch.triggered_by}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Candidate Repos</dt>
              <dd className="font-medium mt-0.5">{batch.candidates_count}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Created At</dt>
              <dd className="mt-0.5">{formatDatetime(batch.created_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Approved At</dt>
              <dd className="mt-0.5">{formatDatetime(batch.approved_at)}</dd>
            </div>
            <div>
              <dt className="text-muted-foreground text-xs">Completed At</dt>
              <dd className="mt-0.5">{formatDatetime(batch.completed_at)}</dd>
            </div>
            {batch.status !== 'pending_approval' && (
              <>
                <div>
                  <dt className="text-muted-foreground text-xs">Added Repos</dt>
                  <dd className="font-medium mt-0.5 text-green-600">{batch.new_repos_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-xs">Updated Repos</dt>
                  <dd className="font-medium mt-0.5">{batch.updated_repos_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-xs">Metadata Changes</dt>
                  <dd className="font-medium mt-0.5 text-amber-600">{batch.metadata_changed_count}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground text-xs">Failed</dt>
                  <dd className="font-medium mt-0.5 text-red-500">{batch.failed_count}</dd>
                </div>
              </>
            )}
            {batch.trigger_reason && (
              <div className="col-span-2 md:col-span-3">
                <dt className="text-muted-foreground text-xs">Trigger Reason</dt>
                <dd className="mt-0.5 text-sm">{batch.trigger_reason}</dd>
              </div>
            )}
          </dl>
        </CardContent>
      </Card>

      {/* Candidate Repo List */}
      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">
            Candidate Repo List
            <span className="ml-2 text-sm text-muted-foreground font-normal">
              {itemsData?.total ?? batch.candidates_count} repos in total
            </span>
            {batch.status === 'pending_approval' && (
              <Badge variant="outline" className="ml-2 text-[10px]">Preview — formally enqueued after approval</Badge>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {itemsLoading ? (
            <div className="flex items-center justify-center py-8 text-muted-foreground">
              <Loader2 className="w-4 h-4 animate-spin mr-2" />
              Loading candidate repos...
            </div>
          ) : items.length === 0 ? (
            <div className="text-center py-8 text-sm text-muted-foreground">
              {batch.status === 'pending_approval'
                ? 'The candidate repo list is empty (all repos are up to date)'
                : 'No data yet'}
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Repo</TableHead>
                  <TableHead className="text-center">Stars</TableHead>
                  <TableHead>Stage Status</TableHead>
                  <TableHead>Shallow Scan Summary Preview</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((item, i) => (
                  <TableRow key={item.repo_id ?? i}>
                    <TableCell>
                      <button
                        className="font-mono text-xs text-primary hover:underline text-left"
                        onClick={() => window.open(`https://github.com/${item.repo_full_name}`, '_blank')}
                      >
                        {item.repo_full_name}
                      </button>
                    </TableCell>
                    <TableCell className="text-center">
                      <span className="flex items-center justify-center gap-1 text-xs">
                        <Star className="w-3 h-3 text-amber-400" />
                        {item.stars.toLocaleString()}
                      </span>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-[10px]">
                        {item.stage_status}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground max-w-[300px] truncate">
                      {item.shallow_summary_excerpt || '—'}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
