# 管网隔离方案培训演示（Isolation Training Demo）

面向工艺培训的**纯演示系统**：在一张固定的模拟管网上，计算隔离目标设备所需关闭的
阀门候选集合。**不连接任何真实控制系统，计算结果不代表真实检修已满足安全隔离条件。**

- 前端：Angular 19 + Cytoscape.js（拓扑、管段方向、旁路、阀门锁定、方案与残余路径高亮）
- 后端：FastAPI + NetworkX（无向物理连通图上的候选阀门集合枚举与约束校验）
- 存储：PostgreSQL（节点连接、阀门开闭/锁定、必要供给点；本地无 PG 时自动回退 SQLite）

## 演示拓扑

```
SRC ─V0─ N1 ─V1─ N2 ─V_TIN─ [T] ─V_TOUT─ N3
          │            ╲  旁路 N2─V_BP_IN─BP─V_BP_OUT─N3 ╱
         V_P1           环网联络 N1─V_LK1─N5─V_LK2─N3
          ↓                          N3 ─V_P2─ P2
          P1
```

- 每条管段显式保存名义方向（upstream→downstream，图上箭头标注）；隔离按**无向物理连通**计算。
- 旁路（紫虚线）与目标设备 T 并联；P1、P2 为**必要供给点**（任何方案不得断供）。
- 阀门与管段 1:1；初始全部打开。锁定 = 禁止关闭（保持现状），可在页面勾选后重新计算。

## 三个培训样例

| 样例 | 锁定 | 结果 |
| --- | --- | --- |
| 1 旁路绕回 | 无 | 关 `V_TIN`+`V_TOUT` 即隔离 T；旁路保持打开，P2 经 `N2→BP→N3` 绕回不断供 |
| 2 锁定入口阀 | `V_TIN` | 最小集合升为 3 阀：`V_TOUT`+`V1`+旁路一只（`V_BP_IN`/`V_BP_OUT` 两个等价方案）；旁路被封，P2 改由环网 `N1→N5→N3` 供料 |
| 3 不应断供的支路 | `V_TOUT` | **无可行方案**：任何切法都不可避免断供 P2（P1 可保住）；页面显示仍连通的残余路径 `SRC→N1→N2→T`，以及经锁定阀的见证路径 `…→N3→T`（含 `V_TOUT`） |

> 仅关 T 一侧阀门时隔离不成立：例如只关 `V_TIN`，介质仍可经旁路绕回 N3 再回到 T
> （`N2→BP→N3→T`），或经环网绕回。这正是样例 1 必须两侧同关的原因。

## 本地运行

### 后端（无 PostgreSQL 时自动用 SQLite 文件）

```bash
cd backend
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --reload --port 8000
# API: http://127.0.0.1:8000/api/topology, /api/isolation, /api/valves/{id}/lock, /api/reset
# 测试: python3 -m pytest tests/ -q
```

指定 PostgreSQL：

```bash
export DATABASE_URL=postgresql+psycopg://isolation:isolation@localhost:5432/isolation_demo
docker compose up -d db        # 或使用任意已有 PG 实例
```

### 前端

```bash
cd frontend
npm install
npm start                      # http://localhost:4200 （/api 代理到 8000）
# 或产物构建后由后端直接托管: npx ng build  → http://127.0.0.1:8000/
```

### 一键（含 PG）

```bash
cd frontend && npm ci && npx ng build && cd ..
docker compose up --build
```

## 临时旁路许可（演练）

演练中可为指定维护窗口临时启用停用的旁路以维持供给。许可是纯模拟层授权，
**不改动任何阀门真实状态、不输出真实控制指令**。

- **许可内容**：适用拓扑快照（哈希）、旁路/阀门、开始/结束时刻、目的、
  前置锁定状态、状态机 `草稿 → 已生效 → 已过期/已撤销`。
- **生效条件**：隔离计算只在许可 `已生效` 且拓扑/锁阀条件与快照匹配时，
  才把许可旁路阀视为临时投用（且不可被方案切断）纳入可用路径。
- **激活校验**：激活前重新校验窗口、同旁路重叠许可、拓扑/前置锁定一致性、
  锁阀限制（许可阀不得被锁定），并预演“应用许可后目标确实可隔离且必要供给点仍可达”。
- **失效联动**：时限到达（过期时刻恒为窗口结束）、拓扑/锁阀变化或许可被撤销后，
  关联方案转为 `待复核/已过期`，不会静默沿用旧计算；方案快照（含旧路径）原样保留，
  并提供重新计算入口生成新方案。
- **确定性**：同一旁路重叠窗口的创建会被拒绝；重复激活/撤销幂等；事件顺序以服务器
  追加序号为准，乱序/迟到的模拟时钟事件不会重排历史；审计事件哈希链可校验篡改。

典型演练：停用 `V_BP_IN/V_BP_OUT`（旁路）与 `V_LK2`（环网）后，无许可时隔离 T
必然断供 P2（无可行方案）；创建并激活窗口许可后，关 `V_TIN`+`V_TOUT` 即可隔离 T，
P2 经许可旁路 `N2→BP→N3` 保供。

## API 摘要

- `GET /api/topology`：节点 / 有向管段 / 阀门（含 `is_open`、`locked`、`is_bypass`）
- `POST /api/isolation`：body `{ "target_id": "T", "locks": {"V_TIN": true}, "permit_id": null, "at": null }`
  - 可行：`best_solution`（最少阀门）、等价方案、方案后每个必要供给点的来源路径
  - 不可行：`residual_path`（仍连通的一条残余路径）、`locked_witness_path`（经锁定阀的见证路径）、
    `unconstrained_best.unavoidable_essentials`（任何切法都无法保住的供给点）
  - 每次计算持久化为方案记录（返回 `plan_id`），响应附培训模拟 `disclaimer`
- `POST /api/valves/{valve_id}/lock`：持久化单只阀门锁定状态
- `POST /api/valves/{valve_id}/state`：设置阀门开闭（投用/停用，模拟检修退出运行）
- `POST /api/permits`：创建许可（草稿）；同旁路重叠窗口返回 409
- `POST /api/permits/{id}/activate` / `revoke`：激活 / 撤销（幂等；冲突 409 并记录审计事件）
- `GET /api/permits` / `/{id}` / `/{id}/history`：许可状态、条件匹配结果与哈希链审计历史
- `GET /api/plans` / `/{id}`：方案记录（读取时惰性复核状态）；`POST /api/plans/{id}/recompute`：按当前条件重算
- `POST /api/reset`：全部阀门恢复打开、未锁定，并清空许可/方案演练记录
- 许可/方案接口均接受可选 `at`（ISO 时刻）作为演练模拟时钟，缺省为服务器当前时间

## 安全边界声明

系统仅对**给定拓扑与阀门模型**做枚举演示：假设阀门与管段 1:1、关阀即断边、
无背压/泄漏/盲板/双阀双断等真实工况要素。任何输出**不得**作为真实检修隔离
（LOTO）的安全依据。
