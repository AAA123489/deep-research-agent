#!/usr/bin/env python3
"""生成 ec_v1 演示数据集。

用法：
    python data/generate.py --out data/ecommerce.duckdb [--seed 20250101]

读参数 --out / --seed
     ↓
删掉旧库（保证每次从空开始）
     ↓
读 schema.sql，建 5 张空表
     ↓
依次调用 5 个生成函数，在内存里造出数据
     ↓
批量写进数据库
     ↓
写入 DATA_VERSION 和 seed，打印各表行数

"""
from __future__ import annotations  # 允许使用 Python 3.10+ 的类型注解语法，并兼容未来版本行为

import argparse  # 导入 argparse，用于解析命令行参数
import random  # 导入 random，用于随机生成模拟业务数据
import sys  # 导入 sys，用于控制标准输出流编码
from datetime import date, datetime, time as dtime, timedelta  # 导入日期时间工具，用于订单时间和支付时间生成
from decimal import Decimal  # 导入 Decimal，确保金额字段保留两位小数且无浮点误差
from pathlib import Path  # 导入 Path，用于安全处理输出路径和 SQL 文件路径

import duckdb  # 导入 DuckDB，用于创建和写入本地数据库
import pandas as pd  # 导入 pandas，用于 DataFrame 批量写入数据库

# Windows 终端默认 GBK，中文输出会乱码  # 说明：在 Windows 环境下需要强制重置输出流编码，否则中文日志会出现乱码
for _stream in (sys.stdout, sys.stderr):  # 遍历标准输出和标准错误流，统一设置为 UTF-8
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8")  # 兼容控制台的 UTF-8 输出，保证中文正常显示

DATA_VERSION = "ec_v1"  # 定义当前数据集版本，供元数据和文档引用
START = date(2025, 1, 1)  # 数据生成起始日期，固定从 2025-01-01 开始
END = date(2026, 9, 30)  # 数据生成终止日期，覆盖到 2026-09-30

# ---------------------------------------------------------------- 基础词表  # 这一段定义随机数据生成的基础词表和采样权重

SURNAMES = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严华金魏陶姜")  # 定义常见中文姓氏，用于生成客户姓名
GIVEN = list("伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超秀霞平刚桂英")  # 定义常见中文名字符，用于生成客户姓名的名字部分

PROVINCE_CITIES = {  # 省份到城市列表以及每个省份的采样权重，用于模拟真实地域分布
    "广东省": (["广州市", "深圳市", "东莞市", "佛山市"], 12),  # 广东省覆盖多个高消费城市，赋予较高权重
    "江苏省": (["南京市", "苏州市", "无锡市", "常州市"], 10),  # 江苏省为典型东部省份，消费水平偏高
    "浙江省": (["杭州市", "宁波市", "温州市", "金华市"], 10),  # 浙江省覆盖主要城市，消费活跃
    "山东省": (["济南市", "青岛市", "烟台市", "潍坊市"], 7),  # 山东省为重要制造和消费大省
    "四川省": (["成都市", "绵阳市", "南充市"], 6),  # 四川省消费规模中等且分布广泛
    "湖北省": (["武汉市", "宜昌市", "襄阳市"], 5),  # 湖北省作为中部典型省份
    "北京市": (["北京市"], 6),  # 北京市单独处理，表示核心城市
    "上海市": (["上海市"], 6),  # 上海市单独处理，表示一线城市
    "福建省": (["福州市", "厦门市", "泉州市"], 5),  # 福建省沿海城市密集，消费活跃
    "河南省": (["郑州市", "洛阳市", "南阳市"], 5),  # 河南省覆盖中原地区
}
PROVINCES = list(PROVINCE_CITIES)  # 提取省份名称列表，供随机抽样使用
PROVINCE_WEIGHTS = [v[1] for v in PROVINCE_CITIES.values()]  # 生成随机省份时使用的权重列表

CHANNELS = ["app", "web", "mini_program", "offline"]  # 渠道枚举，覆盖 app、web、小程序和线下端口
CHANNEL_WEIGHTS = [0.5, 0.25, 0.2, 0.05]  # 各渠道在真实场景中的出现概率，app 占比最高

PAYMENT_METHODS = ["wechat", "alipay", "bank_card", "cash"]  # 支付方式枚举，覆蓋微信、支付宝、银行卡和现金
PAYMENT_WEIGHTS = [0.45, 0.35, 0.15, 0.05]  # 根据使用习惯设定支付方式权重

