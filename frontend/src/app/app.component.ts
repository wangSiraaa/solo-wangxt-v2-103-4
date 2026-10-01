import { Component, OnInit, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import { IsolationResult, Topology } from './models';
import { NetworkGraphComponent } from './network-graph.component';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [NetworkGraphComponent],
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
  }

  toggleLock(valveId: string, locked: boolean): void {
    this.locks.update((l) => ({ ...l, [valveId]: locked }));
  }

  compute(): void {
    this.loading.set(true);
    this.error.set(null);
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
        this.loading.set(false);
      },
      error: (e) => {
        this.error.set(`计算失败：${e.error?.detail ?? e.message ?? e}`);
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
      this.reload();
    });
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
