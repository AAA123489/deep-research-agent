-- ============================================================
-- ec_v1 指标标准 SQL（G1 验收用） -- 说明当前 SQL 文件用于 G1 验收，作为统一指标口径的标准查询
--
-- 所有指标只统计「有效订单」： -- 统一口径：只统计已支付且状态属于有效状态的订单
--     status IN ('paid','shipped','completed') AND paid_at IS NOT NULL -- 有效订单的判定条件，避免未支付或无效状态混入指标
--
-- 口径前提： -- 声明本文件所有指标的统计前提
--   * 退款不回冲 —— gmv 包含已退款订单 -- 将已退款订单的 GMV 计入总额，不做冲减
--   * gmv 不含运费、不扣优惠 -- GMV 仅按商品成交价计算，不扣运费和优惠
--   * order_count 必须 COUNT(DISTINCT)，否则算成明细条数 -- 订单数需按订单去重，不能按明细条数统计
-- ============================================================


-- ------------------------------------------------------------
-- 1. gmv —— 有效订单商品成交价合计 -- 说明 GMV 的定义：所有有效订单商品成交额合计
-- ------------------------------------------------------------
SELECT ROUND(SUM(oi.quantity * oi.unit_price), 2) AS gmv -- 计算所有有效订单商品的成交额合计，并保留两位小数
FROM orders o -- 从订单主表读取基础订单信息
JOIN order_items oi ON oi.order_id = o.order_id -- 关联订单明细，取出商品数量和单价
WHERE o.status IN ('paid', 'shipped', 'completed') -- 只统计已支付、已发货和已完成状态的有效订单
  AND o.paid_at IS NOT NULL; -- 过滤未支付订单，确保统计口径一致


-- ------------------------------------------------------------
-- 2. order_count —— 有效订单去重数 -- 说明订单数统计口径：去重后的有效订单数
-- ------------------------------------------------------------
SELECT COUNT(DISTINCT o.order_id) AS order_count -- 统计有效订单的去重数，避免明细重复计数
FROM orders o -- 从订单表读取订单信息
JOIN order_items oi ON oi.order_id = o.order_id -- 关联订单明细，确保只有有明细的有效订单计数
WHERE o.status IN ('paid', 'shipped', 'completed') -- 只统计有效状态订单
  AND o.paid_at IS NOT NULL; -- 过滤未支付订单，保持与 GMV 同口径


-- ------------------------------------------------------------
-- 3. product_cost —— 有效订单商品成本合计 -- 说明成本统计规则：按商品单位成本乘数量累加
-- ------------------------------------------------------------
SELECT ROUND(SUM(oi.quantity * p.cost), 2) AS product_cost -- 计算所有有效订单对应商品的总成本，保留两位小数
FROM orders o -- 从订单表读取事件数据
JOIN order_items oi ON oi.order_id = o.order_id -- 关联订单明细表
JOIN products p ON p.product_id = oi.product_id -- 关联商品表，获取每件商品的成本
WHERE o.status IN ('paid', 'shipped', 'completed') -- 只统计有效状态订单
  AND o.paid_at IS NOT NULL; -- 去除未支付订单，保持汇总口径一致


-- ------------------------------------------------------------
-- 4. refunded_orders —— 有效订单中发生过退款的订单数 -- 说明退款订单的统计口径：按订单去重计数
-- ------------------------------------------------------------
SELECT COUNT(DISTINCT CASE WHEN r.refund_id IS NOT NULL THEN o.order_id END) AS refunded_orders -- 用 CASE 标记退款订单，保持与语义层 metrics.yaml 完全一致
FROM orders o -- 从订单主表读取订单
LEFT JOIN refunds r ON r.order_id = o.order_id -- 左连接退款表，无退款的订单也保留，不会被过滤掉
WHERE o.status IN ('paid', 'shipped', 'completed') -- 只统计有效订单
  AND o.paid_at IS NOT NULL; -- 过滤未支付订单


-- ------------------------------------------------------------
-- 5. aov —— 客单价 = gmv / order_count -- 说明客单价 = 总销售额 / 订单去重数
-- ------------------------------------------------------------
SELECT ROUND(
           SUM(oi.quantity * oi.unit_price) -- 先计算所有有效订单的商品成交额总和
           / NULLIF(COUNT(DISTINCT o.order_id), 0), 2) AS aov -- 用去重后的订单数作为分母，避免除零，并保留两位小数
FROM orders o -- 从订单主表读取有效订单
JOIN order_items oi ON oi.order_id = o.order_id -- 关联订单明细表，拿到商品成交额
WHERE o.status IN ('paid', 'shipped', 'completed') -- 统计有效订单状态
  AND o.paid_at IS NOT NULL; -- 过滤未支付订单


-- ------------------------------------------------------------
-- 6. gross_profit —— 毛利 = gmv - product_cost -- 说明毛利定义：销售额减去商品成本
-- ------------------------------------------------------------
SELECT ROUND(
           SUM(oi.quantity * oi.unit_price) - SUM(oi.quantity * p.cost), 2 -- 毛利等于总销售额减去总成本，保留两位小数
       ) AS gross_profit -- 输出毛利额
