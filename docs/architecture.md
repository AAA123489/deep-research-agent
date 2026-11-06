# 代码结构与数据流

> 记录 v1.0 当前进度：哪些写完了、彼此怎么连、还有哪些空着。

---

## 0. 一图总览

```
┌──────────────────────────────────────────────────────┐
│ ① 数据层  data/                                      │
│                                                      │
│   schema.sql ──建表──> generate.py ──> ecommerce.duckdb
│                              │                │      │
│                              │                ▼      │
│                              │          verify.py    │
│                              │          29 项验收     │
│                              └──固定种子──> 可复现    │
└──────────────────────────────┬───────────────────────┘
                               │ 真实数据（10 万订单）
                               ▼
┌──────────────────────────────────────────────────────┐
│ ② 口径层  backend/app/semantic/                      │
│                                                      │
│   metrics.yaml     ─┐                                │
│   dimensions.yaml  ─┴──> loader.py ──> MetricRegistry
│                             │            （只读）      │
│                             └─ 14 类配置错误启动即抛   │
└──────────────────────────────┬───────────────────────┘
                               │ MetricSpec 对象
                               ▼
┌──────────────────────────────────────────────────────┐
│ ③ 守卫层  backend/app/graph/guards.py                │
│                                                      │
│   模型生成的 SQL + MetricSpec ──> check() ──五关──> AST
│                                     │                │
│                                     └─ 不过 → 打回    │
└──────────────────────────────┬───────────────────────┘
                               │ 检查通过的 AST
                               ▼
┌──────────────────────────────────────────────────────┐
│ ④ 编排层 / 执行 / 前端                                │
│                                                      │
│   LangGraph 节点 · DuckDB 执行 · 图表 · 结论 · FastAPI│
│                          ⚠ 还没写                     │
└──────────────────────────────────────────────────────┘
```

**一句话概括**：数据层负责把数造出来，口径层负责定义一句话怎么算，守卫层负责确认模型没算错，编排层负责把这三步串起来。

---

## ① 数据层 `data/`

```
                    data/schema.sql
                   （6 张表的 DDL）
                          │
                          │ generate.py 启动时读取并执行
                          ▼
   种子 20250101 ──> data/generate.py ──> data/ecommerce.duckdb
                          │                       │
                          │  写入：                │
                          │  customers    20,000  │
                          │  products      2,000  │
                          │  orders      100,000  │
                          │  order_items 247,174  │
                          │  refunds      15,000  │
                          │  dataset_meta        │
                          │                       ▼
                          │              data/verify.py
                          │                （只读连接）
                          │                       │
                          │              29 项检查，分五组：
                          │               ├─ 结构：表 / 列 / 类型
                          │               ├─ 规模：行数 ±10%
                          │               ├─ 关系：外键无孤儿
                          │               ├─ 取值：状态枚举 / 金额为正
                          │               └─ 业务：大促月峰值 ≥1.3 倍
                          │
                          └── --repro：连跑两次，比对内容摘要
                                （不是比文件 md5，是比表内容）
```

| 文件 | 作用 | 谁依赖它 |
|---|---|---|
| `data/schema.sql` | 6 张表的建表语句，唯一的表结构来源 | `generate.py` 读取执行 |
| `data/generate.py` | 按固定种子造数，约 3.7 秒 | 无（入口） |
| `data/verify.py` | 验收工具，29 项检查 + 可复现校验 | 无（入口） |
| `data/ecommerce.duckdb` | 生成出来的数据库，`.gitignore` 排除 | `verify.py`、后续执行层 |

> 数据库不进版本库。别人克隆下来必须先跑一次 `generate.py`。

---

## ② 口径层 `backend/app/semantic/`

