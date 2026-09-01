#!/usr/bin/env python3
"""生成数据分析模块的模拟业务数据 CSV 文件。

设计目标：
- 12 张业务表，跨表主外键关联（客户、产品、供应商、销售订单等）
- 总行数约 131 万
- 中文表头 / 中文业务值，贴合真实使用
- 日期、金额、数量、布尔、文本等类型混合

用法：
    python3 gen_data.py --out /tmp/mycopaw-seed --rows-scale 1
    # --rows-scale 可放大/缩小行数,默认 1(百万级)
"""
import argparse
import csv
import os
import random
from datetime import datetime, timedelta

random.seed(42)

def _slug_subtable(slug: str, idx: int) -> str:
    return f"{slug}_sub{idx}"

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def w(path: str, rows: list[list], header: list[str]) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(header)
        wr.writerows(rows)

def gen_customers(n: int, scale: float, out: str):
    """客户信息表：id, 客户名称, 客户类型, 行业, 所在区域, 信用等级, 成立日期"""
    cities = ["北京", "上海", "广州", "深圳", "杭州", "成都", "南京", "武汉", "西安", "天津",
              "苏州", "重庆", "长沙", "青岛", "郑州", "合肥", "宁波", "福州", "济南", "佛山"]
    industries = ["信息技术", "金融", "制造业", "零售", "医疗健康", "教育", "能源", "物流", "建筑", "汽车"]
    types = ["大型企业", "中型企业", "小微企业", "政府机构", "事业单位"]
    credits = ["AAA", "AA", "A", "BBB", "BB", "B"]
    header = ["客户ID", "客户名称", "客户类型", "所属行业", "所在区域", "信用等级", "成立日期"]
    rows = []
    for i in range(1, n + 1):
        city = random.choice(cities)
        rows.append([
            f"C{i:07d}",
            f"{random.choice(['华宇','中原','长江','万里','启程','博远','凯盛','恒信','瑞达','泰和','国联','伟业'])}{random.choice(['科技','实业','贸易','集团','电子','医药','物流','能源','机械','发展'])}{city[0]}公司{i}",
            random.choice(types),
            random.choice(industries),
            city,
            random.choice(credits),
            (datetime(1990, 1, 1) + timedelta(days=random.randint(0, 11000))).strftime("%Y-%m-%d"),
        ])
    w(os.path.join(out, "客户信息表.csv"), rows, header)

def gen_products(n: int, scale: float, out: str):
    """产品目录表：id, 名称, 分类, 单价, 成本, 计量单位, 上下架状态"""
    header = ["产品ID", "产品名称", "产品分类", "单价", "成本", "计量单位", "上架状态"]
    cats = ["电子产品", "办公用品", "食品饮料", "家居建材", "服装鞋帽", "药品器械",
            "工业原料", "日化用品", "图书影音", "运动器材"]
    units = ["件", "箱", "千克", "个", "套", "台", "吨", "瓶"]
    rows = []
    for i in range(1, n + 1):
        price = round(random.uniform(9.9, 99999), 2)
        rows.append([
            f"P{i:07d}",
            f"{random.choice(['智能','超值','经典','尊享','轻奢','标准','豪华','节能','便携','专业'])}{random.choice(['系列','型号','升级版','旗舰款','PRO','MAX'])}{random.choice(cats)}",
            random.choice(cats),
            f"{price:.2f}",
            f"{price * random.uniform(0.4, 0.75):.2f}",
            random.choice(units),
            random.choice(["在售", "下架", "预售"]),
        ])
    w(os.path.join(out, "产品目录表.csv"), rows, header)

def gen_sales_orders(n: int, scale: float, out: str, customer_n: int, product_n: int):
    """销售订单表：订单ID, 客户ID, 产品ID, 数量, 单价, 金额, 订单日期, 状态"""
    header = ["订单ID", "客户ID", "产品ID", "数量", "单价", "金额", "订单日期", "订单状态"]
    statuses = ["已完成", "待发货", "已取消", "配送中", "已退款", "已完成"]
    rows = []
    base = datetime(2022, 1, 1)
    for i in range(1, n + 1):
        qty = random.randint(1, 500)
        price = round(random.uniform(9.9, 99999), 2)
        rows.append([
            f"O{i:09d}",
            f"C{random.randint(1, customer_n):07d}",
            f"P{random.randint(1, product_n):07d}",
            qty,
            f"{price:.2f}",
            f"{qty * price:.2f}",
            (base + timedelta(days=random.randint(0, 1100))).strftime("%Y-%m-%d"),
            random.choice(statuses),
        ])
    w(os.path.join(out, "销售订单表.csv"), rows, header)

