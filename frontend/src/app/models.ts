export interface TopoNode {
  id: string;
  name: string;
  kind: 'source' | 'equipment' | 'consumer' | 'junction';
  x: number;
  y: number;
  essential: boolean;
}

export interface Segment {
  id: string;
  source: string;
  target: string;
  direction: string;
  kind: 'main' | 'bypass' | 'branch';
  is_bypass: boolean;
  valve_id: string | null;
}

export interface Valve {
  id: string;
  name: string;
  segment_id: string;
  endpoints: string[];
  is_open: boolean;
  locked: boolean;
  operable: boolean;
  is_bypass: boolean;
  permit_enabled?: boolean;
}

export interface Topology {
  nodes: TopoNode[];
  segments: Segment[];
  valves: Valve[];
}

export interface Solution {
  close_valves: string[];
  size: number;
  alternative_rank: number;
  closes_bypass_valves: string[];
  supply_paths: Record<string, string[] | null>;
  uses_permit?: boolean;
  permit_valves_used?: string[];
  supply_uses_permit?: Record<string, boolean>;
}

export interface ResidualPath {
  nodes: string[];
  valves: (string | null)[];
  locked_valves_on_path: string[];
  uses_bypass: boolean;
}

export interface AppliedPermit {
  id: number;
  bypass_valve_ids: string[];
  status: string;
  purpose: string;
}

export interface IsolationResult {
  feasible: boolean;
  target_id: string;
  sources: string[];
  essentials: string[];
  candidate_valves: Valve[];
  permit_valves: string[];
  applied_permits: AppliedPermit[];
  permit_note: string | null;
  examined_combinations: number;
  solutions: Solution[];
  best_solution: string[];
  residual_path: ResidualPath | null;
  locked_witness_path: ResidualPath | null;
  infeasible_reason: string | null;
  unconstrained_best: {
    close_valves: string[];
    size: number;
    disconnects_essentials: string[];
    unavoidable_essentials: string[];
  } | null;
}

// ---------------------------------------------------------------- 临时旁路许可

export type PermitStatus = 'draft' | 'active' | 'expired' | 'revoked';

export interface Permit {
  id: number;
  target_id: string;
  bypass_valve_ids: string[];
  purpose: string;
  starts_at: string;
  ends_at: string;
  precondition_locks: Record<string, boolean>;
  topology_hash: string;
  state_hash: string;
  status: PermitStatus;
  created_at: string;
  closed_at: string | null;
  applicable_now: boolean;
  linked_plan_ids: number[];
}

export interface PermitEvent {
  id: number;
  permit_id: number;
  seq: number;
  event_type: string;
  occurred_at: string;
  recorded_at: string;
  detail: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface PermitEvents {
  events: PermitEvent[];
  chain_valid: boolean;
}

export interface RevalidateReport {
  permit_id: number;
  status: string;
  checks: Record<string, boolean>;
}

export type PlanStatus = 'current' | 'stale' | 'expired';

export interface PlanSummary {
  id: number;
  target_id: string;
  permit_ids: number[];
  close_valves: string[];
  status: PlanStatus;
  created_at: string;
  status_changed_at: string | null;
}

export interface PlanDetail extends PlanSummary {
  result: IsolationResult;
}

export interface ClockState {
  now: string;
  simulated: boolean;
}
