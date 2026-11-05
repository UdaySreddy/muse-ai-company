import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { CheckCircle2, XCircle, Clock, Play, RefreshCw, Plus, Loader2, ChevronRight } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table';
import type { ShallowBatch } from '@/api/ecosystem';
import {
  useShallowBatches,
  useCreateShallowBatch,
  useApproveBatch,
  useCancelBatch,
} from '@/api/ecosystem';
import { formatDateTime } from '@/lib/datetime';

function formatDatetime(iso: string | null) {
  if (!iso) return '—';
  return formatDateTime(iso, { hour12: false });
}

function StatusBadge({ status }: { status: string }) {
  switch (status) {
    case 'pending_approval':
      return <Badge variant="destructive" className="text-[10px]">Pending Approval</Badge>;
    case 'approved':
      return <Badge variant="outline" className="text-[10px] border-blue-400 text-blue-600">Approved</Badge>;
    case 'running':
      return <Badge variant="outline" className="text-[10px] border-orange-400 text-orange-600">Running</Badge>;
    case 'completed':
      return <Badge variant="outline" className="text-[10px] border-green-400 text-green-600">Completed</Badge>;
    case 'cancelled':
      return <Badge variant="secondary" className="text-[10px]">Cancelled</Badge>;
    default:
      return <Badge variant="outline" className="text-[10px]">{status}</Badge>;
  }
}

function StatusIcon({ status }: { status: string }) {
  if (status === 'completed') return <CheckCircle2 className="w-4 h-4 text-green-500" />;
  if (status === 'cancelled') return <XCircle className="w-4 h-4 text-muted-foreground" />;
  if (status === 'running') return <Play className="w-4 h-4 text-orange-500 animate-pulse" />;
  if (status === 'pending_approval') return <Clock className="w-4 h-4 text-red-500" />;
  return <RefreshCw className="w-4 h-4 text-blue-500" />;
}

function BatchRow({ batch, onApprove, onCancel }: {
  batch: ShallowBatch;
  onApprove: (id: string) => void;
  onCancel: (id: string) => void;
}) {
  const navigate = useNavigate();
  const canApprove = batch.status === 'pending_approval';
  const canCancel = batch.status === 'pending_approval' || batch.status === 'running';

  return (
    <TableRow
      className="cursor-pointer hover:bg-muted/30"
      onClick={() => navigate(`/ecosystem/batches/${batch.id}`)}
    >
      <TableCell className="w-8">
        <StatusIcon status={batch.status} />
      </TableCell>
      <TableCell className="font-mono text-xs text-muted-foreground w-24">
        {batch.id.slice(0, 8)}
      </TableCell>
      <TableCell>
        <span className="text-xs text-muted-foreground">{batch.triggered_by}</span>
        {batch.trigger_reason && (
          <p className="text-[10px] text-muted-foreground/60 truncate max-w-[180px]">{batch.trigger_reason}</p>
        )}
      </TableCell>
      <TableCell className="text-center font-medium">{batch.candidates_count}</TableCell>
      <TableCell><StatusBadge status={batch.status} /></TableCell>
      <TableCell className="text-xs text-muted-foreground">{formatDatetime(batch.created_at)}</TableCell>
      <TableCell onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center gap-1">
          {canApprove && (
            <Button
              size="sm"
              variant="default"
              className="h-7 text-xs"
              onClick={() => onApprove(batch.id)}
            >
              Approve
            </Button>
          )}
          {canCancel && (
            <Button
              size="sm"
              variant="ghost"
              className="h-7 text-xs text-muted-foreground"
              onClick={() => onCancel(batch.id)}
            >
              Cancel
            </Button>
          )}
          <ChevronRight className="w-4 h-4 text-muted-foreground ml-1" />
        </div>
      </TableCell>
    </TableRow>
  );
}

/**
 * Shallow-scan batch list page — lists all batches, highlights the pending_approval state, and supports approve/cancel actions.
 * Route: /ecosystem/batches
 */