# 85% 有效订单 + 10% 未支付 + 5% 已取消  # 这条注释说明订单状态分布符合真实电商业务比例
STATUSES = ["paid", "shipped", "completed", "unpaid", "cancelled"]  # 状态枚举，覆盖支付、发货、完成、未付款和取消
STATUS_WEIGHTS = [0.20, 0.20, 0.45, 0.10, 0.05]  # 状态分布设定：完成订单最多，其次为已支付和已发货

REFUND_REASONS = ["七天无理由", "质量问题", "发错货", "不喜欢", "物流太慢"]  # 退款理由列表，模拟真实退货和退款原因

# 品类 -> (子类, 价格区间)  # 这一段定义不同品类的子类以及售价范围，用于生成商品
CATEGORIES = {  # 商品品类字典，键为大类，值为子类和价格区间
    "服饰": (["T恤", "连衣裙", "外套", "裤装"], (49, 799)),  # 服饰品类覆盖上衣、裙装、外套和裤装
    "数码": (["手机", "耳机", "平板", "配件"], (99, 5999)),  # 数码品类广泛、单价高，适合高客单分析
    "家电": (["冰箱", "洗衣机", "小家电", "厨房电器"], (199, 4999)),  # 家电品类价格跨度大，适合毛利分析
    "美妆": (["面部护理", "彩妆", "香水", "个护"], (39, 899)),  # 美妆品类客单价中等，兼顾多品类需求
    "食品": (["零食", "饮料", "生鲜", "粮油"], (9, 299)),  # 食品品类覆盖小单价订单与频次型购买
    "家居": (["床上用品", "收纳", "厨具", "装饰"], (29, 1299)),  # 家居品类覆盖日用和装饰商品
    "运动": (["运动鞋", "健身器材", "户外", "运动服"], (59, 1999)),  # 运动品类包含鞋服和器材
    "母婴": (["奶粉", "纸尿裤", "玩具", "童装"], (19, 899)),  # 母婴品类需要覆盖婴幼儿消费场景
}

ITEMS_PER_ORDER = [1, 2, 3, 4, 5]  # 每单商品件数分布，覆盖小单和多件场景
ITEMS_WEIGHTS = [0.12, 0.28, 0.28, 0.20, 0.12]  # 平均每单约 2.9 件

# 品类销量权重：食品、服饰、美妆走量，数码、家电低频高价，用于把客单价拉回合理区间
CATEGORY_POPULARITY = {
    "食品": 0.28, "服饰": 0.25, "美妆": 0.18, "家居": 0.13,
    "母婴": 0.10, "运动": 0.03, "数码": 0.02, "家电": 0.01,
}


# ---------------------------------------------------------------- 工具  # 这一段提供通用函数，供后续生成逻辑复用

def money(x: float) -> Decimal:  # 将浮点数转成 Decimal 并保留 2 位小数，避免金额误差
    """统一定点两位小数，避免浮点误差进入 DECIMAL 列。"""
    return Decimal(f"{x:.2f}")  # 通过格式化字符串保留两位小数并返回 Decimal 类型


def day_weight(d: date) -> float:  # 根据日期返回该日订单权重，用于模拟促销日订单集中放大
    """大促加权：618 / 双 11 期间订单量放大。"""
    w = 1.0  # 初始权重为 1，表示普通日的基础订单量
    if d.month == 6 and 1 <= d.day <= 20:  # 6 月的 618 销售周期被赋予高权重
        w *= 3.0  # 618 活动期间订单量放大 3 倍
    if d.month == 11 and 1 <= d.day <= 15:  # 11 月双十一销售周期被赋予高权重
        w *= 4.0  # 双十一期间订单量放大 4 倍
    if d.year in (2025, 2026) and d.month == 6 and d.day == 18:  # 618 当天为大促核心日
        w *= 2.0  # 进一步加大当天订单规模
    if d.year == 2025 and d.month == 11 and d.day == 11:  # 2025 双十一当天为峰值购物日
        w *= 2.0  # 再次放大当天订单量
    return w  # 返回该日期的订单权重


def all_days() -> list[date]:  # 返回起止日期之间所有日期，用于生成订单和客户注册日期
    n = (END - START).days + 1  # 计算日期区间长度
    return [START + timedelta(days=i) for i in range(n)]  # 生成日期列表，便于按日抽样


