# 代码结构

> 这 6 个文件是一条流水线，前一个的产物交给后一个。

---

## 流水线

```
1. schema.sql      规定有哪些表、每张表哪些列
        ↓
2. generate.py     照着表造 10 万条假数据，存进 ecommerce.duckdb
        ↓
3. verify.py       检查造出来的数据对不对（29 项）
        ↓
4. metrics.yaml    手写：GMV 怎么算、退款率怎么算
   dimensions.yaml 手写：能按哪几个维度分组
        ↓
5. loader.py       把 yaml 读成程序能用的东西，顺便检查写没写错
        ↓
6. guards.py       检查模型写的 SQL，有没有按 yaml 的规矩算
```

前面 3 个是**数据**，中间 2 个是**规矩**，最后 1 个是**检查**。

---

## 谁用谁

```
generate.py    用  schema.sql
verify.py      用  generate.py 造出来的数据库
loader.py      用  两个 yaml
guards.py      用  loader.py 读出来的结果
```

就这么四条，没有别的依赖。

---

## 现在缺什么

缺第 7 步：**把上面这些串起来，让用户问一句话就能出结果。**

```
用户："上个月各省销售额"
        ↓
  调 loader 拿到 GMV 的定义
        ↓
  让模型写 SQL
        ↓
  guards 检查 → 通过了才查库
        ↓
  把数字和图表返回给用户
```

这一段就是接下来要写的 LangGraph 编排层。

---

## 每个文件一句话

| 文件 | 干什么的 | 状态 |
|---|---|---|
| `data/schema.sql` | 6 张表的建表语句 | ✅ |
| `data/generate.py` | 按固定种子造数，3.7 秒跑完 | ✅ |
| `data/verify.py` | 验收工具，29 项检查 | ✅ |
| `backend/app/semantic/metrics.yaml` | 8 个指标怎么算 | ✅ |
| `backend/app/semantic/dimensions.yaml` | 7 个可分组维度 | ✅ |
| `backend/app/semantic/loader.py` | 读 yaml + 检查写没写错 | ✅ |
| `backend/app/graph/guards.py` | 检查模型写的 SQL | ✅ |
| `eval/seed_queries.sql` | 手写 8 条标准 SQL，用来对答案 | ✅ |

---

## 额外的：怎么确认 yaml 写对了

`metrics.yaml` 里的公式是手写的，可能写错。所以另写了一份 `seed_queries.sql`，
里面是 8 条**人工写好的标准 SQL**，直接拿去查库，得到基准数字。

然后让 `guards.py` 放行按 `metrics.yaml` 拼出来的 SQL，也查一遍。

```
seed_queries.sql  →  查库  →  基准数字
                                    ≠  比对
metrics.yaml      →  查库  →  另一组数字
```

两边一致，说明 yaml 写对了。目前 8 个指标全部一致。

---

## 怎么跑

```bash
# 1. 造数据
python data/generate.py --out data/ecommerce.duckdb

# 2. 验收
python data/verify.py data/ecommerce.duckdb

# 3. 检查 yaml 写没写错
python backend/app/semantic/loader.py

# 4. 跑守卫的演示
cd backend && python -m app.graph.guards
```
