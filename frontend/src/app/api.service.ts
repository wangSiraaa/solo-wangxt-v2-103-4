import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { IsolationResult, Permit, PermitHistory, Plan, Topology } from './models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  topology(): Observable<Topology> {
    return this.http.get<Topology>('/api/topology');
  }

  setLock(valveId: string, locked: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/lock`, { locked });
  }

  setValveState(valveId: string, isOpen: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/state`, { is_open: isOpen });
  }

  reset(): Observable<unknown> {
    return this.http.post('/api/reset', {});
  }

  isolate(
    targetId: string,
    locks?: Record<string, boolean>,
    permitId?: number | null,
    at?: string | null,
  ): Observable<IsolationResult> {
    return this.http.post<IsolationResult>('/api/isolation', {
      target_id: targetId,
      locks: locks ?? null,
      permit_id: permitId ?? null,
      at: at ?? null,
    });
  }

  // ---------------- 临时旁路许可 ----------------

  createPermit(body: {
    target_id: string;
    valve_ids: string[];
    purpose: string;
    start_at: string;
    end_at: string;
    at?: string | null;
  }): Observable<Permit> {
    return this.http.post<Permit>('/api/permits', { ...body, at: body.at ?? null });
  }

  listPermits(at?: string | null): Observable<Permit[]> {
    return this.http.get<Permit[]>('/api/permits', this.atParams(at));
  }

  activatePermit(id: number, at?: string | null): Observable<Permit> {
    return this.http.post<Permit>(`/api/permits/${id}/activate`, { at: at ?? null });
  }

  revokePermit(id: number, reason: string, at?: string | null): Observable<Permit> {
    return this.http.post<Permit>(`/api/permits/${id}/revoke`, { at: at ?? null, reason });
  }

  permitHistory(id: number, at?: string | null): Observable<PermitHistory> {
    return this.http.get<PermitHistory>(`/api/permits/${id}/history`, this.atParams(at));
  }

  listPlans(at?: string | null): Observable<Plan[]> {
    return this.http.get<Plan[]>('/api/plans', this.atParams(at));
  }

  getPlan(id: number, at?: string | null): Observable<Plan> {
    return this.http.get<Plan>(`/api/plans/${id}`, this.atParams(at));
  }

  recomputePlan(id: number, at?: string | null): Observable<Plan> {
    return this.http.post<Plan>(`/api/plans/${id}/recompute`, { at: at ?? null });
  }

  private atParams(at?: string | null): { params: Record<string, string> } | Record<string, never> {
    return at ? { params: { at } } : {};
  }
}
