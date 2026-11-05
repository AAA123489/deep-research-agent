-- 电商经营分析数据集 ec_v1 -- 定义当前数据集的业务主题，供后续分析和文档引用
-- 生成顺序：customers → products → orders → order_items → refunds -- 按依赖关系顺序建表，避免外键和关联查询失败
-- 金额统一 DECIMAL(10,2)，避免浮点误差污染后续 Decimal 比对 -- 所有金额字段统一精度，保证财务计算一致性

DROP TABLE IF EXISTS order_items; -- 删除订单明细表，确保重建时不受历史数据干扰
DROP TABLE IF EXISTS refunds; -- 删除退款表，保证后续重建顺序和分析表一致
DROP TABLE IF EXISTS orders; -- 删除订单主表，避免历史订单记录影响新数据集初始化
DROP TABLE IF EXISTS products; -- 删除商品维度表，确保品类及价格数据同步更新
DROP TABLE IF EXISTS customers; -- 删除客户维度表，重置用户基础信息表
DROP TABLE IF EXISTS dataset_meta; -- 删除元数据表，清理版本和种子信息

CREATE TABLE customers ( -- 创建客户维度表，保存用户的基础属性和注册信息
    customer_id   BIGINT PRIMARY KEY, -- 客户唯一标识，用作订单和访问行为的关联键
    name          VARCHAR NOT NULL, -- 客户姓名，作为用户明细展示字段
    gender        VARCHAR NOT NULL, -- male / female / unknown -- 用户性别枚举，便于人口统计分析
    age           INTEGER NOT NULL, -- 客户年龄，用于分层分析和画像研究
    city          VARCHAR NOT NULL, -- 城市，便于按地域分析消费和活跃度
    province      VARCHAR NOT NULL, -- 省份，用于区域经营分析和省级汇总
    register_date DATE    NOT NULL, -- 注册日期，用于衡量新老用户结构
    channel       VARCHAR NOT NULL -- app / web / mini_program / offline -- 用户注册来源渠道
);

CREATE TABLE products ( -- 创建商品维度表，保存商品基础信息和成本价格
    product_id   BIGINT PRIMARY KEY, -- 商品唯一标识，用于订单明细和复购分析
    name         VARCHAR NOT NULL, -- 商品名称，便于报表展示和搜索
    category     VARCHAR NOT NULL, -- 商品大类，如服装、家电、食品等
    sub_category VARCHAR NOT NULL, -- 商品子类，用于更细粒度的品类分析
    price        DECIMAL(10,2) NOT NULL, -- 商品售价，保留两位小数用于订单金额计算
    cost         DECIMAL(10,2) NOT NULL -- 商品成本，便于毛利和利润率分析
);

CREATE TABLE orders ( -- 创建订单主表，保存订单核心信息和支付状态
    order_id        BIGINT PRIMARY KEY, -- 订单唯一标识，用于订单明细关联和查询
    customer_id     BIGINT    NOT NULL, -- 订单所属客户，关联 customers.customer_id
    order_date      DATE      NOT NULL, -- 下单日期，用于订单时间序列分析
    paid_at         TIMESTAMP, -- NULL = 未支付 -- 支付时间，未支付订单为空
    status          VARCHAR   NOT NULL, -- paid / shipped / completed / unpaid / cancelled -- 订单状态枚举，覆盖支付、发货、完成和取消
    channel         VARCHAR   NOT NULL, -- 订单渠道，如 app / web / mini_program / offline
    province        VARCHAR   NOT NULL, -- 下单省份，用于地域销售分析
    payment_method  VARCHAR   NOT NULL, -- 支付方式，如微信、支付宝、银行卡、现金等
    shipping_fee    DECIMAL(10,2) NOT NULL, -- 运费，参与订单总金额计算
    discount_amount DECIMAL(10,2) NOT NULL -- 折扣金额，便于分析促销策略效果
);

CREATE TABLE order_items ( -- 创建订单明细表，记录每个订单中的商品数量与单价
    order_id   BIGINT        NOT NULL, -- 订单主键，关联 orders.order_id
    product_id BIGINT        NOT NULL, -- 商品主键，关联 products.product_id
    quantity   INTEGER       NOT NULL, -- 购买数量，用于销量和复购分析
    unit_price DECIMAL(10,2) NOT NULL -- 商品单价，保留两位小数用于订单金额计算
);

CREATE TABLE refunds ( -- 创建退款表，记录订单退款明细和退款原因
    refund_id   BIGINT PRIMARY KEY, -- 退款唯一标识
    order_id    BIGINT        NOT NULL, -- 退款所属订单，关联 orders.order_id
    refund_date DATE          NOT NULL, -- 退款日期，用于退款时序分析
    amount      DECIMAL(10,2) NOT NULL, -- 退款金额，通常用于退款率和损失分析
    reason      VARCHAR       NOT NULL -- 退款原因，便于原因分类和业务问题排查
);

-- 元信息：DATA_VERSION / seed -- 元数据表记录数据集版本和初始化种子信息，供数据同步与回溯使用
CREATE TABLE dataset_meta ( -- 创建数据集元信息表，用于维护版本号和必要配置参数
    key   VARCHAR PRIMARY KEY, -- 元数据键名，如 DATA_VERSION、seed
    value VARCHAR NOT NULL -- 元数据键对应的值，存放版本号或初始化信息
);