def gen_inventory(n: int, scale: float, out: str, product_n: int):
    """库存流水表：流水ID, 产品ID, 仓库, 变动类型, 变动数量, 库存余额, 变动日期"""
    header = ["流水ID", "产品ID", "仓库名称", "变动类型", "变动数量", "变动后余额", "变动日期"]
    warehouses = ["华东仓", "华南仓", "华北仓", "西南仓", "华中仓", "东北仓"]
    types = ["入库", "出库", "调拨入库", "调拨出库", "报废", "盘点调整"]
    rows = []
    for i in range(1, n + 1):
        ct = random.choice(types)
        rows.append([
            f"I{i:09d}",
            f"P{random.randint(1, product_n):07d}",
            random.choice(warehouses),
            ct,
            random.randint(1, 2000),
            random.randint(1, 30000),
            (datetime(2023, 1, 1) + timedelta(days=random.randint(0, 985))).strftime("%Y-%m-%d"),
        ])
    w(os.path.join(out, "库存流水表.csv"), rows, header)

def gen_employees(n: int, scale: float, out: str):
    """员工花名册：工号, 姓名, 部门, 职级, 入职日期, 月薪, 城市, 在职状态"""
    header = ["工号", "姓名", "部门", "职级", "入职日期", "月薪", "所在城市", "在职状态"]
    depts = ["研发部", "市场部", "销售部", "人事部", "财务部", "运营部", "客服部", "法务部", "行政部", "产品部"]
    levels = ["专员", "主管", "经理", "总监", "副总", "总裁"]
    cities = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安"]
    surnames = ["王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴", "徐", "孙", "马", "朱", "胡", "郭", "何", "林", "罗", "郑"]
    given = ["伟", "芳", "敏", "静", "磊", "军", "洋", "勇", "艳", "杰", "涛", "明", "超", "霞", "平", "刚", "桂英", "建华", "文博", "子涵"]
    rows = []
    for i in range(1, n + 1):
        rows.append([
            f"E{i:06d}",
            random.choice(surnames) + random.choice(given),
            random.choice(depts),
            random.choice(levels),
            (datetime(2005, 1, 1) + timedelta(days=random.randint(0, 7000))).strftime("%Y-%m-%d"),
            round(random.uniform(5000, 60000), 2),
            random.choice(cities),
            random.choice(["在职", "在职", "在职", "离职"]),
        ])
    w(os.path.join(out, "员工花名册表.csv"), rows, header)

def gen_suppliers(n: int, scale: float, out: str):
    """供应商信息表：供应商ID, 名称, 供货类别, 所在城市, 合作状态, 评分"""
    header = ["供应商ID", "供应商名称", "供货类别", "所在城市", "合作状态", "综合评分"]
    cats = ["电子元器件", "原材料", "包装耗材", "办公设备", "物流服务", "软件服务"]
    cities = ["苏州", "东莞", "宁波", "青岛", "佛山", "天津", "无锡", "常州"]
    rows = []
    for i in range(1, n + 1):
        rows.append([
            f"S{i:06d}",
            f"{random.choice(['宏达','德润','嘉禾','永盛','华越','鑫源','天工','凯捷','联创','三星','精工','恒力'])}供应链有限公司",
            random.choice(cats),
            random.choice(cities),
            random.choice(["合作中", "合作中", "暂停合作", "已终止"]),
            round(random.uniform(3.0, 5.0), 1),
        ])
    w(os.path.join(out, "供应商信息表.csv"), rows, header)

def gen_marketing(n: int, scale: float, out: str):
    """营销活动表：活动ID, 活动名称, 投放渠道, 投入金额, 带来流量, 转化客户数, 开始日期, 结束日期"""
    header = ["活动ID", "活动名称", "投放渠道", "投入金额", "带来流量", "转化客户数", "开始日期", "结束日期"]
    channels = ["线上广告", "线下展会", "新媒体种草", "电话营销", "电商促销", "联盟合作"]
    rows = []
    for i in range(1, n + 1):
        start = datetime(2023, 1, 1) + timedelta(days=random.randint(0, 960))
        dur = random.randint(3, 90)
        rows.append([
            f"M{i:08d}",
            f"{random.choice(['春季','夏季','秋季','冬季','年中','年终','618','双11','新品','周年'])}大促活动{i}",
            random.choice(channels),
            round(random.uniform(5000, 2000000), 2),
            random.randint(1000, 2000000),
            random.randint(10, 50000),
            start.strftime("%Y-%m-%d"),
            (start + timedelta(days=dur)).strftime("%Y-%m-%d"),
        ])
    w(os.path.join(out, "营销活动表.csv"), rows, header)

