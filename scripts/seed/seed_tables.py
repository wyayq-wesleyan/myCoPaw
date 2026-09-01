#!/usr/bin/env python3
"""通过真实 API 创建数据表并导入 CSV。

用法：
    python3 seed_tables.py --api http://localhost:18088 --token-file /tmp/admin_token.txt --dir /tmp/mycopaw-seed
"""
import argparse
import json
import os
import sys
import time
import urllib.request

def read_token(token_file: str) -> str:
    with open(token_file, "r") as f:
        return f.read().strip()

def api_request(api: str, token: str, method: str, path: str, body: dict | None = None,
                query: str | None = None) -> tuple[int, dict]:
    url = f"{api}/api{path}"
    if query:
        url += "?" + query
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def api_upload(api: str, token: str, method: str, path: str, files: list[str], fields: dict | None = None) -> tuple[int, dict]:
    import uuid
    boundary = uuid.uuid4().hex
    body = b""
    if fields:
        for k, v in fields.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
    for fp in files:
        with open(fp, "rb") as f:
            content = f.read()
        fn = os.path.basename(fp)
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{fn}\"\r\n"
                 f"Content-Type: application/octet-stream\r\n\r\n").encode()
        body += content + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{api}/api{path}", data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def create_table(api, token, display_name, description, columns) -> str:
    code, res = api_request(api, token, "POST", "/data-tables", {
        "display_name": display_name, "description": description, "columns": columns,
    })
    if code != 201:
        raise RuntimeError(f"创建表失败 [{code}]: {res}")
    return res["id"]

COLUMN_MAP = {
    # 中文列描述（必须与 CSV 表头顺序一一对应）
    "客户信息表.csv": ["唯一客户编号", "客户公司全称", "客户规模类型", "所属行业", "所在城市区域", "信用评级", "营业执照成立日期"],
    "产品目录表.csv": ["唯一产品编号", "产品名称", "产品分类", "销售单价", "成本单价", "计量单位", "上下架状态"],
    "销售订单表.csv": ["唯一订单编号", "关联客户编号", "关联产品编号", "购买数量", "成交单价", "订单总额", "下单日期", "订单状态"],
    "库存流水表.csv": ["唯一流水编号", "关联产品编号", "所在仓库", "库存变动类型", "变动数量", "变动后结余", "变动日期"],
    "员工花名册表.csv": ["唯一员工工号", "员工姓名", "所属部门", "职级", "入职日期", "月度薪资", "所在城市", "在职状态"],
    "供应商信息表.csv": ["唯一供应商编号", "供应商名称", "供货品类", "所在地城市", "合作状态", "综合评分"],
    "营销活动表.csv": ["唯一活动编号", "活动名称", "投放渠道", "投入金额", "带来的流量", "转化客户数", "活动开始日期", "活动结束日期"],
    "财务报表表.csv": ["唯一记录编号", "财务统计月份", "营业收入", "成本支出", "净利润", "经营活动现金流", "活跃客户数"],
    "物流订单表.csv": ["唯一物流单号", "关联订单编号", "物流承运商", "发货城市", "收货城市", "货物重量", "运费", "发货日期", "物流状态"],
    "渠道销售表.csv": ["唯一记录编号", "关联产品编号", "销售渠道", "销售区域", "销售量", "销售额", "统计月份"],
    "采购订单表.csv": ["唯一采购单号", "关联供应商编号", "关联产品编号", "采购数量", "采购单价", "采购金额", "下单日期", "采购状态"],
    "机器运行表.csv": ["唯一记录编号", "设备编号", "设备类型", "运行温度", "转速", "运行时长", "负载率", "数据采集时间", "健康状态"],
}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:18088")
    ap.add_argument("--token-file", default="/tmp/admin_token.txt")
    ap.add_argument("--dir", default="/tmp/mycopaw-seed")
    ap.add_argument("--only", default=None, help="只处理指定文件,逗号分隔")
    args = ap.parse_args()

    token = read_token(args.token_file)
    files = [f for f in sorted(os.listdir(args.dir)) if f.endswith(".csv")]
    if args.only:
        files = [f for f in files if f in args.only.split(",")]
    if not files:
        print("没有找到 CSV 文件"); sys.exit(1)

    # 为每张表创建元数据并等待导入完成
    for fn in files:
        display = fn.replace(".csv", "")
        desc = f"{display}数据表，用于业务分析。"
        cols = COLUMN_MAP.get(fn)
        if not cols:
            print(f"!! 跳过 {fn}: 无列描述映射")
            continue
        try:
            table_id = create_table(args.api, token, display, desc, cols)
        except RuntimeError as e:
            print(f"!! {fn}: {e}")
            continue
        path = os.path.join(args.dir, fn)
        t0 = time.time()
        code, res = api_upload(args.api, token, "POST", f"/data-tables/{table_id}/import", [path])
        dt = time.time() - t0
        print(f"[{fn}] 表id={table_id} 提交导入 {code} 耗时{dt:.1f}s res={res}")

    # 打印所有导入任务状态
    print("\n=== 等待导入任务完成 ===")
    time.sleep(3)
    code, res = api_request(args.api, token, "GET", "/data-import-jobs", query="page_size=50")
    if code == 200:
        for item in res.get("items", []):
            print(f"  {item['filename']} -> {item['status']} 成功{item['success_rows']} 失败{item['failed_rows']}")

if __name__ == "__main__":
    main()