export function EcosystemBatchesPage() {
  const { data, isLoading, refetch } = useShallowBatches(50);
  const createBatch = useCreateShallowBatch();
  const approveBatch = useApproveBatch();
  const cancelBatch = useCancelBatch();
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const batches = data?.batches ?? [];
  const pendingCount = batches.filter((b) => b.status === 'pending_approval').length;

  const handleCreate = () => {
    createBatch.mutate(
      { triggered_by: 'user', trigger_reason: 'Manually triggered shallow-scan batch' },
      {
        onSuccess: (res) => {
          setActionMsg(res.message);
          setTimeout(() => setActionMsg(null), 4000);
        },
        onError: () => setActionMsg('Failed to create the batch; check the backend logs'),
      },
    );
  };

  const handleApprove = (id: string) => {
    approveBatch.mutate(
      { batchId: id, approvedBy: 'user' },
      {
        onSuccess: (res) => {
          // worker.tick() only enqueues the shallow-scan tasks — the real deep-review summaries still require an active CC session to claim
          // the dispatch intent before they execute; the backend message is surfaced here together with this honest note (user decision, 2026-07-14).
          setActionMsg(`${res.message}; Deep reviews execute only once an active CC session claims the dispatch intent.`);
          setTimeout(() => setActionMsg(null), 6000);
        },
      },
    );
  };

  const handleCancel = (id: string) => {
    cancelBatch.mutate(id, {
      onSuccess: () => {
        setActionMsg('Batch cancelled');
        setTimeout(() => setActionMsg(null), 3000);
      },
    });
  };

  return (
    <div className="p-6 space-y-4 max-w-5xl mx-auto">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Shallow Scan Batches</h1>
          <p className="text-sm text-muted-foreground mt-1">
            Manage shallow-scan candidate repo batches — dispatch triggers only after a created batch is approved
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Button variant="outline" size="sm" onClick={() => refetch()} disabled={isLoading}>
            <RefreshCw className={`w-4 h-4 mr-1 ${isLoading ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
          <Button size="sm" onClick={handleCreate} disabled={createBatch.isPending}>
            {createBatch.isPending ? (
              <Loader2 className="w-4 h-4 mr-1 animate-spin" />
            ) : (
              <Plus className="w-4 h-4 mr-1" />
            )}
            New Batch
          </Button>
        </div>
      </div>

      {pendingCount > 0 && (
        <div className="flex items-center gap-2 bg-red-50 dark:bg-red-950/30 border border-red-200 dark:border-red-800 rounded-md px-4 py-2 text-sm text-red-700 dark:text-red-400">
          <Clock className="w-4 h-4 flex-shrink-0" />
          <span>
            <strong>{pendingCount}</strong> batches awaiting approval — click a row to open its detail page and approve
          </span>
        </div>
      )}

      {actionMsg && (
        <div className="bg-green-50 dark:bg-green-950/30 border border-green-200 dark:border-green-800 rounded-md px-4 py-2 text-sm text-green-700 dark:text-green-400">
          {actionMsg}
        </div>
      )}

      <Card>
        <CardHeader className="pb-2">
          <CardTitle className="text-base">
            Batch History
            {batches.length > 0 && (
              <span className="ml-2 text-sm text-muted-foreground font-normal">
                {batches.length} in total
              </span>
            )}
          </CardTitle>
        </CardHeader>
        <CardContent className="p-0">
          {isLoading ? (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="w-5 h-5 animate-spin mr-2" />
              Loading...
            </div>
          ) : batches.length === 0 ? (
            <div className="text-center py-12 text-muted-foreground text-sm">
              No batch records yet. Click "New Batch" to create the first batch
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead className="w-8"></TableHead>
                  <TableHead>ID</TableHead>
                  <TableHead>Trigger Source</TableHead>
                  <TableHead className="text-center">Candidate Repos</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Created At</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {batches.map((b) => (
                  <BatchRow
                    key={b.id}
                    batch={b}
                    onApprove={handleApprove}
                    onCancel={handleCancel}
                  />
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