def gen_finance(n: int, scale: float, out: str):
    """财务报表表：记录ID, 月份, 营业收入, 成本支出, 净利润, 现金流, 客户数"""
    header = ["记录ID", "财务月份", "营业收入", "成本支出", "净利润", "经营活动现金流", "活跃客户数"]
    rows = []
    for i in range(1, n + 1):
        rev = round(random.uniform(100000, 50000000), 2)
        cost = rev * random.uniform(0.5, 0.9)
        m = 2022 + random.randint(0, 3)
        mm = random.randint(1, 12)
        rows.append([
            f"F{i:09d}",
            f"{m}-{mm:02d}",
            f"{rev:.2f}",
            f"{cost:.2f}",
            f"{rev - cost:.2f}",
            f"{(rev - cost) * random.uniform(0.5, 1.5):.2f}",
            random.randint(100, 50000),
        ])
    w(os.path.join(out, "财务报表表.csv"), rows, header)

def gen_logistics(n: int, scale: float, out: str, order_n: int):
    """物流订单表：物流单号, 订单ID, 承运商, 发货地, 收货地, 货物重量, 运费, 发货日期, 状态"""
    header = ["物流单号", "订单ID", "承运商", "发货城市", "收货城市", "货物重量", "运费", "发货日期", "物流状态"]
    carriers = ["顺丰速运", "中通快递", "圆通速递", "申通快递", "韵达快递", "京东物流", "德邦物流"]
    cities = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "长沙", "南京", "天津", "重庆"]
    statuses = ["已签收", "运输中", "待揽收", "已退回", "已签收"]
    rows = []
    for i in range(1, n + 1):
        rows.append([
            f"L{i:09d}",
            f"O{random.randint(1, order_n):09d}",
            random.choice(carriers),
            random.choice(cities),
            random.choice(cities),
            round(random.uniform(0.1, 500), 2),
            round(random.uniform(5, 5000), 2),
            (datetime(2022, 1, 1) + timedelta(days=random.randint(0, 1100))).strftime("%Y-%m-%d"),
            random.choice(statuses),
        ])
    w(os.path.join(out, "物流订单表.csv"), rows, header)

def gen_channel_sales(n: int, scale: float, out: str, product_n: int):
    """渠道销售表：记录ID, 产品ID, 销售渠道, 区域, 销售量, 销售额, 月份"""
    header = ["记录ID", "产品ID", "销售渠道", "销售区域", "销售量", "销售额", "统计月份"]
    channels = ["电商平台", "直营门店", "经销商", "批发市场", "海外渠道", "企业客户"]
    regions = ["华东", "华南", "华北", "华中", "西南", "西北", "东北"]
    rows = []
    for i in range(1, n + 1):
        qty = random.randint(1, 3000)
        price = round(random.uniform(10, 3000), 2)
        m = 2022 + random.randint(0, 3)
        mm = random.randint(1, 12)
        rows.append([
            f"CS{i:09d}",
            f"P{random.randint(1, product_n):07d}",
            random.choice(channels),
            random.choice(regions),
            qty,
            f"{qty * price:.2f}",
            f"{m}-{mm:02d}",
        ])
    w(os.path.join(out, "渠道销售表.csv"), rows, header)

def gen_purchase(n: int, scale: float, out: str, supplier_n: int, product_n: int):
    """采购订单表：采购单号, 供应商ID, 产品ID, 数量, 单价, 金额, 下单日期, 状态"""
    header = ["采购单号", "供应商ID", "产品ID", "数量", "单价", "金额", "下单日期", "状态"]
    statuses = ["已收货", "待收货", "部分收货", "已取消", "待审批"]
    rows = []
    for i in range(1, n + 1):
        qty = random.randint(10, 2000)
        price = round(random.uniform(5, 5000), 2)
        rows.append([
            f"PO{i:08d}",
            f"S{random.randint(1, supplier_n):06d}",
            f"P{random.randint(1, product_n):07d}",
            qty,
            f"{price:.2f}",
            f"{qty * price:.2f}",
            (datetime(2023, 1, 1) + timedelta(days=random.randint(0, 985))).strftime("%Y-%m-%d"),
            random.choice(statuses),
        ])
    w(os.path.join(out, "采购订单表.csv"), rows, header)