# ---------------------------------------------------------------- 各表生成  # 这一段负责各张表的数据生成逻辑

def gen_customers(rng: random.Random, n: int) -> list[tuple]:  # 生成客户维度表，返回客户列表
    days = all_days()  # 读取全时间日期列表，便于为每个客户生成注册日期
    rows = []  # 初始化客户数据列表
    for i in range(1, n + 1):  # 循环生成指定数量的客户记录
        province = rng.choices(PROVINCES, weights=PROVINCE_WEIGHTS, k=1)[0]  # 随机抽取省份，遵从区域权重
        city = rng.choice(PROVINCE_CITIES[province][0])  # 从该省份城市列表中随机抽取城市
        rows.append((  # 追加一条客户记录，字段顺序符合 customers 表结构
            i,  # customer_id，客户唯一主键
            rng.choice(SURNAMES) + rng.choice(GIVEN) + rng.choice(GIVEN),  # 生成姓名，姓 + 两个名字字
            rng.choices(["male", "female", "unknown"], weights=[0.48, 0.48, 0.04])[0],  # 使用抽样权重生成性别
            rng.randint(18, 70),  # 年龄随机分布在 18 到 70 岁之间
            city,  # 城市名
            province,  # 省份名
            rng.choice(days),  # 注册日期随机选择一个日期
            rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS)[0],  # 注册渠道随机抽样
        ))
    return rows  # 返回生成完毕的客户列表


def gen_products(rng: random.Random) -> list[tuple]:  # 生成商品维度表，返回商品列表
    rows = []  # 初始化商品数据列表
    cats = list(CATEGORIES.items())  # 读取商品品类配置，用于循环生成商品
    for i in range(2000):  # 生成 2000 个商品样本，覆盖主流品类
        category, (subs, (lo, hi)) = cats[i % len(cats)]  # 按循环方式选定品类，并拿到子类和价格区间
        price = rng.uniform(lo, hi)  # 在该品类价格区间随机生成售价
        rows.append((  # 追加商品记录，字段顺序符合 products 表
            i + 1,  # product_id，商品主键
            f"{category}{rng.choice(subs)}{i + 1:04d}",  # 生成商品名称，如 服饰T恤0001
            category,  # 商品大类
            rng.choice(subs),  # 随机选择子类
            money(price),  # 保留两位小数的售价
            money(price * rng.uniform(0.4, 0.8)),  # 成本价按 40%~80% 比例生成，支持毛利分析
        ))
    return rows  # 返回生成完毕的商品列表


