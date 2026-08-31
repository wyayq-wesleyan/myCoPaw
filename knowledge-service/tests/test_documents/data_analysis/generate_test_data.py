#!/usr/bin/env python3
"""generate_test_data.py - 生成数据分析模块测试文件"""

import csv
import os
import random
from datetime import datetime, timedelta

OUT = os.path.join(os.path.dirname(__file__), "test_files")
os.makedirs(OUT, exist_ok=True)

random.seed(42)

# === 1. 销售订单_正常.csv (UTF-8, 50 rows) ===
with open(f"{OUT}/销售订单_正常.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["订单编号", "客户名称", "产品名称", "数量", "单价", "订单日期"])
    for i in range(1, 51):
        w.writerow([
            f"ORD-{i:04d}",
            random.choice(["华为技术有限公司", "阿里巴巴集团", "腾讯科技", "字节跳动", "小米集团"]),
            random.choice(["云服务器ECS", "数据库RDS", "对象存储OSS", "CDN加速", "安全防护"]),
            random.randint(1, 100),
            round(random.uniform(100, 10000), 2),
            (datetime(2024, 1, 1) + timedelta(days=random.randint(0, 365))).strftime("%Y-%m-%d"),
        ])

# === 2. 销售订单_GBK.csv (GBK encoding, 20 rows) ===
with open(f"{OUT}/销售订单_GBK.csv", "w", encoding="gbk", newline="") as f:
    w = csv.writer(f)
    w.writerow(["订单编号", "客户名称", "产品名称", "数量", "单价", "订单日期"])
    for i in range(1, 21):
        w.writerow([
            f"GBK-{i:04d}",
            random.choice(["中国移动", "中国电信", "中国联通"]),
            random.choice(["5G套餐", "光纤宽带", "企业专线"]),
            random.randint(1, 50),
            round(random.uniform(50, 5000), 2),
            (datetime(2024, 6, 1) + timedelta(days=random.randint(0, 180))).strftime("%Y/%m/%d"),
        ])

# === 3. 产品目录_类型混合.csv (5 columns, 30 rows) ===
with open(f"{OUT}/产品目录_类型混合.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["产品ID", "产品名称", "价格", "库存数量", "是否上架"])
    products = [
        ("P001", "云服务器ECS", "299.99", "1000", "是"),
        ("P002", "数据库RDS", "599.00", "500", "是"),
        ("P003", "对象存储OSS", "0.12", "999999", "是"),
        ("P004", "测试产品A", "10.50", "0", "否"),
        ("P005", "测试产品B", "9999.99", "1", "是"),
    ]
    for pid, name, price, stock, on_sale in products:
        for _ in range(6):
            w.writerow([pid, name, price, stock, on_sale])

# === 4. 员工信息_大文件.csv (4 columns, 5000 rows) ===
with open(f"{OUT}/员工信息_大文件.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["工号", "姓名", "部门", "入职日期"])
    depts = ["研发部", "市场部", "财务部", "人事部", "运维部", "产品部", "法务部"]
    surnames = ["张", "李", "王", "赵", "刘", "陈", "杨", "黄", "周", "吴", "徐", "孙", "马", "朱", "胡"]
    names_char = ["伟", "芳", "娜", "强", "磊", "洋", "艳", "勇", "军", "杰", "敏", "静", "丽", "涛", "明"]
    for i in range(1, 5001):
        name = random.choice(surnames) + random.choice(names_char)
        w.writerow([
            f"EMP-{i:05d}",
            name,
            random.choice(depts),
            (datetime(2015, 1, 1) + timedelta(days=random.randint(0, 3650))).strftime("%Y-%m-%d"),
        ])

# === 5. 空文件.csv (header only) ===
with open(f"{OUT}/空文件.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["列A", "列B", "列C"])

# === 6. 格式错误.csv (binary content) ===
with open(f"{OUT}/格式错误.csv", "wb") as f:
    f.write(b"\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f")
    f.write(b"this is not csv data at all, just random bytes to simulate a corrupt file")

# === 7. 列数不匹配.csv (8 columns) ===
with open(f"{OUT}/列数不匹配.csv", "w", encoding="utf-8", newline="") as f:
    w = csv.writer(f)
    w.writerow(["订单编号", "客户名称", "产品名称", "数量", "单价", "订单日期", "额外列1", "额外列2"])
    for i in range(1, 11):
        w.writerow([
            f"EXTRA-{i:04d}",
            "测试客户有限公司",
            "测试产品",
            random.randint(1, 10),
            round(random.uniform(100, 1000), 2),
            "2024-06-01",
            f"extra_value_{i}",
            f"extra_data_{i}",
        ])

# === 8. UTF-8-BOM.csv (UTF-8 with BOM) ===
with open(f"{OUT}/UTF-8-BOM.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f)
    w.writerow(["编号", "名称", "状态", "金额"])
    statuses = ["进行中", "已完成", "待审批", "已驳回"]
    for i in range(1, 11):
        w.writerow([
            f"BOM-{i:04d}",
            f"BOM测试项目{i}",
            random.choice(statuses),
            round(random.uniform(100, 5000), 2),
        ])

# === 9. 订单数据.xlsx ===
try:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "订单数据"
    ws.append(["订单号", "客户名称", "产品名称", "金额", "创建日期"])
    for i in range(1, 26):
        ws.append([
            f"XLSX-{i:03d}",
            random.choice(["客户Alpha", "客户Beta", "客户Gamma", "客户Delta"]),
            random.choice(["产品A", "产品B", "产品C"]),
            round(random.uniform(100, 10000), 2),
            f"2024-{random.randint(1,12):02d}-{random.randint(1,28):02d}",
        ])
    wb.save(f"{OUT}/订单数据.xlsx")
    print("  订单数据.xlsx generated")
except ImportError:
    print("  [SKIP] openpyxl not installed, skipping .xlsx generation")

# === 10. 订单数据.xls ===
try:
    import xlwt
    wb2 = xlwt.Workbook(encoding="utf-8")
    ws2 = wb2.add_sheet("订单数据")
    headers = ["订单号", "客户名称", "产品名称", "金额", "创建日期"]
    for i, h in enumerate(headers):
        ws2.write(0, i, h)
    for r in range(1, 16):
        ws2.write(r, 0, f"XLS-{r:03d}")
        ws2.write(r, 1, f"客户{chr(64+r)}")
        ws2.write(r, 2, f"产品{r%3+1}")
        ws2.write(r, 3, round(random.uniform(100, 5000), 2))
        ws2.write(r, 4, f"2024-{r%12+1:02d}-15")
    wb2.save(f"{OUT}/订单数据.xls")
    print("  订单数据.xls generated")
except ImportError:
    print("  [SKIP] xlwt not installed, skipping .xls generation")

print(f"\nAll test files generated in {OUT}/:")
for fn in sorted(os.listdir(OUT)):
    size = os.path.getsize(f"{OUT}/{fn}")
    print(f"  {fn}: {size:,} bytes")
