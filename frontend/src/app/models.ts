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
}

export interface ResidualPath {
  nodes: string[];
  valves: (string | null)[];
  locked_valves_on_path: string[];
  uses_bypass: boolean;
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
}