def gen_orders(rng: random.Random, n: int, customers: list[tuple]) -> list[tuple]:  # 生成订单主表，返回订单列表
    days = all_days()  # 获取全部日期列表，作为下单日期候选集
    weights = [day_weight(d) for d in days]  # 根据日期权重计算每一天的订单概率权重
    order_days = rng.choices(days, weights=weights, k=n)  # 依据权重随机抽取每个订单的下单日期

    # 20% 的客户贡献大部分复购：加权抽样制造客户分层  # 这段注释说明高活跃客户会更频繁下单
    loyal = list(range(len(customers) // 5))  # 取前 20% 客户作为忠诚客户群体，用于模拟复购行为

    rows = []  # 初始化订单数据列表
    for i in range(1, n + 1):  # 遍历生成指定数量的订单
        cid = customers[rng.choice(loyal)][0] if rng.random() < 0.7 else rng.choice(customers)[0]  # 70% 使用忠诚客户，30% 使用普通客户
        od = order_days[i - 1]  # 当前订单的下单日期
        status = rng.choices(STATUSES, weights=STATUS_WEIGHTS)[0]  # 根据权重随机抽取订单状态

        paid_at = None  # 默认支付时间为空，表示订单尚未支付或无需支付
        if status in ("paid", "shipped", "completed"):  # 对已支付状态生成支付时间
            paid_at = datetime.combine(  # 组合日期和随机时分秒，生成支付时间戳
                od, dtime(rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59))
            )

        province = customers[cid - 1][5] if rng.random() < 0.9 else rng.choice(PROVINCES)  # 90% 按客户省份下单，10% 允许跨省下单
        shipping = 0.0 if rng.random() < 0.4 else rng.uniform(5, 20)  # 40% 订单免运费，否则随机生成运费
        discount = 0.0 if rng.random() < 0.5 else rng.uniform(0, 50)  # 50% 无折扣，否则随机生成折扣金额

        rows.append((  # 追加一条订单记录，字段顺序符合 orders 表
            i,  # order_id，订单唯一主键
            cid,  # customer_id，客户编号，关联 customers
            od,  # order_date，下单日期
            paid_at,  # 支付时间，可能为空
            status,  # 当前订单状态
            rng.choices(CHANNELS, weights=CHANNEL_WEIGHTS)[0],  # 下单渠道
            province,  # 下单省份
            rng.choices(PAYMENT_METHODS, weights=PAYMENT_WEIGHTS)[0],  # 支付方式
            money(shipping),  # 运费保留两位小数
            money(discount),  # 折扣金额保留两位小数
        ))
    return rows  # 返回生成完毕的订单列表


def gen_order_items(
    rng: random.Random, orders: list[tuple], products: list[tuple]
) -> tuple[list[tuple], dict[int, Decimal]]:  # 生成订单明细，并返回每单成交额，供退款生成使用
    """只为有效订单生成明细，同时返回每单成交额（退款要用）。"""
    rows = []  # 初始化订单明细列表
    totals: dict[int, Decimal] = {}  # key 为 order_id，value 为该订单总金额
    prod_weights = [CATEGORY_POPULARITY[p[2]] for p in products]  # 每个商品的抽样权重，按品类销量倾斜
    for order in orders:  # 遍历每一条订单，生成对应明细
        order_id, paid_at = order[0], order[3]  # 获取订单编号和支付时间
        if paid_at is None:  # 未支付订单不生成明细，避免无效成交记录
            continue
        total = Decimal("0.00")  # 订单明细小计初始化为 0.00
        for _ in range(rng.choices(ITEMS_PER_ORDER, weights=ITEMS_WEIGHTS)[0]):  # 每单生成随机件数的商品项
            product = rng.choices(products, weights=prod_weights)[0]  # 按品类销量权重选择商品
            unit_price = money(float(product[4]) * rng.uniform(0.7, 1.0))  # 按商品标价打折，模拟真实销售单价
            qty = rng.choices([1, 2, 3], weights=[0.6, 0.3, 0.1])[0]  # 每项商品购买 1~3 个，平均 1.5
            rows.append((order_id, product[0], qty, unit_price))  # 追加订单明细记录：订单号、商品号、数量、单价
            total += unit_price * qty  # 累加订单总金额
        totals[order_id] = total  # 记住每单金额，供退款计算使用
    return rows, totals  # 返回明细列表和每单成交额映射


def gen_refunds(
    rng: random.Random,
    orders: list[tuple],
    totals: dict[int, Decimal],
    target: int,
) -> list[tuple]:  # 生成退款表，模拟真实退款事件
    latest_paid = date(2026, 8, 31)  # 留出 30 天退款窗口，避免退款事件越界  # 退款窗口的截止日期为 2026-08-31
    eligible = [  # 筛选有效退款候选：订单已支付、支付日期在窗口内且存在成交额
        (o[0], o[3]) for o in orders
        if o[3] is not None and o[3].date() <= latest_paid and totals.get(o[0], 0) > 0
    ]
    rng.shuffle(eligible)  # 随机打乱候选订单，避免退款从同一批客户集中出现
    picked = eligible[:target]  # 从候选池中抽取 target 条记录作为退款样本

    rows = []  # 初始化退款列表
    for i, (order_id, paid_at) in enumerate(picked, start=1):  # 按抽样顺序生成退款记录
        total = totals[order_id]  # 获取该订单的成交总额
        refund_date = paid_at.date() + timedelta(days=rng.randint(1, 30))  # 退款发生在支付后 1~30 天内
        if refund_date > END:  # 若退款日期超过数据结束时间，则截断到结束日
            refund_date = END
        amount = max(Decimal("0.01"), money(float(total) * rng.uniform(0.1, 1.0)))  # 退款金额为订单金额的 10%~100%，最低 0.01
        rows.append((i, order_id, refund_date, amount, rng.choice(REFUND_REASONS)))  # 追加退款记录：退款号、订单号、退款日期、金额、原因
    return rows  # 返回生成完毕的退款列表


# ---------------------------------------------------------------- 主流程  # 这一段负责执行完整的数据生成过程，连接数据库并写入各表

def bulk_insert(con, table: str, columns: list[str], rows: list[tuple]) -> None:  # 使用 DataFrame 批量写入，提升写入效率
    """DataFrame 批量装载。DuckDB 的 executemany 逐行提交，几十万行会慢到不能用。"""
    df = pd.DataFrame(rows, columns=columns)  # 将列表转成 DataFrame，便于一次性批量写入
    con.register("_tmp_df", df)  # 把 DataFrame 注册成临时表，便于使用 SQL 语句导入
    con.execute(f"INSERT INTO {table} SELECT * FROM _tmp_df")  # 批量插入到目标表
    con.unregister("_tmp_df")  # 删除临时视图，避免后续冲突


def main() -> None:  # 主函数，执行脚本主体逻辑
    ap = argparse.ArgumentParser(description="生成 ec_v1 演示数据集")  # 创建命令行参数解析器，说明脚本用途
    ap.add_argument("--out", default="data/ecommerce.duckdb")  # 定义输出数据库路径参数，默认输出到 data/ecommerce.duckdb
    ap.add_argument("--seed", type=int, default=20250101)  # 定义随机种子参数，保证结果可复现
    args = ap.parse_args()  # 解析命令行参数

    out = Path(args.out)  # 将输出路径转换成 Path 对象
    out.parent.mkdir(parents=True, exist_ok=True)  # 创建输出目录，确保目录存在
    if out.exists():  # 如果旧数据库文件存在则移除，避免脏数据影响
        out.unlink()  # 删除旧文件，确保从空库开始生成

    schema_sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")  # 读取 schema.sql 建表语句
    con = duckdb.connect(str(out))  # 连接目标数据库文件
    con.execute(schema_sql)  # 执行 schema.sql 创建表结构

    customers = gen_customers(random.Random(f"{args.seed}:customers"), 20_000)  # 生成 20,000 个客户
    products = gen_products(random.Random(f"{args.seed}:products"))  # 生成 2,000 个商品
    orders = gen_orders(random.Random(f"{args.seed}:orders"), 100_000, customers)  # 生成 100,000 个订单
    items, totals = gen_order_items(random.Random(f"{args.seed}:items"), orders, products)  # 生成订单明细并累计金额
    refunds = gen_refunds(random.Random(f"{args.seed}:refunds"), orders, totals, 15_000)  # 生成 15,000 条退款

    bulk_insert(con, "customers",  # 批量插入 customers 表
                ["customer_id", "name", "gender", "age", "city", "province",
                 "register_date", "channel"], customers)  # 指定字段顺序和客户数据
    bulk_insert(con, "products",  # 批量插入 products 表
                ["product_id", "name", "category", "sub_category", "price", "cost"], products)  # 指定字段顺序和商品数据
    bulk_insert(con, "orders",  # 批量插入 orders 表
                ["order_id", "customer_id", "order_date", "paid_at", "status",
                 "channel", "province", "payment_method", "shipping_fee",
                 "discount_amount"], orders)  # 指定字段顺序和订单数据
    bulk_insert(con, "order_items",  # 批量插入 order_items 表
                ["order_id", "product_id", "quantity", "unit_price"], items)  # 指定字段顺序和订单明细数据
    bulk_insert(con, "refunds",  # 批量插入 refunds 表
                ["refund_id", "order_id", "refund_date", "amount", "reason"], refunds)  # 指定字段顺序和退款数据
    con.execute(  # 记录数据版本和随机种子到 dataset_meta 中
        "INSERT INTO dataset_meta VALUES ('DATA_VERSION', ?), ('seed', ?)",  # SQL 插入语句，用于写入版本与种子信息
        [DATA_VERSION, str(args.seed)],  # 传入数据版本和种子值
    )

    for table in ("customers", "products", "orders", "order_items", "refunds"):  # 遍历主要业务表，检查各自插入条数
        n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # 执行 COUNT(*) 查询获取记录数
        print(f"{table:14} {n:>8,}")  # 按固定宽度输出每张表的记录数，便于观察生成规模
    con.close()  # 关闭数据库连接，释放资源
    print(f"写入 {out}")  # 输出数据文件保存位置，方便确认输出结果


if __name__ == "__main__":  # 只有脚本作为主程序执行时才运行 main
    main()  # 调用主函数，启动完整的数据生成流程
