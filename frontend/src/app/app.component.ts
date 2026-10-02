import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Observable } from 'rxjs';
import { ApiService } from './api.service';
import { IsolationResult, Permit, PermitHistory, Plan, Topology } from './models';
import { NetworkGraphComponent } from './network-graph.component';

const BYPASS_VALVES = ['V_BP_IN', 'V_BP_OUT'];

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [NetworkGraphComponent, FormsModule],
  templateUrl: './app.component.html',
})
export class AppComponent implements OnInit {
  private api = inject(ApiService);

  topology = signal<Topology | null>(null);
  result = signal<IsolationResult | null>(null);
  targetId = signal<string>('T');
  loading = signal(false);
  error = signal<string | null>(null);

  // 阀门锁定状态（仅前端选择，计算时随请求提交并由后端持久化）
  locks = signal<Record<string, boolean>>({});

  // ---------------- 临时旁路许可 ----------------
  permits = signal<Permit[]>([]);
  plans = signal<Plan[]>([]);
  // 查看历史快照时不覆盖当前计算结果，而是单独展示
  snapshot = signal<Plan | null>(null);
  history = signal<PermitHistory | null>(null);
  // 计算时选择应用的许可（null=不应用）
  selectedPermitId = signal<number | null>(null);
  // 演练模拟时钟（留空=服务器当前时间），用于演示到期/乱序事件
  simClock = '';
  // 新建许可表单
  permitPurpose = '维护窗口内临时启用旁路保供';
  permitStart = '';
  permitEnd = '';

  readonly bypassValves = BYPASS_VALVES;

  ngOnInit(): void {
    this.reload();
  }

  private simAt(): string | null {
    return this.simClock ? new Date(this.simClock).toISOString() : null;
  }

