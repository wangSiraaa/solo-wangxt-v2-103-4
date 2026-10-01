import {
  AfterViewInit,
  Component,
  ElementRef,
  Input,
  OnChanges,
  OnDestroy,
  ViewChild,
} from '@angular/core';
import cytoscape from 'cytoscape';
import { IsolationResult, Topology } from './models';

/**
 * Cytoscape.js 拓扑渲染：
 * - 节点：来源 / 目标设备 / 必要供给点 / 普通节点
 * - 边：管段（按保存的名义方向显示箭头），旁路使用虚线
 * - 高亮：候选关闭阀（红）、残余/绕回供给路径（橙/绿）
 */
@Component({
  selector: 'app-network-graph',
  standalone: true,
  template: `<div #cy class="cy"></div>`,
  styles: [
    `
      .cy {
        width: 100%;
        height: 560px;
        background: #0f172a;
        border-radius: 10px;
      }
    `,
  ],
})
export class NetworkGraphComponent implements AfterViewInit, OnChanges, OnDestroy {
  @Input() topology: Topology | null = null;
  @Input() result: IsolationResult | null = null;

  @ViewChild('cy') cyHost!: ElementRef<HTMLDivElement>;

  private cy: cytoscape.Core | null = null;

  ngAfterViewInit(): void {
    this.render();
  }

  ngOnChanges(): void {
    if (this.cy) {
      this.render();
    }
  }

  ngOnDestroy(): void {
    this.cy?.destroy();
    this.cy = null;
  }

  private render(): void {
    if (!this.topology) {
      return;
    }
    const topo = this.topology;

    const elements: cytoscape.ElementDefinition[] = [
      ...topo.nodes.map((n) => ({
        data: {
          id: n.id,
          label: `${n.name}${n.essential ? ' ★' : ''}`,
          kind: n.kind,
          essential: n.essential,
        },
        position: { x: n.x, y: n.y },
      })),
      ...topo.segments.map((s) => ({
        data: {
          id: s.id,
          source: s.source,
          target: s.target,
          label: s.valve_id ?? '',
          bypass: s.is_bypass,
        },
      })),
    ];

    if (this.cy) {
      this.cy.destroy();
    }
    this.cy = cytoscape({
      container: this.cyHost.nativeElement,
      elements,
      zoomingEnabled: true,
      userZoomingEnabled: true,
      panningEnabled: true,
      style: [
        {
          selector: 'node',
          style: {
            'background-color': '#94a3b8',
            label: 'data(label)',
            color: '#e2e8f0',
            'font-size': '12px',
            'text-valign': 'bottom',
            'text-margin-y': 6,
            width: 26,
            height: 26,
            'border-width': 2,
            'border-color': '#cbd5e1',
          },
        },
        {
          selector: 'node[kind = "source"]',
          style: { 'background-color': '#16a34a', 'border-color': '#86efac' },
        },
        {
          selector: 'node[kind = "equipment"]',
          style: {
            'background-color': '#f59e0b',
            'border-color': '#fcd34d',
            width: 34,
            height: 34,
          },
        },
        {
          selector: 'node[essential = true]',
          style: { 'background-color': '#0284c7', 'border-color': '#7dd3fc' },
        },
        {
          selector: 'node.hl-target',
          style: { 'border-color': '#f8fafc', 'border-width': 4 },
        },
        {
          selector: 'edge',
          style: {
            width: 2.5,
            'line-color': '#64748b',
            'target-arrow-color': '#64748b',
            'target-arrow-shape': 'triangle',
            'curve-style': 'bezier',
            label: 'data(label)',
            'font-size': '10px',
            color: '#cbd5e1',
            'text-background-color': '#0f172a',
            'text-background-opacity': 0.85,
            'text-background-padding': '2px',
          },
        },
        {
          selector: 'edge[bypass = true]',
          style: {
            'line-style': 'dashed',
            'line-color': '#a78bfa',
            'target-arrow-color': '#a78bfa',
          },
        },
        {
          selector: 'edge.close',
          style: {
            'line-color': '#ef4444',
            'target-arrow-color': '#ef4444',
            width: 5,
            color: '#fca5a5',
          },
        },
        {
          selector: 'edge.residual',
          style: {
            'line-color': '#fb923c',
            'target-arrow-color': '#fb923c',
            width: 4,
            'line-style': 'dotted',
          },
        },
        {
          selector: 'edge.witness',
          style: {
            'line-color': '#f87171',
            'target-arrow-color': '#f87171',
            width: 4,
          },
        },
        {
          selector: 'edge.supply',
          style: {
            'line-color': '#22d3ee',
            'target-arrow-color': '#22d3ee',
            width: 4,
          },
        },
        {
          selector: 'node.onpath',
          style: { 'border-color': '#67e8f9', 'border-width': 3 },
        },
      ] as cytoscape.StylesheetJsonBlock[],
      layout: { name: 'preset' },
    });

    this.applyHighlights();
    this.cy!.fit(undefined, 40);
  }

  private applyHighlights(): void {
    const cy = this.cy;
    const result = this.result;
    const topo = this.topology;
    if (!cy || !result || !topo) {
      return;
    }

    const edgeByValve = new Map<string, string>();
    for (const s of topo.segments) {
      if (s.valve_id) {
        edgeByValve.set(s.valve_id, s.id);
      }
    }

    // 目标设备
    cy.$(`node#${result.target_id}`).addClass('hl-target');

    if (result.feasible && result.solutions.length) {
      const best = result.solutions[0];
      for (const vid of best.close_valves) {
        const eid = edgeByValve.get(vid);
        if (eid) {
          cy.$(`edge#${eid}`).addClass('close');
        }
      }
      // 方案后必要供给点的来源路径
      for (const path of Object.values(best.supply_paths)) {
        if (!path) {
          continue;
        }
        path.forEach((n) => cy.$(`node#${n}`).addClass('onpath'));
        for (let i = 0; i < path.length - 1; i++) {
          cy.edges(`[source = "${path[i]}"][target = "${path[i + 1]}"]`).addClass('supply');
          cy.edges(`[source = "${path[i + 1]}"][target = "${path[i]}"]`).addClass('supply');
        }
      }
    } else if (!result.feasible) {
      const mark = (nodePath: string[], cls: string) => {
        nodePath.forEach((n) => cy.$(`node#${n}`).addClass('onpath'));
        for (let i = 0; i < nodePath.length - 1; i++) {
          cy.edges(`[source = "${nodePath[i]}"][target = "${nodePath[i + 1]}"]`).addClass(cls);
          cy.edges(`[source = "${nodePath[i + 1]}"][target = "${nodePath[i]}"]`).addClass(cls);
        }
      };
      if (result.residual_path) {
        mark(result.residual_path.nodes, 'residual');
      }
      if (result.locked_witness_path) {
        mark(result.locked_witness_path.nodes, 'witness');
      }
    }
  }
}
