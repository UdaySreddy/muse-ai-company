import type { WorkflowAgentState } from '@/api/workflows';

/**
 * Terminal-state semantics for workflow team members (task 8306664f): after a workflow-subagent completes a phase,
 * the backend agents.status falls back to 'offline' — that is the true state at the data layer (the CC sub-agent process
 * really has exited), but it misleads users: "4 done + 1 running" gets presented as "4 of 5 offline".
 *
 * Trust the workflow observability layer's state first (the authoritative source in design workflow-observability §2.2):
 * done -> completed, running -> busy, queued -> waiting; when no projection data is available (no wf_id attached, or the
 * agent is not in the observability table), fall back to a role-based heuristic — the workflow-subagent role only ever appears in
 * workflow teams (hook_translator.WORKFLOW_AGENT_TYPE); session team members use custom
 * role names and never trigger this rule, so offline is still shown truthfully as offline (user decision, 2026-07-14).
 */
export function resolveWorkflowAgentStatus(
  agent: { status: string; role: string },
  wfState: WorkflowAgentState | undefined,
): string {
  if (wfState === 'done') return 'done';
  if (wfState === 'running') return 'busy';
  if (wfState === 'queued') return 'waiting';
  const raw = agent.status.toLowerCase();
  if (raw === 'offline' && agent.role === 'workflow-subagent') return 'done';
  return raw;
}
