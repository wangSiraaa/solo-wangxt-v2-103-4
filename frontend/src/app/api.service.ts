import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { IsolationResult, Topology } from './models';

@Injectable({ providedIn: 'root' })
export class ApiService {
  private http = inject(HttpClient);

  topology(): Observable<Topology> {
    return this.http.get<Topology>('/api/topology');
  }

  setLock(valveId: string, locked: boolean): Observable<unknown> {
    return this.http.post(`/api/valves/${valveId}/lock`, { locked });
  }

  reset(): Observable<unknown> {
    return this.http.post('/api/reset', {});
  }

  isolate(targetId: string, locks?: Record<string, boolean>): Observable<IsolationResult> {
    return this.http.post<IsolationResult>('/api/isolation', {
      target_id: targetId,
      locks: locks ?? null,
    });
  }
}
