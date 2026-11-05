export interface Team {
  id: string;
  name: string;
  mode: string;
  project_id?: string | null;
  leader_agent_id?: string | null;
  status: 'active' | 'completed' | 'archived';
  summary?: string;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
  completed_at?: string | null;
  // The CC process that owns this session container team; derived by the backend, not persisted. Multiple container teams of the same process are grouped by it.
  // Null when it cannot be established (historic sessions are not found in CC's process registry); always null for non-container teams.
  cc_pid?: number | null;
}

export interface Agent {
  id: string;
  team_id: string;
  name: string;
  role: string;
  system_prompt: string;
  model: string;
  status: string;
  config: Record<string, unknown>;
  created_at: string;
  source?: 'api' | 'hook';       // Source marker
  session_id?: string | null;     // CC session ID
  cc_tool_use_id?: string | null; // CC-internal agent ID
  current_task?: string | null;   // Description of the task currently being executed
  last_active_at?: string | null; // Last active time
  // Sub-agent context watermark ledger (docs/agent-reuse-design.md section 4).
  // Populated on SubagentStop; agents with no capture yet leave these null.
  ctx_tokens?: number | null;
  ctx_window?: number | null;
  ctx_pct?: number | null;
  transcript_path?: string | null;
  ctx_measured_at?: string | null;
  reuse_domain?: string | null;
}

export interface Task {
  id: string;
  team_id: string;
  title: string;
  description: string;
  status: string;
  result: string | null;
  priority: 'critical' | 'high' | 'medium' | 'low';
  horizon: 'short' | 'mid' | 'long';
  tags: string[];
  score?: number;
  assigned_to?: string | null;
  team_name?: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export interface TaskWallResponse {
  wall: {
    short: Task[];
    mid: Task[];
    long: Task[];
  };
  completed?: Task[];
  stats: {
    total: number;
    by_priority: Record<string, number>;
    by_status: Record<string, number>;
    avg_score: number;
    completed_count?: number;
  };
}

export interface Event {
  id: string;
  type: string;
  source: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface Memory {
  id: string;
  scope: 'global' | 'team' | 'agent' | 'user';
  scope_id: string;
  content: string;
  metadata: Record<string, unknown>;
  created_at: string;
  accessed_at: string | null;
}

export interface APIResponse<T> {
  data: T;
  message: string;
  /** Some write endpoints (e.g. task run/decompose) additionally return a line of operating guidance,
   *  honestly stating "this only enqueues; a CC session must pick it up" — the frontend should surface it rather than discard it. */
  _hint?: string;
}

export interface APIListResponse<T> {
  data: T[];
  total: number;
  message: string;
}

export interface TeamStatus {
  team: Team;
  agents: Agent[];
  active_tasks: Task[];
  completed_tasks: number;
  total_tasks: number;
}

export interface Meeting {
  id: string;
  team_id: string;
  topic: string;
  status: 'active' | 'concluded';
  participants: string[];
  created_at: string;
  concluded_at: string | null;
}

export interface MeetingMessage {
  id: string;
  meeting_id: string;
  agent_id: string;
  agent_name: string;
  content: string;
  round_number: number;
  timestamp: string;
}

export interface WSEvent {
  type: string;
  source: string;
  data: Record<string, unknown>;
  timestamp: string;
}

export interface Project {
  id: string;
  name: string;
  root_path: string;
  description: string;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface Phase {
  id: string;
  project_id: string;
  name: string;
  description: string;
  status: 'planning' | 'active' | 'completed' | 'archived';
  order: number;
  config: Record<string, unknown>;
  created_at: string;
  updated_at: string;
}

export interface AgentActivity {
  id: string;
  agent_id: string;
  agent_name?: string;
  session_id: string;
  tool_name: string;
  input_summary: string;
  output_summary: string;
  status: 'running' | 'completed' | 'error';
  duration_ms: number | null;
  timestamp: string;
  error?: string;
}
