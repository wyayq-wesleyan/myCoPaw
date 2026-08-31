#!/usr/bin/env python3
"""生成4类测试数据CSV — 用于验证多表数据独立性"""
import csv
import os
import random
from datetime import datetime, timedelta

random.seed(42)
OUT = os.path.join(os.path.dirname(__file__), "test_files")
os.makedirs(OUT, exist_ok=True)

# ═══════════════════════════════════════════
# 表1: 销售订单 — 日期+金额+数量+布尔+文本
# ═══════════════════════════════════════════
CUSTOMERS = ["华为技术有限公司", "阿里巴巴集团", "腾讯科技有限公司", "字节跳动有限公司", "小米集团",
             "京东集团", "美团科技有限公司", "百度在线", "网易杭州", "拼多多有限公司"]
PRODUCTS_ORDERS = ["云服务器ECS", "数据库RDS", "对象存储OSS", "CDN加速包", "安全防护服务",
                   "大数据分析平台", "AI推理服务", "容器服务K8s", "消息队列", "视频会议系统"]

with open(f"{OUT}/销售订单.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["订单编号", "下单日期", "客户名称", "产品名称", "销售数量", "单价金额", "是否含税", "备注说明"])
    for i in range(1, 81):
        qty = random.randint(1, 200)
        price = round(random.uniform(50, 15000), 2)
        date = (datetime(2024, 1, 1) + timedelta(days=random.randint(0, 365)))
        has_tax = random.choice(["是", "否"])
        note = random.choice([
            "急单优先处理", "长期合作协议客户", "新客户首单优惠",
            "季度返利客户", "政府项目采购", "教育行业折扣",
            "续约客户", "大客户定制方案", "", ""
        ])
        w.writerow([
            f"SO-2024-{i:04d}",
            date.strftime("%Y-%m-%d"),
            random.choice(CUSTOMERS),
            random.choice(PRODUCTS_ORDERS),
            qty,
            price,
            has_tax,
            note,
        ])
print(f"  销售订单.csv: 80 rows")

# ═══════════════════════════════════════════
# 表2: 员工花名册 — 文本+日期+数字+枚举
# ═══════════════════════════════════════════
SURNAMES = ["张", "李", "王", "赵", "刘", "陈", "杨", "黄", "周", "吴", "徐", "孙", "马", "朱", "胡", "林", "郭", "何"]
NAMES_CHAR = ["伟", "芳", "娜", "强", "磊", "洋", "艳", "勇", "军", "杰", "敏", "静", "丽", "涛", "明", "超", "秀英", "建华"]
DEPTS = ["研发部", "市场部", "财务部", "人事部", "运维部", "产品部", "法务部", "销售部"]
LEVELS = ["P4-初级", "P5-中级", "P6-高级", "P7-专家", "P8-资深专家", "M1-经理", "M2-总监"]

with open(f"{OUT}/员工花名册.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["工号", "姓名", "性别", "所属部门", "职级", "入职日期", "月薪", "是否在职"])
    for i in range(1, 61):
        name = random.choice(SURNAMES) + random.choice(NAMES_CHAR)
        gender = random.choice(["男", "女"])
        dept = random.choice(DEPTS)
        level = random.choice(LEVELS)
        hire_date = (datetime(2016, 1, 1) + timedelta(days=random.randint(0, 3000)))
        salary = random.randint(8000, 55000)
        active = random.choices(["是", "否"], weights=[85, 15])[0]
        w.writerow([
            f"EMP-{i:05d}",
            name,
            gender,
            dept,
            level,
            hire_date.strftime("%Y-%m-%d"),
            salary,
            active,
        ])
print(f"  员工花名册.csv: 60 rows")

# ═══════════════════════════════════════════
# 表3: 产品目录 — 文本+数字+布尔+长文本
# ═══════════════════════════════════════════
CATEGORIES = ["云计算", "数据库", "存储", "网络", "安全", "大数据", "人工智能", "物联网"]
PRODUCT_NAMES = [
    ("ECS-云服务器", "弹性计算服务，支持多种实例规格"),
    ("RDS-关系型数据库", "MySQL/PostgreSQL/SQL Server 托管服务"),
    ("OSS-对象存储", "海量文件存储，支持图片视频等多媒体"),
    ("CDN-内容分发", "全球加速节点，降低访问延迟"),
    ("WAF-Web防火墙", "防护SQL注入、XSS等常见攻击"),
    ("MaxCompute-大数据", "PB级数据仓库解决方案"),
    ("PAI-机器学习", "一站式AI开发平台"),
    ("IoT-物联网平台", "设备接入、管理和数据采集"),
    ("SLB-负载均衡", "高可用流量分发服务"),
    ("VPC-专有网络", "隔离的网络环境，保障安全"),
    ("RAM-访问控制", "精细化权限管理"),
    ("KMS-密钥管理", "数据加密和密钥托管"),
]

with open(f"{OUT}/产品目录.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["产品编号", "产品名称", "产品分类", "单价", "库存数量", "是否上架", "产品描述"])
    for i, (pname, desc) in enumerate(PRODUCT_NAMES, 1):
        cat = random.choice(CATEGORIES)
        price = round(random.uniform(0.01, 9999.99), 2)
        stock = random.randint(0, 100000)
        on_sale = random.choices(["是", "否"], weights=[80, 20])[0]
        w.writerow([
            f"PROD-{i:04d}",
            pname,
            cat,
            price,
            stock,
            on_sale,
            desc,
        ])
print(f"  产品目录.csv: {len(PRODUCT_NAMES)} rows")

# ═══════════════════════════════════════════
# 表4: 库存台账 — 日期+数字+文本+数字
# ═══════════════════════════════════════════
WAREHOUSES = ["华东仓", "华南仓", "华北仓", "西南仓"]
CHANGE_TYPES = ["入库", "出库", "调拨", "盘点"]

with open(f"{OUT}/库存台账.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["变动日期", "产品编号", "产品名称", "变动类型", "变动数量", "单价", "库存余额", "仓库"])
    balance = 5000
    for i in range(1, 101):
        prod = random.choice(PRODUCT_NAMES)
        prod_name = prod[0].split("-")[1]
        prod_code = f"PROD-{random.randint(1,12):04d}"
        change_type = random.choice(CHANGE_TYPES)
        change_qty = random.randint(10, 500)
        unit_price = round(random.uniform(50, 5000), 2)
        if change_type == "入库":
            balance += change_qty
        elif change_type == "出库":
            balance = max(100, balance - change_qty)
        elif change_type == "调拨":
            balance = max(100, balance - change_qty // 2)
        # 盘点不改变余额
        date = (datetime(2024, 1, 1) + timedelta(days=random.randint(0, 365)))
        w.writerow([
            date.strftime("%Y-%m-%d"),
            prod_code,
            prod_name,
            change_type,
            change_qty if change_type != "盘点" else 0,
            unit_price,
            balance,
            random.choice(WAREHOUSES),
        ])
print(f"  库存台账.csv: 100 rows")

print(f"\n生成完毕: {OUT}/")
for fn in sorted(os.listdir(OUT)):
    size = os.path.getsize(f"{OUT}/{fn}")
    print(f"  {fn}: {size:,} bytes")
