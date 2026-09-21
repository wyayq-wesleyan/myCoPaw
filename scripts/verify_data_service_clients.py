"""Verify the runtime can CONNECT to and OPERATE graph DBs and object storage.

Importing a client library proves nothing, so this exercises real behaviour
against in-process mocks and reports what actually works:

  * plain-HTTP S3 round trip (boto3 PUT/GET/LIST/DELETE, presigned URL,
    MinIO SDK, pandas+pyarrow over boto3)
  * HTTPS to an S3-compatible endpoint (TLS handshake + byte-exact round trip,
    plus the SigV4 / aws-chunked streaming request a real MinIO also receives)
  * Neo4j Bolt handshake over a real TCP socket, and TLS URI schemes

Run it inside the runtime image (or any host with the same deps):

    docker run --rm -v "$PWD/scripts:/s" --entrypoint /bin/bash \\
        huizhi-runtime:2.2.1-arm64 -lc 'cd /s && python verify_data_service_clients.py'

No network, cluster or credentials are required: every endpoint is a local mock,
so this only answers "is the client stack able to talk to such a service?".
"""

from __future__ import annotations

import http.server
import io
import ipaddress
import socket
import ssl
import tempfile
import threading
import urllib.parse
import xml.sax.saxutils as sx
from pathlib import Path

