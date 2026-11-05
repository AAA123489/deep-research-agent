# 电商经营分析 Agent（ChatBI）

用自然语言问数据：生成 SQL → 校验 → 执行 → 自纠错 → 出图 → 给结论。
**指标口径统一、过程可控、结果可审计。**

![status](https://img.shields.io/badge/status-WIP-yellow)
![Python](https://img.shields.io/badge/Python-3.11%2B-3772AA?logo=python&logoColor=white)
![LangGraph](https://img.shields.io/badge/LangGraph-stateful%20graph-1C3C3C)
![DuckDB](https://img.shields.io/badge/DuckDB-analytics-FFF000)
![Vue3](https://img.shields.io/badge/Vue-3-42B883?logo=vue.js&logoColor=white)

> 开发中。设计文档：[docs/design.md](docs/design.md)

## 为什么做这个

BI 自助取数的真实痛点是**口径不一致**：同一个「销售额」，市场部含退款、财务部不含，两个人问同一个问题得到两个数。直接让大模型写 SQL 只会把这个混乱放大。

所以这个项目把四件事放在模型前面：

1. **指标语义层**——指标公式、时间字段、维度白名单写在 YAML 里；模型可以自由组织 SQL，但必须用指标别名，并满足口径签名。
2. **口径签名校验**——AST 检查聚合函数、引用列、必需过滤、时间字段和维度，缺过滤即拒，不静默放行。
3. **歧义澄清**——「最近卖得怎么样」不猜，先问清时间窗和指标。
4. **SQL 守卫**——只读连接、单语句 / SELECT-only / 表白名单、强制 LIMIT、超时熔断。

模型负责理解与表达，代码负责口径与安全。

## 核心能力

| 能力 | 实现 |
|---|---|
| 🧠 多步编排 | LangGraph 状态机：改写 → 分类 → 槽位检查 → 生成 → 校验 → 执行 → 修复 → 出图 → 结论 |
| 🙋 人机协同 | `interrupt()` 处理歧义追问与高风险 SQL 审批，`Command(resume)` 从断点继续 |
| 💾 会话记忆 | Checkpointer 持久化，多轮追问（「按省份拆一下」）与刷新续跑 |
| 📐 口径校验 | AST 检查聚合 / 引用列 / 必需过滤 / 时间字段 / 维度，口径不符直接修复 |
| 🛡️ SQL 安全 | 只读 DuckDB，单语句 / SELECT-only / 表白名单 / 强制 LIMIT / 超时熔断 |
| 🔁 自纠错 | 报错、空结果、列不匹配时带错误上下文重写，最多 3 次 |
| 📊 图表 | 模型输出结构化 chart spec，代码用 ECharts 渲染，禁止生成 JS |
| 📏 可评测 | 100 道基准题按**结果集**判定，输出准确率 / 耗时 / 成本 |

## 流程

```
用户提问
   ↓
rewrite ─ classify ─ load_context ─ check_slots
                          │            │
                          │      缺失 → interrupt 澄清
                          ↓
                     generate_sql
                          ↓
                     guard_sql ──失败──→ repair ──→ …
                          ↓
                 高风险 → interrupt 审批
                          ↓
                      execute
                          ↓
          inspect ──报错/空结果──→ repair（≤3 次）
                          ↓
                   chart ─ interpret
                          ↓
                     答案 + SQL + 图表 + 口径说明
```

## 技术栈

| 层 | 选型 |
|---|---|
| 编排 | LangGraph + SQLite checkpointer |
| Web | FastAPI + SSE |
| 分析库 | DuckDB（只读） |
| SQL 校验 | sqlglot |
| LLM | DeepSeek（OpenAI 兼容抽象，可切换） |
| 前端 | Vue3 + Vite + ECharts |
| 测试 | pytest + FakeLLM |

## 演示数据

固定随机种子生成的电商经营数据，时间范围 2025-01 ~ 2026-09：

`customers` / `products` / `orders` / `order_items` / `refunds` / `visits`

约 3 万客户、2000 商品、12 万订单、80 万访问。生成器刻意埋入大促峰值、渠道差异、区域偏好和高退款品类。

## 快速开始

```bash
# 待实现
```

## 路线图

- [ ] P0 数据生成 + DuckDB + 指标语义层 + 口径签名守卫原型
- [ ] P1 LangGraph 主干 + SQL 守卫 + 修复环
- [ ] P2 澄清 + 审批 + 多轮会话
- [ ] P3 图表 + 结论 + 审计
- [ ] P4 Vue 前端
- [ ] P5 100 题评测与基线

## 评测

实现后补充：执行准确率、澄清命中率、修复成功率、P50/P95 耗时、单次成本。

## 工程说明（演示版与生产差异）

- **Checkpointer**：本地用 `langgraph-checkpoint-sqlite`（单写者 + WAL）；生产并发换 `langgraph-checkpoint-postgres`。
- **任务生命周期**：run 由 `asyncio.create_task` 驱动，进程重启后标记 `interrupted`；生产改用任务队列（Arq / Celery）。
- **数据一致性**：DuckDB 以只读方式打开，查询中断或进程退出不会留下半写状态；连接断开自动重连。
- **审批阈值**：演示环境 4 表触发，生产建议 5 表 + 以 `EXPLAIN` 扫描量为主，全部走配置。

## License

MIT