```
   metrics.yaml                    dimensions.yaml
   （8 个指标）                     （7 个维度）
        │                                │
        │  name / synonyms               │  type / expr
        │  kind / expression             │  granularities
        │  required_filters              │
        │  time_field                    │
        │  refund_treatment              │
        │  dimensions ──────────────────>│ 维度名必须在这里存在
        │                                │
        └────────────┬───────────────────┘
                     ▼
                loader.py
                     │
                     │ 加载 + 校验，错误一律 SemanticConfigError
                     ├─ 字段缺失 / 类型不对 / 枚举越界
                     ├─ 维度不在白名单
                     ├─ 组件引用不存在的指标
                     ├─ 依赖成环（拓扑排序时发现）
                     ├─ additive 漏 refund_treatment
                     ├─ 组件口径冲突却没写 mixed_treatment_ok
                     ├─ expression 被 sqlglot 解析不了
                     └─ 表别名在 source 里没声明（漏 JOIN）
                     ▼
            MetricRegistry（只读）
              ├─ .metrics    : dict[key, MetricSpec]
              ├─ .dimensions : dict[key, DimensionSpec]
              ├─ .get(key)         精确取，取不到给拼写建议
              ├─ .resolve(词)      按 key / 名称 / 同义词匹配
              └─ .visible()        5 个用户可问的指标
                     │
                     ▼
              MetricSpec（frozen dataclass）
              ├─ expression             （YAML 原文）
              ├─ normalized_expression  （sqlglot 归一化后）
              ├─ required_filters / required_joins
              ├─ dimensions             白名单
              └─ refund_treatment       已解析，派生指标不会是 None
```

**8 个指标的分层**

```
                可问（visible: true）
        ┌───────────────┴───────────────┐
     原子指标                      派生指标
   gmv            ──┐            aov   = gmv / order_count
   order_count    ──┼──> gross_profit = gmv - product_cost
   （内部）            │            gross_margin = gross_profit / gmv
   product_cost   ──┘            refund_rate  = refunded_orders / order_count
   refunded_orders

   内部指标 visible: false，用户问不到，只作为组件存在
```

**退款口径怎么传下去**

```
   gmv            refund_treatment = keep_original
   product_cost   refund_treatment = keep_original
        │
        └──> gross_profit  没写 → 继承组件 → keep_original
                    │
                    └──> gross_margin  继承 → keep_original

   refunded_orders  refund_treatment = none
   order_count      refund_treatment = keep_original
        │
        └──> refund_rate   两个组件打架
                           → 查 mixed_treatment_ok
                           → 是 true → mixed
```

| 文件 | 作用 | 被谁依赖 |
|---|---|---|
| `metrics.yaml` | 8 个指标的口径契约 | `loader.py` |
| `dimensions.yaml` | 7 个可分组维度 | `loader.py` |
| `loader.py` | 读 + 校验 + 转对象 | `guards.py`、后续编排层 |
| `__init__.py` | 导出 `load_registry` 等 | 外部 import |

---

## ③ 守卫层 `backend/app/graph/guards.py`

```
                       模型生成的 SQL
                             │
                             ▼
                    ┌─────────────────┐
                    │  sqlglot 解析   │
                    └────────┬────────┘
                             │
        ┌────────────────────┴────────────────────┐
        │              五道关，顺序执行             │
        │                                         │
        │  ①只允许单条 SELECT                      │
        │      拦住 DROP / DELETE / 多语句          │
        │                 │                       │
        │  ②表达式 = 标准算法                       │
        │      派生指标按组件递归展开                │
        │      二元运算插入时补括号                  │
        │                 │                       │
        │  ③必带过滤条件都在                        │
        │  ④必带 JOIN 条件都在                      │
        │      条件沿依赖链合并                      │
        │      （refund_rate 的 JOIN 在组件身上）    │
        │                 │                       │
        │  ⑤GROUP BY 限白名单                       │
        │      支持 DATE_TRUNC 粒度 / 位置 / 别名    │
        │      最终投影禁止出现 customer_id 等字段   │
        └────────────────────┬────────────────────┘
                             │
              ┌──────────────┴──────────────┐
              ▼                             ▼
        返回 AST                     GuardError
      （执行层可直接复用）            ├─ code：给程序分流
                                    └─ hint：回填给模型重写
```

**依赖关系**

```
   MetricRegistry ──┐
                    ├──> guards.check()
   模型生成的 SQL ───┘
```

`check(sql, spec, registry)` 的 `spec` 来自 `registry.get("gmv")`。

| 文件 | 作用 | 依赖 |
|---|---|---|
| `guards.py` | 执行前的五类静态检查 + 12 条演示 | `loader.py` 的 `MetricSpec` / `MetricRegistry` |
| `__init__.py` | 包标记 | — |

---

## 交叉验证：口径层有没有写对

这是目前最重要的一环 —— **两条独立路径必须算出同一个数**。