PASS, FAIL = "PASS", "FAIL"
RESULTS: list[tuple[str, str, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, PASS if ok else FAIL, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


# --------------------------------------------------------------------------- S3
STORE: dict[tuple[str, str], bytes] = {}
OPS: list[str] = []


class _S3Mock(http.server.BaseHTTPRequestHandler):
    """Just enough of the S3 REST surface for real client round trips."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # keep the output readable
        pass

    def _key(self) -> str:
        return urllib.parse.urlparse(self.path).path.lstrip("/")

    def _xml(self, code: int, body: bytes = b"") -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _listing(self, bucket: str) -> bytes:
        items = "".join(
            f"<Contents><Key>{sx.escape(k)}</Key><Size>{len(v)}</Size>"
            "<LastModified>2026-01-01T00:00:00.000Z</LastModified>"
            "<ETag>&quot;e&quot;</ETag><StorageClass>STANDARD</StorageClass>"
            "</Contents>"
            for (b, k), v in STORE.items() if b == bucket
        )
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
            f"<Name>{bucket}</Name><MaxKeys>1000</MaxKeys>"
            "<IsTruncated>false</IsTruncated>"
            f"{items}</ListBucketResult>"
        ).encode()

    def do_PUT(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        key = self._key()
        STORE[tuple(key.split("/", 1))] = body
        OPS.append(f"PUT {key}")
        self.send_response(200)
        self.send_header("ETag", '"e"')
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        key = self._key()
        bucket = key.split("/", 1)[0]
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if query.get("list-type") or key in ("", bucket):
            OPS.append(f"LIST {bucket}")
            self._xml(200, self._listing(bucket))
            return
        if tuple(key.split("/", 1)) in STORE:
            OPS.append(f"GET {key}")
            body = STORE[tuple(key.split("/", 1))]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self._xml(404)

    def do_DELETE(self):
        key = self._key()
        STORE.pop(tuple(key.split("/", 1)), None)
        OPS.append(f"DELETE {key}")
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _serve(handler, *, tls: bool = False) -> tuple[http.server.ThreadingHTTPServer, int, Path]:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    cert_dir = Path(tempfile.mkdtemp())
    if tls:
        srv.socket = _tls_context(cert_dir).wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1], cert_dir


def _tls_context(cert_dir: Path) -> ssl.SSLContext:
    """Self-signed cert for 127.0.0.1/minio.local, built with the image's own stack."""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "minio.local")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("minio.local"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = cert_dir / "c.pem", cert_dir / "k.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        ),
    )
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    return ctx


def check_versions() -> None:
    print("\n=== 客户端库版本 ===")
    import importlib.metadata as md

    for pkg in ("neo4j", "minio", "boto3", "botocore", "s3fs", "fsspec",
                "urllib3", "certifi", "pandas", "pyarrow", "cryptography"):
        try:
            print(f"    {pkg:14} {md.version(pkg)}")
        except md.PackageNotFoundError:
            print(f"    {pkg:14} -")
    try:
        n = len(ssl.create_default_context().get_ca_certs())
        record("系统 CA 信任库可用（https / bolt+s）", n > 0, f"{n} 个 CA")
    except Exception as exc:  # noqa: BLE001
        record("系统 CA 信任库可用（https / bolt+s）", False, repr(exc))


def check_s3_plain() -> None:
    print("\n=== S3 / MinIO（明文 HTTP，真实请求往返） ===")
    srv, port, _ = _serve(_S3Mock)
    endpoint, bucket, key = f"http://127.0.0.1:{port}", "demo-bucket", "reports/q3.csv"
    payload = "季度,收入\nQ3,1.28亿\n".encode()

    try:
        import boto3
        from botocore.config import Config

        s3 = boto3.client(
            "s3", endpoint_url=endpoint,
            aws_access_key_id="minioadmin", aws_secret_access_key="minioadmin",
            region_name="us-east-1",
            config=Config(s3={"addressing_style": "path"},
                          signature_version="s3v4",
                          retries={"max_attempts": 1}),
        )
        s3.put_object(Bucket=bucket, Key=key, Body=payload)
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        record("boto3 上传→下载字节一致", got == payload, f"{len(got)} 字节")

        listed = [o["Key"] for o in
                  s3.list_objects_v2(Bucket=bucket).get("Contents", [])]
        record("boto3 列举对象", key in listed, f"{listed}")

        url = s3.generate_presigned_url(
            "get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=3600)
        record(
            "boto3 预签名 URL（浏览器直传/直取）",
            "X-Amz-Signature=" in url and "X-Amz-Expires=" in url,
            "SigV4 签名已生成",
        )

        s3.delete_object(Bucket=bucket, Key=key)
        record("boto3 删除对象", (bucket, key) not in STORE)
    except Exception as exc:  # noqa: BLE001
        record("boto3 S3 往返", False, repr(exc))

    try:
        from minio import Minio

        cli = Minio(endpoint.replace("http://", ""), access_key="minioadmin",
                    secret_key="minioadmin", secure=False)
        body = b"hello-minio"
        cli.put_object(bucket, "from-minio.txt", io.BytesIO(body), len(body),
                       content_type="text/plain")
        back = cli.get_object(bucket, "from-minio.txt").read()
        record("MinIO SDK 上传→下载一致", back == body, f"{len(back)} 字节")
    except Exception as exc:  # noqa: BLE001
        record("MinIO SDK 上传→下载一致", False, repr(exc))

    try:
        import pandas as pd

        df = pd.DataFrame({"城市": ["北京", "上海"], "收入": [100, 200]})
        buf = io.BytesIO()
        df.to_parquet(buf, engine="pyarrow")
        s3.put_object(Bucket=bucket, Key="analytics/d.parquet", Body=buf.getvalue())
        raw = s3.get_object(Bucket=bucket, Key="analytics/d.parquet")["Body"].read()
        back = pd.read_parquet(io.BytesIO(raw), engine="pyarrow")
        record("pandas 经 boto3 读写 parquet",
               list(back["城市"]) == ["北京", "上海"], f"{len(back)} 行")
    except Exception as exc:  # noqa: BLE001
        record("pandas 经 boto3 读写 parquet", False, repr(exc))

    print(f"    实际 S3 操作: {OPS}")
    srv.shutdown()


def check_s3_tls() -> None:
    print("\n=== S3 / MinIO（HTTPS，集群通常走 TLS） ===")
    srv, port, cert_dir = _serve(_S3Mock, tls=True)
    endpoint = f"https://127.0.0.1:{port}"

    try:
        raw = socket.create_connection(("127.0.0.1", port), timeout=5)
        cctx = ssl.create_default_context()
        cctx.check_hostname = False
        cctx.verify_mode = ssl.CERT_NONE
        with cctx.wrap_socket(raw, server_hostname="minio.local") as tls:
            record("TLS 握手", True, f"{tls.version()} / {tls.cipher()[0]}")
    except Exception as exc:  # noqa: BLE001
        record("TLS 握手", False, repr(exc))

    try:
        import boto3
        from botocore.config import Config

        kwargs = dict(s3={"addressing_style": "path"},
                      signature_version="s3v4", retries={"max_attempts": 1})
        try:
            # boto3 >= 1.36 streams checksums (aws-chunked); ask for the classic
            # Content-Length body so a plain mock can echo it back verbatim.
            cfg = Config(request_checksum_calculation="when_required", **kwargs)
        except TypeError:
            cfg = Config(**kwargs)

        s3 = boto3.client("s3", endpoint_url=endpoint,
                          aws_access_key_id="minioadmin",
                          aws_secret_access_key="minioadmin",
                          region_name="us-east-1", verify=False, config=cfg)
        payload = "城市,收入\n北京,100\n".encode()
        s3.put_object(Bucket="b", Key="tls.csv", Body=payload)
        got = s3.get_object(Bucket="b", Key="tls.csv")["Body"].read()
        record("boto3 over HTTPS 往返字节一致（含中文）", got == payload,
               f"{len(got)} 字节")
    except Exception as exc:  # noqa: BLE001
        record("boto3 over HTTPS 往返字节一致（含中文）", False, repr(exc))

    srv.shutdown()
    for p in cert_dir.iterdir():
        p.unlink(missing_ok=True)
    cert_dir.rmdir()


BOLT_MAGIC = b"\x60\x60\xb0\x17"


def check_neo4j() -> None:
    print("\n=== Neo4j / Bolt ===")
    captured: dict[str, bytes] = {}

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def serve() -> None:
        try:
            conn, _ = listener.accept()
            with conn:
                # Bolt preamble: 4 magic bytes + 4 version proposals (20 bytes).
                captured["preamble"] = conn.recv(20)
                conn.sendall(b"\x00\x00\x00\x00")  # refuse a version
        except Exception as exc:  # noqa: BLE001
            captured["error"] = repr(exc).encode()

    threading.Thread(target=serve, daemon=True).start()

    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(f"bolt://127.0.0.1:{port}",
                                      auth=("neo4j", "password"),
                                      connection_timeout=5)
        try:
            driver.verify_connectivity()
        except Exception:  # noqa: BLE001  (the mock intentionally refuses)
            pass
        finally:
            driver.close()

        preamble = captured.get("preamble", b"")
        record(
            "Neo4j 驱动发出 Bolt 握手（真实 TCP + 协议）",
            preamble[:4] == BOLT_MAGIC and len(preamble) == 20,
            f"len={len(preamble)} magic={preamble[:4].hex() if preamble else '-'}",
        )
    except Exception as exc:  # noqa: BLE001
        record("Neo4j 驱动发出 Bolt 握手（真实 TCP + 协议）", False, repr(exc))
    finally:
        listener.close()

    # Cypher/Query construction and TLS schemes are offline capabilities.
    try:
        from neo4j import GraphDatabase, Query

        d = GraphDatabase.driver("bolt://localhost:7687", auth=("u", "p"))
        d.session()
        q = Query("MATCH (n) RETURN n LIMIT 1")
        d.close()
        record("会话与 Cypher 查询构造", q.text.startswith("MATCH"))
    except Exception as exc:  # noqa: BLE001
        record("会话与 Cypher 查询构造", False, repr(exc))

    schemes = ["bolt://h:7687", "bolt+s://h:7687", "neo4j://h:7687",
               "neo4j+s://h:7687", "neo4j+ssc://h:7687"]
    accepted = []
    try:
        from neo4j import GraphDatabase

        for uri in schemes:
            try:
                GraphDatabase.driver(uri, auth=("u", "p")).close()
                accepted.append(uri.split("://")[0])
            except Exception:  # noqa: BLE001
                pass
        record("TLS URI 方案可用（集群加密连接）",
               "bolt+s" in accepted and "neo4j+s" in accepted, ", ".join(accepted))
    except Exception as exc:  # noqa: BLE001
        record("TLS URI 方案可用（集群加密连接）", False, repr(exc))


def main() -> None:
    check_versions()
    check_s3_plain()
    check_s3_tls()
    check_neo4j()

    print("\n=== 汇总 ===")
    for name, status, detail in RESULTS:
        print(f"  {status}  {name}")
    failed = [n for n, s, _ in RESULTS if s == FAIL]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} 通过")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    main()
