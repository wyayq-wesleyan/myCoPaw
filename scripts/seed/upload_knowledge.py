#!/usr/bin/env python3
"""通过知识服务 API 上传多品类知识文档到指定空间的目录。

先调用 get_or_create_user_space 获取/创建 admin 空间(实际上直接复用已有),
然后逐个上传文档文件。

用法：
    python3 upload_knowledge.py --api http://localhost:18100 --key huizhi-local-knowledge-dev \
        --user admin --is-admin true --dir /tmp/mycopaw-knowledge
"""
import argparse
import json
import os
import time
import uuid
import urllib.request

def request(api, key, method, path, user, is_admin, body=None, form_fields=None, file_path=None):
    url = api + path
    boundary = uuid.uuid4().hex
    if form_fields is not None or file_path is not None:
        wire = b""
        if form_fields:
            for k, v in form_fields.items():
                wire += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode()
        if file_path:
            fn = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                data = f.read()
            wire += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{fn}\"\r\n"
                     f"Content-Type: application/octet-stream\r\n\r\n").encode()
            wire += data + b"\r\n"
        wire += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(url, data=wire, method=method)
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    else:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
    req.add_header("X-Knowledge-Key", key)
    req.add_header("X-User-Id", user)
    req.add_header("X-Is-Admin", is_admin)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://localhost:18100")
    ap.add_argument("--key", default="huizhi-local-knowledge-dev")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--is-admin", default="true")
    ap.add_argument("--dir", default="/tmp/mycopaw-knowledge")
    ap.add_argument("--space-file", default="/tmp/admin_space_id.txt", help="复用已有的空间ID时不新建")
    args = ap.parse_args()

    # 获取或复用 admin 空间
    code, res = request(args.api, args.key, "GET", "/v1/spaces", args.user, args.is_admin)
    if code != 200:
        print(f"列出空间失败 [{code}]: {res}"); return
    spaces = res
    space_id = None
    for s in spaces:
        if s["owner_id"] == args.user:
            space_id = s["id"]
            name = s["name"]
            break
    if not space_id:
        code, res = request(args.api, args.key, "POST", "/v1/spaces", args.user, args.is_admin,
                            body={"name": f"user_{args.user}"})
        if code != 200:
            print(f"创建空间失败 [{code}]: {res}"); return
        space_id = res["id"]
        name = res.get("name", "")
    print(f"使用空间: {name} ({space_id})")
    with open(args.space_file, "w") as f:
        f.write(space_id)

    for fn in sorted(os.listdir(args.dir)):
        fp = os.path.join(args.dir, fn)
        if not os.path.isfile(fp):
            continue
        t0 = time.time()
        code, res = request(args.api, args.key, "POST", "/v1/documents/upload", args.user,
                            args.is_admin, form_fields={"space_id": space_id}, file_path=fp)
        dt = time.time() - t0
        status = res.get("status", "?")
        print(f"[{code}] {fn} -> {status} job={res.get('job_id','?')} 耗时{dt:.1f}s")

if __name__ == "__main__":
    main()
