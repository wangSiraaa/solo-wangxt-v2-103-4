import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import {
  ClockState,
  IsolationResult,
  Permit,
  PermitEvents,
  PlanDetail,
  PlanSummary,
  RevalidateReport,
  Topology,
} from './models';

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
  ): Observable<IsolationResult> {
    return this.http.post<IsolationResult>('/api/isolation', {
      target_id: targetId,
      locks: locks ?? null,
      permit_id: permitId ?? null,
    });
  }

  // ---------------- 临时旁路许可 ----------------

  listPermits(): Observable<Permit[]> {
    return this.http.get<Permit[]>('/api/permits');
  }

  createPermit(body: {
    target_id: string;
    bypass_valve_ids: string[];
    starts_at: string;
    ends_at: string;
    purpose: string;
  }): Observable<Permit> {
    return this.http.post<Permit>('/api/permits', body);
  }

  activatePermit(id: number, occurredAt?: string | null): Observable<Permit> {
    return this.http.post<Permit>(`/api/permits/${id}/activate`, {
      occurred_at: occurredAt ?? null,
    });
  }

  revokePermit(id: number, occurredAt?: string | null): Observable<Permit> {
    return this.http.post<Permit>(`/api/permits/${id}/revoke`, {
      occurred_at: occurredAt ?? null,
    });
  }

  revalidatePermit(id: number): Observable<RevalidateReport> {
    return this.http.post<RevalidateReport>(`/api/permits/${id}/revalidate`, {});
  }

  permitEvents(id: number): Observable<PermitEvents> {
    return this.http.get<PermitEvents>(`/api/permits/${id}/events`);
  }

  // ---------------- 关联方案 ----------------

  listPlans(): Observable<PlanSummary[]> {
    return this.http.get<PlanSummary[]>('/api/plans');
  }

  planDetail(id: number): Observable<PlanDetail> {
    return this.http.get<PlanDetail>(`/api/plans/${id}`);
  }

  // ---------------- 演示时钟 ----------------

  getClock(): Observable<ClockState> {
    return this.http.get<ClockState>('/api/clock');
  }

  setClock(nowIso: string | null): Observable<ClockState> {
    return this.http.post<ClockState>('/api/clock', { now: nowIso });
  }
}
