import { useState } from 'react';
import { Link } from 'react-router-dom';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Eye, Trash2 } from 'lucide-react';
import { useTeams, useDeleteTeam, useTeamStatus } from '@/api/teams';
import { useWorkflow } from '@/api/workflows';
import type { Team } from '@/types';
import { useT } from '@/i18n';
import { formatDateTimeSystemLocale } from '@/lib/datetime';

// Badge for run teams auto-tracked by CC Workflow (ultracode). Clickable, jumping to the /workflows observability detail;
// workflow-session-* are session-level fallback teams (temporary grouping while the wf_id is late) and get no badge.
function WorkflowBadge({ team }: { team: Team }) {
  const t = useT();
  if (!team.name.startsWith('workflow-') || team.name.startsWith('workflow-session-')) {
    return null;
  }
  // Reverse-lookup of wf_id: prefer config.workflow_run_id; fall back to stripping the prefix from the team name workflow-<wf_id>.
  const wfId =
    (team.config?.workflow_run_id as string | undefined) ??
    team.name.replace(/^workflow-/, '');
  const badge = (
    <Badge
      variant="outline"
      className="border-violet-400 text-violet-600 text-[10px]"
      title={t.teams.workflowTitle}
    >
      {t.teams.workflowLabel}
    </Badge>
  );
  if (!wfId) return badge;
  return (
    <Link
      to={`/workflows/${encodeURIComponent(wfId)}`}
      className="inline-flex hover:opacity-80"
      title={t.teams.workflowRunHint}
    >
      {badge}
    </Link>
  );
}

// A workflow team's headline = the observability-layer run name (e.g. d5-ecosystem-axis-convergence);
// the wf number is demoted to a small, muted tracking mark — numbers are for tracking, not the main description (user requirement, 2026-07-06).
export function TeamDisplayName({ team }: { team: Team }) {
  const wfId =
    (team.config?.workflow_run_id as string | undefined) ??
    (team.name.startsWith('workflow-') && !team.name.startsWith('workflow-session-')
      ? team.name.replace(/^workflow-/, '')
      : undefined);
  const { data: run } = useWorkflow(wfId ?? '');
  if (!wfId) return <>{team.name}</>;
  return (
    <span className="inline-flex flex-col leading-tight">
      <span>{run?.name || team.name}</span>
      <span className="font-mono text-[10px] font-normal text-muted-foreground/60">
        {wfId}
      </span>
    </span>
  );
}

// One CC process may leave several container teams on the OS side: the session id CC stamps at team creation never updates, so when the process changes
// session (reopened after ending / resumed) it starts a new one, and the old completed one stays in the list. The backend resolves
// cc_pid to sort teams of one process together and badge the group; teams it cannot resolve (historic sessions not found in the process registry, and
// deliberately no cwd guessing) stay as standalone rows, unchanged.
type GroupedTeam = { team: Team; groupSize: number; indexInGroup: number };

export function groupByProcess(teams: Team[]): GroupedTeam[] {
  const byPid = new Map<number, Team[]>();
  for (const team of teams) {
    if (typeof team.cc_pid === 'number') {
      const siblings = byPid.get(team.cc_pid) ?? [];
      siblings.push(team);
      byPid.set(team.cc_pid, siblings);
    }
  }
  const out: GroupedTeam[] = [];
  const emitted = new Set<string>();
  for (const team of teams) {
    if (emitted.has(team.id)) continue;
    const siblings =
      typeof team.cc_pid === 'number' ? (byPid.get(team.cc_pid) ?? [team]) : [team];
    siblings.forEach((sibling, index) => {
      emitted.add(sibling.id);
      out.push({ team: sibling, groupSize: siblings.length, indexInGroup: index });
    });
  }
  return out;
}

function ProcessGroupBadge({ row }: { row: GroupedTeam }) {
  const t = useT();
  if (row.groupSize < 2 || typeof row.team.cc_pid !== 'number') return null;
  return (
    <Badge
      variant="outline"
      className="border-sky-400 text-sky-600 text-[10px]"
      title={t.teams.sameProcessTitle}
    >
      {row.indexInGroup === 0
        ? t.teams.sameProcessLabel(row.team.cc_pid, row.groupSize)
        : t.teams.sameProcessContinued}
    </Badge>
  );
}

function TeamAgentCount({ team }: { team: Team }) {
  const { data, isLoading } = useTeamStatus(team.id);
  if (isLoading) return <Skeleton className="h-4 w-8 inline-block" />;
  return <>{data?.data?.agents.length ?? 0}</>;
}

function TeamTaskCount({ team }: { team: Team }) {
  const { data, isLoading } = useTeamStatus(team.id);
  if (isLoading) return <Skeleton className="h-4 w-8 inline-block" />;
  return <>{data?.data?.total_tasks ?? 0}</>;
}

export function TeamsPage() {
  const t = useT();
  const { data, isLoading, error } = useTeams();
  const teams = data?.data ?? [];
  const deleteTeam = useDeleteTeam();

  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Team | null>(null);

  function handleDelete() {
    if (!deleteTarget) return;
    deleteTeam.mutate(deleteTarget.id, {
      onSuccess: () => {
        setDeleteOpen(false);
        setDeleteTarget(null);
      },
    });
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t.teams.title}</h1>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{t.teams.teamList}</CardTitle>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : error ? (
            <p className="text-sm text-destructive">
              {t.teams.loadFailed(error.message)}
            </p>
          ) : teams.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">
              {t.teams.noTeams}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>{t.teams.colName}</TableHead>
                  <TableHead>{t.teams.colMode}</TableHead>
                  <TableHead>{t.teams.colAgentCount}</TableHead>
                  <TableHead>{t.teams.colTaskCount}</TableHead>
                  <TableHead>{t.teams.colCreatedAt}</TableHead>
                  <TableHead className="text-right">{t.teams.colActions}</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {groupByProcess(teams).map((row) => (
                  <TableRow key={row.team.id}>
                    <TableCell className="font-medium">
                      <span
                        className={`inline-flex items-center gap-2 ${
                          row.indexInGroup > 0 ? 'pl-4' : ''
                        }`}
                      >
                        <TeamDisplayName team={row.team} />
                        <WorkflowBadge team={row.team} />
                        <ProcessGroupBadge row={row} />
                      </span>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary">{row.team.mode}</Badge>
                    </TableCell>
                    <TableCell>
                      <TeamAgentCount team={row.team} />
                    </TableCell>
                    <TableCell>
                      <TeamTaskCount team={row.team} />
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {formatDateTimeSystemLocale(row.team.created_at)}
                    </TableCell>
                    <TableCell className="text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="sm"
                          render={<Link to={`/projects/${row.team.id}`} />}
                        >
                          <Eye className="mr-1 h-3 w-3" />
                          {t.teams.viewDetail}
                        </Button>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => {
                            setDeleteTarget(row.team);
                            setDeleteOpen(true);
                          }}
                        >
                          <Trash2 className="mr-1 h-3 w-3 text-destructive" />
                          {t.teams.deleteTeam}
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Delete Confirmation Dialog */}
      <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{t.teams.confirmDelete}</DialogTitle>
            <DialogDescription>
              {t.teams.confirmDeleteDesc(deleteTarget?.name ?? '')}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteOpen(false)}>
              {t.common.cancel}
            </Button>
            <Button
              variant="destructive"
              onClick={handleDelete}
              disabled={deleteTeam.isPending}
            >
              {deleteTeam.isPending ? t.teams.deleting : t.teams.confirmDelete}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