  reload(): void {
    this.loading.set(true);
    this.api.topology().subscribe({
      next: (t) => {
        this.topology.set(t);
        const l: Record<string, boolean> = {};
        for (const v of t.valves) {
          l[v.id] = v.locked;
        }
        this.locks.set(l);
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`无法加载拓扑：${e.message ?? e}`);
        this.loading.set(false);
      },
    });
    this.refreshPermitsAndPlans();
  }

  refreshPermitsAndPlans(): void {
    const at = this.simAt();
    this.api.listPermits(at).subscribe({
      next: (p) => this.permits.set(p),
      error: (e) => this.error.set(`许可列表加载失败：${this.errText(e)}`),
    });
    this.api.listPlans(at).subscribe({
      next: (p) => this.plans.set(p),
      error: (e) => this.error.set(`方案列表加载失败：${this.errText(e)}`),
    });
  }

  private errText(e: { error?: { detail?: unknown }; message?: string }): string {
    const d = e.error?.detail;
    if (d && typeof d === 'object' && 'message' in d) {
      return String((d as { message: unknown }).message);
    }
    return String(d ?? e.message ?? e);
  }

  private run<T>(obs: Observable<T>, ok: (v: T) => void): void {
    this.error.set(null);
    obs.subscribe({
      next: (v) => {
        ok(v);
        this.refreshPermitsAndPlans();
      },
      error: (e) => this.error.set(this.errText(e)),
    });
  }

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  toggleValveState(valveId: string, isOpen: boolean): void {
    this.run(this.api.setValveState(valveId, isOpen), () => this.reload());
  }

  compute(): void {
    this.loading.set(true);
    this.error.set(null);
    this.snapshot.set(null);
    this.api.isolate(this.targetId(), this.locks(), this.selectedPermitId(), this.simAt()).subscribe({
      next: (r) => {
        this.result.set(r);
        // 同步锁定勾选状态（后端为权威来源）
        this.locks.update((l) => {
          const next = { ...l };
          for (const v of r.candidate_valves) {
            next[v.id] = v.locked;
          }
          return next;
        });
        this.loading.set(false);
        this.refreshPermitsAndPlans();
      },
      error: (e) => {
        this.error.set(`计算失败：${this.errText(e)}`);
        this.loading.set(false);
        this.refreshPermitsAndPlans();
      },
    });
  }

  /** 快捷装载三个培训样例的锁定组合（样例 1 先重置）。 */
  loadSample(sample: 1 | 2 | 3): void {
    const applyLocksAndCompute = () => {
      const l: Record<string, boolean> = {};
      for (const v of this.topology()?.valves ?? []) {
        l[v.id] = false;
      }
      if (sample === 2) {
        l['V_TIN'] = true;
      } else if (sample === 3) {
        l['V_TOUT'] = true;
      }
      this.locks.set(l);
      this.compute();
    };

    if (sample === 1) {
      // 重置 -> 重新拉取拓扑 -> 计算
      this.api.reset().subscribe(() => {
        this.api.topology().subscribe((t) => {
          this.topology.set(t);
          const l: Record<string, boolean> = {};
          for (const v of t.valves) {
            l[v.id] = v.locked;
          }
          this.locks.set(l);
          applyLocksAndCompute();
        });
      });
    } else {
      applyLocksAndCompute();
    }
  }

  resetAll(): void {
    this.api.reset().subscribe(() => {
      this.result.set(null);
      this.snapshot.set(null);
      this.history.set(null);
      this.selectedPermitId.set(null);
      this.reload();
    });
  }

  // ---------------- 许可操作 ----------------

  createPermit(): void {
    if (!this.permitStart || !this.permitEnd) {
      this.error.set('请填写许可窗口的开始/结束时刻');
      return;
    }
    this.run(
      this.api.createPermit({
        target_id: this.targetId(),
        valve_ids: BYPASS_VALVES,
        purpose: this.permitPurpose,
        start_at: new Date(this.permitStart).toISOString(),
        end_at: new Date(this.permitEnd).toISOString(),
        at: this.simAt(),
      }),
      () => undefined,
    );
  }

  activatePermit(id: number): void {
    this.run(this.api.activatePermit(id, this.simAt()), () => undefined);
  }

  revokePermit(id: number): void {
    this.run(this.api.revokePermit(id, '演练撤销', this.simAt()), () => undefined);
  }

  showHistory(id: number): void {
    if (this.historyOpenFor(id)) {
      this.history.set(null);
      return;
    }
    this.run(this.api.permitHistory(id, this.simAt()), (h) => this.history.set(h));
  }

  historyOpenFor(id: number): boolean {
    return this.history()?.permit.id === id;
  }

  viewPlan(id: number): void {
    this.run(this.api.getPlan(id, this.simAt()), (p) => {
      this.snapshot.set(p);
      this.result.set(p.result);
    });
  }

  recomputePlan(id: number): void {
    this.run(this.api.recomputePlan(id, this.simAt()), (p) => {
      this.snapshot.set(null);
      this.result.set(p.result);
    });
  }

  // ---------------- 展示辅助 ----------------

  permitStatusText(s: Permit['status']): string {
    return { draft: '草稿', active: '已生效', expired: '已过期', revoked: '已撤销' }[s];
  }

  planStatusText(s: Plan['status']): string {
    return { valid: '有效', stale: '待复核', expired: '已过期' }[s];
  }

  eventText(t: string): string {
    const map: Record<string, string> = {
      created: '创建',
      activated: '激活',
      activate_duplicate: '重复激活（幂等忽略）',
      activate_rejected: '激活被拒绝',
      revoked: '撤销',
      revoke_duplicate: '重复撤销（幂等忽略）',
      revoke_rejected: '撤销被拒绝',
      expired: '到期',
    };
    return map[t] ?? t;
  }

  fmtTime(iso: string | null): string {
    if (!iso) {
      return '—';
    }
    const d = new Date(iso);
    return isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  activePermits(): Permit[] {
    return this.permits().filter((p) => p.status === 'active' && p.conditions_match);
  }

  valveName(id: string): string {
    return this.topology()?.valves.find((v) => v.id === id)?.name ?? id;
  }

  nodeName(id: string): string {
    return this.topology()?.nodes.find((n) => n.id === id)?.name ?? id;
  }

  formatValves(valves: (string | null)[]): string {
    return valves.map((v) => v ?? '—').join('、');
  }

  joinIds(ids: string[]): string {
    return ids.join('、');
  }
}