def gen_machines(n: int, scale: float, out: str):
    """机器运行表：记录ID, 设备编号, 设备类型, 温度, 转速, 运行时长, 负载率, 采集时间, 健康状态"""
    header = ["记录ID", "设备编号", "设备类型", "运行温度", "转速", "运行时长", "负载率", "采集时间", "健康状态"]
    types = ["立式加工中心", "数控车床", "注塑机", "冲压机", "真空泵", "工业机器人"]
    statuses = ["正常", "正常", "异常", "警告"]
    rows = []
    for i in range(1, n + 1):
        rows.append([
            f"MC{i:09d}",
            f"EQ{random.randint(1, 2000):05d}",
            random.choice(types),
            round(random.uniform(30, 95), 1),
            random.randint(500, 20000),
            round(random.uniform(1, 7200), 1),
            f"{random.uniform(10, 100):.1f}%",
            (datetime(2024, 1, 1) + timedelta(minutes=random.randint(0, 300 * 24 * 60))).strftime("%Y-%m-%d %H:%M:%S"),
            random.choice(statuses),
        ])
    w(os.path.join(out, "机器运行表.csv"), rows, header)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/mycopaw-seed", help="输出目录")
    ap.add_argument("--scale", type=float, default=1.0, help="行数缩放因子")
    ap.add_argument("--customer-n", type=int, default=60000)
    ap.add_argument("--product-n", type=int, default=25000)
    ap.add_argument("--supplier-n", type=int, default=12000)
    ap.add_argument("--sales-n", type=int, default=300000)
    ap.add_argument("--inventory-n", type=int, default=300000)
    ap.add_argument("--employee-n", type=int, default=40000)
    ap.add_argument("--marketing-n", type=int, default=80000)
    ap.add_argument("--finance-n", type=int, default=200000)
    ap.add_argument("--logistics-n", type=int, default=180000)
    ap.add_argument("--channel-n", type=int, default=150000)
    ap.add_argument("--purchase-n", type=int, default=90000)
    ap.add_argument("--machine-n", type=int, default=100000)
    args = ap.parse_args()

    out = args.out
    ensure_dir(out)
    s = args.scale
    def sc(n): return max(1, int(n * s))

    print("生成客户信息表...")
    gen_customers(sc(args.customer_n), s, out)
    print("生成产品目录表...")
    gen_products(sc(args.product_n), s, out)
    print("生成销售订单表...")
    gen_sales_orders(sc(args.sales_n), s, out, sc(args.customer_n), sc(args.product_n))
    print("生成库存流水表...")
    gen_inventory(sc(args.inventory_n), s, out, sc(args.product_n))
    print("生成员工花名册表...")
    gen_employees(sc(args.employee_n), s, out)
    print("生成供应商信息表...")
    gen_suppliers(sc(args.supplier_n), s, out)
    print("生成营销活动表...")
    gen_marketing(sc(args.marketing_n), s, out)
    print("生成财务报表表...")
    gen_finance(sc(args.finance_n), s, out)
    print("生成物流订单表...")
    gen_logistics(sc(args.logistics_n), s, out, sc(args.sales_n))
    print("生成渠道销售表...")
    gen_channel_sales(sc(args.channel_n), s, out, sc(args.product_n))
    print("生成采购订单表...")
    gen_purchase(sc(args.purchase_n), s, out, sc(args.supplier_n), sc(args.product_n))
    print("生成机器运行表...")
    gen_machines(sc(args.machine_n), s, out)

    total = (sc(args.customer_n) + sc(args.product_n) + sc(args.supplier_n) + sc(args.sales_n)
             + sc(args.inventory_n) + sc(args.employee_n) + sc(args.marketing_n) + sc(args.finance_n)
             + sc(args.logistics_n) + sc(args.channel_n) + sc(args.purchase_n) + sc(args.machine_n))
    print(f"\n完成！共生成 {total} 行数据到 {out}")

if __name__ == "__main__":
    main()