```
  路径 A（人工标准答案）
  eval/seed_queries.sql ──8 条手写 SQL──> DuckDB ──> 基准数字

  路径 B（语义层驱动）
  metrics.yaml 的 expression
        │
        ▼
  guards.check() 放行
        │
        ▼
       DuckDB ──> 同样 8 个数字

  两条路径结果比对 ──> 一致 ⇒ 语义层可信
```

上次实测结果（8/8 一致）：

| 指标 | 路径 A | 路径 B |
|---|---|---|
| gmv | 150,637,325.19 | 150,637,325.19 |
| order_count | 84,760 | 84,760 |
| product_cost | 106,033,976.71 | 106,033,976.71 |
| refunded_orders | 15,000 | 15,000 |
| aov | 1,777.22 | 1,777.22 |
| gross_profit | 44,603,348.48 | 44,603,348.48 |
| refund_rate | 0.1770 | 0.1770 |
| gross_margin | 0.2961 | 0.2961 |

> `seed_queries.sql` 第 4 条已从 `INNER JOIN + COUNT(DISTINCT o.order_id)` 改成
> `LEFT JOIN + COUNT(DISTINCT CASE ...)`，与 `metrics.yaml` 字面一致。
> 两种写法数字相同（都验证过是 15,000），但守卫只认后一种。

---

## 还没写的部分

```
   用户提问
      │
      ▼
  ┌─────────┐   意图识别（reg.resolve）    ┌──────────────┐
  │ 澄清节点 │<──────────────────────────>│ 生成 SQL 节点 │
  └─────────┘  看不清问什么就问回去        └──────┬───────┘
                                                 │ SQL
                                                 ▼
                                          ┌──────────────┐
                                          │ ✅ guards.py │ 已完成
                                          └──────┬───────┘
                                                 │ 通过 / GuardError
                                                 ▼
                                          ┌──────────────┐
                                          │ 执行 + 修复循环│ ⚠ 待写
                                          └──────┬───────┘
                                                 │ rows
                                                 ▼
                                   ┌─────────────┴─────────────┐
                                   ▼                           ▼
                            ┌────────────┐            ┌────────────┐
                            │ 图表配置    │            │ 结论生成    │
                            │ ⚠ 待写     │            │ ⚠ 待写     │
                            └────────────┘            └────────────┘
                                   │                           │
                                   └─────────────┬─────────────┘
                                                 ▼
                                          ┌────────────┐
                                          │ FastAPI/SSE│ ⚠ 待写
                                          │ + 单页前端 │
                                          └────────────┘
```

| 待写 | 属于 | 说明 |
|---|---|---|
| LangGraph 状态机 | 编排 | 四节点：澄清 / 生成 / 守卫 / 修复 |
| DuckDB 执行节点 | 编排 | 接收守卫返回的 AST |
| 修复循环 | 编排 | 拿 `GuardError.hint` 重新生成，最多 2 次 |
| 图表配置 | 展示 | 按结果形状选折线 / 柱状 / 表格 |
| 结论生成 | 展示 | 每个数字必须能在 `rows` 里找到 |
| FastAPI + SSE | 接口 | 流式返回中间状态 |
| 单页前端 | 展示 | ECharts + 对话区 |
| 20 题评测集 | 评测 | 12 分析 + 5 分组排序 + 3 歧义 |

---

## 文件总表

| 文件 | 层次 | 作用 | 状态 |
|---|---|---|---|
| `data/schema.sql` | ① 数据 | 6 张表结构 | ✅ |
| `data/generate.py` | ① 数据 | 固定种子造数，3.7 秒 | ✅ |
| `data/verify.py` | ① 数据 | 29 项验收 + 可复现 | ✅ |
| `backend/app/semantic/metrics.yaml` | ② 口径 | 8 个指标契约 | ✅ |
| `backend/app/semantic/dimensions.yaml` | ② 口径 | 7 个维度白名单 | ✅ |
| `backend/app/semantic/loader.py` | ② 口径 | 加载 + 14 类校验 | ✅ |
| `backend/app/graph/guards.py` | ③ 守卫 | 五类 SQL 检查 | ✅ |
| `eval/seed_queries.sql` | 交叉 | 8 条标准 SQL 做基准 | ✅ |
| `docs/design.md` | 文档 | v1.0 设计说明 | ✅ |
| 编排层 / 执行 / 前端 | ④ | 见上表 | ⚠ 待写 |
