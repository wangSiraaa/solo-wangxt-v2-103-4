import { KeyValuePipe } from '@angular/common';
import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import {
  IsolationResult,
  Permit,
  PermitEvent,
  PlanDetail,
  PlanSummary,
  RevalidateReport,
  Topology,
} from './models';
import { NetworkGraphComponent } from './network-graph.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [NetworkGraphComponent, KeyValuePipe],
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
  plans = signal<PlanSummary[]>([]);
  clockNow = signal<string>('');
  clockSimulated = signal(false);
  clockInput = signal<string>('');

  // 新建许可表单
  formValves = signal<Record<string, boolean>>({});
  formStarts = signal<string>('');
  formEnds = signal<string>('');
  formPurpose = signal<string>('');

  // 审计与复核展示
  eventsPermitId = signal<number | null>(null);
  events = signal<PermitEvent[]>([]);
  chainValid = signal<boolean>(true);
  validations = signal<Record<number, RevalidateReport>>({});

  // 历史方案快照（查看时不影响当前状态）
  viewingPlan = signal<PlanDetail | null>(null);

  /** 结果面板/拓扑图实际展示的结果：查看快照时为快照内容，否则为当前计算结果。 */
  displayResult = computed<IsolationResult | null>(
    () => this.viewingPlan()?.result ?? this.result(),
  );

  readonly permitStatusLabel: Record<string, string> = {
    draft: '草稿',
    active: '已生效',
    expired: '已过期',
    revoked: '已撤销',
  };
  readonly planStatusLabel: Record<string, string> = {
    current: '有效',
    stale: '待复核',
    expired: '已过期',
  };
  readonly eventTypeLabel: Record<string, string> = {
    created: '创建',
    activated: '激活',
    revoked: '撤销',
    expired: '到期',
    activate_rejected: '激活被拒绝',
    revoke_rejected: '撤销被拒绝',
  };
  readonly checkLabel: Record<string, string> = {
    window_ok: '窗口内',
    topology_ok: '拓扑快照一致',
    state_ok: '阀门状态一致',
    valves_ok: '锁阀限制满足',
    conditions_ok: '条件匹配',
    solver_feasible: '存在可行方案',
    target_isolated: '目标确已隔离',
    essentials_supplied: '必要供给可达',
    applicable_now: '当前可应用',
    activatable_now: '当前可激活',
  };

  ngOnInit(): void {
    this.reload();
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
    this.reloadPermitsAndPlans();
    this.refreshClock();
  }

  private refreshClock(): void {
    this.api.getClock().subscribe((c) => {
      this.clockNow.set(c.now);
      this.clockSimulated.set(c.simulated);
    });
  }

  reloadPermitsAndPlans(): void {
    this.api.listPermits().subscribe((p) => this.permits.set(p));
    this.api.listPlans().subscribe((p) => this.plans.set(p));
  }

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  compute(): void {
    this.loading.set(true);
    this.error.set(null);
    this.viewingPlan.set(null);
    this.api.isolate(this.targetId(), this.locks()).subscribe({
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
        this.reloadPermitsAndPlans();
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`计算失败：${this.errText(e)}`);
        this.loading.set(false);
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
        this.refreshClock();
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
      this.viewingPlan.set(null);
      this.eventsPermitId.set(null);
      this.reload();
    });
  }

  // ---------------- 临时旁路许可操作 ----------------

  bypassValves() {
    return (this.topology()?.valves ?? []).filter((v) => v.is_bypass);
  }

  toggleFormValve(id: string): void {
    this.formValves.update((m) => ({ ...m, [id]: !m[id] }));
  }

  onFormInput(field: 'starts' | 'ends' | 'purpose', ev: Event): void {
    const value = (ev.target as HTMLInputElement).value;
    if (field === 'starts') {
      this.formStarts.set(value);
    } else if (field === 'ends') {
      this.formEnds.set(value);
    } else {
      this.formPurpose.set(value);
    }
  }

  /** datetime-local 值按 UTC 解释，避免时区歧义（演示时钟统一使用 UTC）。 */
  private toUtcIso(local: string): string {
    return local.length === 16 ? `${local}:00Z` : `${local}Z`;
  }

  createPermit(): void {
    this.error.set(null);
    const valves = Object.keys(this.formValves()).filter((k) => this.formValves()[k]);
    if (!valves.length || !this.formStarts() || !this.formEnds()) {
      this.error.set('请选择旁路阀门并填写维护窗口（开始/结束，UTC）。');
      return;
    }
    this.api
      .createPermit({
        target_id: this.targetId(),
        bypass_valve_ids: valves,
        starts_at: this.toUtcIso(this.formStarts()),
        ends_at: this.toUtcIso(this.formEnds()),
        purpose: this.formPurpose(),
      })
      .subscribe({
        next: () => {
          this.formValves.set({});
          this.formPurpose.set('');
          this.reloadPermitsAndPlans();
        },
        error: (e) => this.error.set(`创建许可被拒绝：${this.errText(e)}`),
      });
  }

  activatePermit(p: Permit): void {
    this.error.set(null);
    this.api.activatePermit(p.id).subscribe({
      next: () => this.reloadPermitsAndPlans(),
      error: (e) => {
        this.error.set(`激活被拒绝：${this.errText(e)}`);
        this.reloadPermitsAndPlans();
      },
    });
  }

  revokePermit(p: Permit): void {
    this.error.set(null);
    this.api.revokePermit(p.id).subscribe({
      next: () => this.reloadPermitsAndPlans(),
      error: (e) => {
        this.error.set(`撤销被拒绝：${this.errText(e)}`);
        this.reloadPermitsAndPlans();
      },
    });
  }

  revalidatePermit(p: Permit): void {
    this.api.revalidatePermit(p.id).subscribe((rep) => {
      this.validations.update((m) => ({ ...m, [p.id]: rep }));
      this.reloadPermitsAndPlans();
    });
  }

  toggleEvents(p: Permit): void {
    if (this.eventsPermitId() === p.id) {
      this.eventsPermitId.set(null);
      return;
    }
    this.api.permitEvents(p.id).subscribe((res) => {
      this.eventsPermitId.set(p.id);
      this.events.set(res.events);
      this.chainValid.set(res.chain_valid);
    });
  }

  // ---------------- 关联方案 ----------------

  viewPlan(plan: PlanSummary): void {
    this.api.planDetail(plan.id).subscribe((detail) => {
      this.viewingPlan.set(detail);
      this.reloadPermitsAndPlans();
    });
  }

  backToCurrent(): void {
    this.viewingPlan.set(null);
  }

  /** 重新计算入口：放弃快照，按当前状态重算（许可仅在仍有效时自动应用）。 */
  recompute(): void {
    this.viewingPlan.set(null);
    this.compute();
  }

  // ---------------- 演示时钟 ----------------

  onClockInput(ev: Event): void {
    this.clockInput.set((ev.target as HTMLInputElement).value);
  }

  applyClock(): void {
    const v = this.clockInput();
    if (!v) {
      return;
    }
    this.api.setClock(this.toUtcIso(v)).subscribe((c) => {
      this.clockNow.set(c.now);
      this.clockSimulated.set(c.simulated);
      this.reloadPermitsAndPlans();
    });
  }

  clearClock(): void {
    this.api.setClock(null).subscribe((c) => {
      this.clockNow.set(c.now);
      this.clockSimulated.set(c.simulated);
      this.reloadPermitsAndPlans();
    });
  }

  // ---------------- 展示辅助 ----------------

  private errText(e: { error?: { detail?: unknown }; message?: string }): string {
    const d = e?.error?.detail;
    if (typeof d === 'string') {
      return d;
    }
    if (d && typeof d === 'object' && 'message' in d) {
      return String((d as { message: unknown }).message);
    }
    return e?.message ?? String(e);
  }

  fmtTime(iso: string | null | undefined): string {
    if (!iso) {
      return '—';
    }
    return iso.replace('T', ' ').replace(/(\+00:00|Z)$/, '').slice(0, 16) + ' UTC';
  }

  checkName(key: string): string {
    return this.checkLabel[key] ?? key;
  }

  eventName(type: string): string {
    return this.eventTypeLabel[type] ?? type;
  }

  permitEnabledValves(): Set<string> {
    const r = this.result();
    return new Set((r?.candidate_valves ?? []).filter((v) => v.permit_enabled).map((v) => v.id));
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
