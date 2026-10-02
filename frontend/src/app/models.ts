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
  permit_bypass_in_supply?: string[];
}

export interface ResidualPath {
  nodes: string[];
  valves: (string | null)[];
  locked_valves_on_path: string[];
  uses_bypass: boolean;
}

export interface Permit {
  id: number;
  target_id: string;
  valve_ids: string[];
  segment_ids: string[];
  purpose: string;
  start_at: string | null;
  end_at: string | null;
  status: 'draft' | 'active' | 'expired' | 'revoked';
  topo_hash: string;
  precondition_locks: Record<string, boolean>;
  conditions_match: boolean;
  condition_mismatches: string[];
  created_at: string | null;
  activated_at: string | null;
  revoked_at: string | null;
  expired_at: string | null;
  revoke_reason: string | null;
  disclaimer: string;
}

export interface PermitEvent {
  seq: number;
  event_type: string;
  at: string | null;
  recorded_at: string | null;
  detail: Record<string, unknown>;
  prev_hash: string;
  hash: string;
}

export interface PermitHistory {
  permit: Permit;
  events: PermitEvent[];
  chain_valid: boolean;
  disclaimer: string;
}

export interface Plan {
  id: number;
  target_id: string;
  permit_id: number | null;
  status: 'valid' | 'stale' | 'expired';
  status_reason: string | null;
  feasible: boolean;
  created_at: string | null;
  status_at: string | null;
  best_solution: string[];
  supply_paths: Record<string, string[] | null>;
  result: IsolationResult;
  disclaimer: string;
}

export interface IsolationResult {
  feasible: boolean;
  target_id: string;
  sources: string[];
  essentials: string[];
  candidate_valves: Valve[];
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
  permit_valves: string[];
  permit: Permit | null;
  plan_id: number | null;
  disclaimer: string;
}