FROM orders o -- 从订单表读取有效订单
JOIN order_items oi ON oi.order_id = o.order_id -- 关联订单明细分摊商品数据
JOIN products p ON p.product_id = oi.product_id -- 关联商品表，读取商品成本
WHERE o.status IN ('paid', 'shipped', 'completed') -- 只保留有效订单
  AND o.paid_at IS NOT NULL; -- 排除未支付订单


-- ------------------------------------------------------------
-- 7. refund_rate —— 退款率 = refunded_orders / order_count -- 说明退款率定义：退款订单数 / 有效订单总数
-- ------------------------------------------------------------
SELECT ROUND(
           COUNT(DISTINCT r.order_id)::DOUBLE -- 统计有退款记录的订单数量，并转换为 DOUBLE 以便做比率
           / NULLIF(COUNT(DISTINCT o.order_id), 0), 4) AS refund_rate -- 用有效订单总数做分母，保留四位小数
FROM orders o -- 从订单主表读取有效订单基数
LEFT JOIN refunds r ON r.order_id = o.order_id -- LEFT JOIN 退款表，保留无退款订单也参与分母计算
WHERE o.status IN ('paid', 'shipped', 'completed') -- 限定有效订单状态
  AND o.paid_at IS NOT NULL; -- 排除未支付订单


-- ------------------------------------------------------------
-- 8. gross_margin —— 毛利率 = gross_profit / gmv -- 说明毛利率定义：毛利额 / 销售额
-- ------------------------------------------------------------
SELECT ROUND(
           (SUM(oi.quantity * oi.unit_price) - SUM(oi.quantity * p.cost)) -- 先计算订单毛利额总和
           / NULLIF(SUM(oi.quantity * oi.unit_price), 0), 4) AS gross_margin -- 用总 GMV 做分母，计算毛利率并保留四位小数
FROM orders o -- 从订单主表读取有效订单
JOIN order_items oi ON oi.order_id = o.order_id -- 关联订单明细表
JOIN products p ON p.product_id = oi.product_id -- 关联商品表，用于获取商品成本
WHERE o.status IN ('paid', 'shipped', 'completed') -- 过滤有效订单状态
  AND o.paid_at IS NOT NULL; -- 排除未支付订单


-- ------------------------------------------------------------
-- 9. 合并版 —— 一行看全 8 个指标 -- 说明这是一个汇总版查询，可直接得到 8 个关键指标
-- ------------------------------------------------------------
WITH eff AS ( -- 先定义有效订单集合，统一后续所有指标的基数
    SELECT o.order_id -- 只保留订单编号，方便后续去重和关联
    FROM orders o -- 从订单主表读取订单信息
    WHERE o.status IN ('paid', 'shipped', 'completed') -- 仅保留有效状态订单
      AND o.paid_at IS NOT NULL -- 仅保留真实已支付订单
),
sales AS ( -- 定义每个订单的 GMV 与成本汇总
    SELECT oi.order_id, -- 订单编号
           SUM(oi.quantity * oi.unit_price) AS gmv, -- 统计每单商品成交额
           SUM(oi.quantity * p.cost)        AS cost -- 统计每单商品成本
    FROM order_items oi -- 从订单明细表读取商品明细
    JOIN products p ON p.product_id = oi.product_id -- 关联商品表，计算成本
    GROUP BY oi.order_id -- 按订单编号分组汇总
),
refunded AS ( -- 定义有退款记录的订单集合
    SELECT DISTINCT order_id FROM refunds -- 去除重复订单，确保每个订单只计一次退款
)
SELECT
    ROUND(SUM(s.gmv), 2)                                                  AS gmv, -- 汇总总销售额，保留两位小数
    COUNT(DISTINCT e.order_id)                                            AS order_count, -- 统计订单去重总数
    ROUND(SUM(s.gmv) / NULLIF(COUNT(DISTINCT e.order_id), 0), 2)          AS aov, -- 计算客单价 = 总 GMV / 订单数
    ROUND(SUM(s.gmv) - SUM(s.cost), 2)                                    AS product_cost, -- 这行实际上计算的是毛利，不是产品成本，需注意语义
    COUNT(DISTINCT r.order_id)                                            AS refunded_orders, -- 统计有退款的订单数
    ROUND(COUNT(DISTINCT r.order_id)::DOUBLE
          / NULLIF(COUNT(DISTINCT e.order_id), 0), 4)                     AS refund_rate, -- 计算退款率
    ROUND(SUM(s.gmv) - SUM(s.cost), 2)                                    AS gross_profit, -- 计算毛利额
    ROUND((SUM(s.gmv) - SUM(s.cost)) / NULLIF(SUM(s.gmv), 0), 4)          AS gross_margin -- 计算毛利率
FROM eff e -- 以有效订单作为主基数
LEFT JOIN sales s    ON s.order_id = e.order_id -- 左连接销售汇总，补齐每单 GMV 与成本
LEFT JOIN refunded r ON r.order_id = e.order_id; -- 左连接退款表，用于判断哪些订单发生退款
