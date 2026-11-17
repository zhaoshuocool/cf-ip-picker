#!/usr/bin/env python3
"""
cf-ip-picker
============

给「Cloudflare Tunnel + 3X-UI / VLESS + WS + TLS」节点挑选当前可用的
Cloudflare 边缘 IP，并对外提供一个可以直接导入 v2rayN / Clash 的订阅地址。

背景：同一个域名解析到的不同 CF 边缘 IP，在国内的可达性差别巨大
（常见 0/4 与 4/4 的差别）。客户端写死域名时会被分配到劣质 IP，
表现为「隧道本身正常，但代理极慢或直接超时」。

只用 Python 标准库，无第三方依赖。
"""

import hmac
import json
import os
import random
import secrets
import socket
import ssl
import statistics
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

CF_HOST = os.environ.get("CF_HOST", "").strip()
CF_PATH = os.environ.get("CF_PATH", "/").strip() or "/"
CF_UUID = os.environ.get("CF_UUID", "").strip()
CF_INTERVAL = int(os.environ.get("CF_INTERVAL", "3600"))
CF_RANDOM = int(os.environ.get("CF_RANDOM", "60"))
CF_TIMEOUT = float(os.environ.get("CF_TIMEOUT", "3"))
CF_WORKERS = int(os.environ.get("CF_WORKERS", "32"))
CF_FINALISTS = int(os.environ.get("CF_FINALISTS", "20"))
CF_ROUNDS = int(os.environ.get("CF_ROUNDS", "3"))
# 订阅里输出前几个 IP，0 表示全部输出
CF_TOP = int(os.environ.get("CF_TOP", "0"))
CF_STATE_FILE = os.environ.get("CF_STATE_FILE", "")
PORT = int(os.environ.get("PORT", "8080"))


def _token_file_path():
    """订阅口令落盘位置：优先 CF_TOKEN_FILE，其次数据目录。"""
    explicit = os.environ.get("CF_TOKEN_FILE", "").strip()
    if explicit:
        return explicit
    if CF_STATE_FILE:
        return os.path.join(os.path.dirname(os.path.abspath(CF_STATE_FILE)) or ".", "sub_token")
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/sub_token"
    return os.path.join(tempfile.gettempdir(), "cf_sub_token")


def load_or_create_token():
    """口令来源：CF_SUB_TOKEN > 已落盘的口令 > 首次启动随机生成。"""
    explicit = os.environ.get("CF_SUB_TOKEN", "").strip()
    if explicit:
        return explicit

    path = _token_file_path()
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                saved = fh.read().strip()
            if saved:
                return saved
    except OSError:
        pass

    token = secrets.token_urlsafe(9)  # 12 个 URL 安全字符
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(token)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as exc:
        print("警告：订阅口令无法落盘（{}），容器重启后会变".format(exc), flush=True)
    return token


# 所有页面与订阅都挂在 /s/<口令>/ 下，裸路径一律 404
SUB_TOKEN = load_or_create_token()

# 这些状态码说明「TLS 握手成功且请求到达了源站」。
# 400 = xray 拒绝了不带 WebSocket 头的普通 GET
# 404 = xray 对未知路径的默认响应
OK_CODES = {101, 200, 204, 400, 404}

# 经验上对国内线路比较友好的 CF 边缘 IP 段，先测这些，命中率高
CURATED = [
    "172.67.73.120", "104.26.10.78", "104.26.11.78", "172.67.73.121",
    "172.67.72.120", "104.24.0.1", "104.25.0.1", "104.27.0.1",
    "104.20.0.1", "104.22.0.1", "104.19.0.1", "104.17.0.1",
]

PREFIXES = [
    "104.16", "104.17", "104.18", "104.19", "104.20", "104.21",
    "104.22", "104.23", "104.24", "104.25", "104.26", "104.27",
    "172.64", "172.65", "172.66", "172.67",
]

STATE = {
    "host": CF_HOST,
    "path": CF_PATH,
    "uuid": CF_UUID,
    "scanning": False,
    "scan_started": None,
    "updated_at": None,
    "duration_s": None,
    "tested": 0,
    "reachable": 0,
    "results": [],
    "error": None,
    "history": [],
}
LOCK = threading.Lock()


# --------------------------------------------------------------------------
# 探测
# --------------------------------------------------------------------------

def probe_once(ip, host, path, timeout):
    """连到指定边缘 IP，用 host 做 SNI，请求 path，返回是否可达与耗时(ms)。"""
    started = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((ip, 443), timeout=timeout)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # 只测可达性，不校验证书；SNI 仍是真实域名
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls = ctx.wrap_socket(sock, server_hostname=host)
        sock = None  # wrap_socket 接管了原 socket
        tls.settimeout(timeout)
        request = (
            "GET {} HTTP/1.1\r\n"
            "Host: {}\r\n"
            "User-Agent: Mozilla/5.0 (cf-ip-picker)\r\n"
            "Accept: */*\r\n"
            "Connection: close\r\n\r\n"
        ).format(path, host)
        tls.sendall(request.encode())
        buf = b""
        while b"\r\n" not in buf:
            chunk = tls.recv(1024)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 8192:
                break
        tls.close()

        parts = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace").split()
        code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        elapsed = (time.perf_counter() - started) * 1000.0
        return code in OK_CODES, code, elapsed
    except Exception:
        return False, 0, None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def dns_candidates(host):
    """把域名自己的 A 记录也算进候选，方便观察它当前分到哪个 IP。"""
    ips = []
    try:
        infos = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        for item in infos:
            ip = item[4][0]
            if ip not in ips:
                ips.append(ip)
    except Exception:
        pass
    return ips


def build_candidates(host):
    seen, out = set(), []
    for ip in dns_candidates(host) + CURATED:
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    for _ in range(CF_RANDOM):
        ip = "{}.{}.{}".format(
            random.choice(PREFIXES),
            random.randint(1, 254),
            random.randint(1, 254),
        )
        if ip not in seen:
            seen.add(ip)
            out.append(ip)
    return out


def scan():
    host, path = STATE["host"], STATE["path"]
    if not host:
        raise RuntimeError("CF_HOST 未设置")

    with LOCK:
        STATE["scanning"] = True
        STATE["scan_started"] = time.time()
        STATE["error"] = None

    started = time.time()
    candidates = build_candidates(host)

    # 第一轮：并发粗扫，只保留可达的
    reachable = {}
    with ThreadPoolExecutor(max_workers=CF_WORKERS) as pool:
        futures = {pool.submit(probe_once, ip, host, path, CF_TIMEOUT): ip
                   for ip in candidates}
        for fut in as_completed(futures):
            ip = futures[fut]
            ok, _code, ms = fut.result()
            if ok and ms is not None:
                reachable[ip] = ms

    # 第二轮：对靠前的候选多测几轮，避免单次抖动误判
    ranked = []
    finalists = sorted(reachable.items(), key=lambda kv: kv[1])[:CF_FINALISTS]
    if finalists:
        with ThreadPoolExecutor(max_workers=min(CF_WORKERS, len(finalists) * CF_ROUNDS)) as pool:
            futures = []
            for ip, _ in finalists:
                for _ in range(CF_ROUNDS):
                    futures.append((ip, pool.submit(probe_once, ip, host, path, CF_TIMEOUT + 1)))
            samples = {}
            codes = {}
            for ip, fut in futures:
                ok, code, ms = fut.result()
                codes.setdefault(ip, code)
                if ok and ms is not None:
                    samples.setdefault(ip, []).append(ms)
            for ip, _ in finalists:
                got = samples.get(ip, [])
                ranked.append({
                    "ip": ip,
                    "ok": len(got),
                    "total": CF_ROUNDS,
                    "rate": round(len(got) / CF_ROUNDS, 3),
                    "avg_ms": round(statistics.mean(got)) if got else None,
                    "min_ms": round(min(got)) if got else None,
                    "code": codes.get(ip, 0),
                })

    ranked = [r for r in ranked if r["ok"] > 0]
    ranked.sort(key=lambda r: (-r["rate"], r["avg_ms"] or 10 ** 9))

    duration = round(time.time() - started, 1)
    with LOCK:
        STATE["results"] = ranked
        STATE["tested"] = len(candidates)
        STATE["reachable"] = len(reachable)
        STATE["updated_at"] = time.time()
        STATE["duration_s"] = duration
        STATE["scanning"] = False
        STATE["history"].append({
            "at": int(time.time()),
            "tested": len(candidates),
            "reachable": len(reachable),
            "best": ranked[0]["ip"] if ranked else None,
        })
        STATE["history"] = STATE["history"][-50:]
        persist()


def persist():
    if not CF_STATE_FILE:
        return
    try:
        payload = {
            "host": STATE["host"],
            "path": STATE["path"],
            "updated_at": STATE["updated_at"],
            "results": STATE["results"],
        }
        with open(CF_STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass


def scheduler():
    interval = max(CF_INTERVAL, 60)
    while True:
        try:
            scan()
        except Exception as exc:  # 扫描失败不能把服务带崩
            with LOCK:
                STATE["scanning"] = False
                STATE["error"] = str(exc)
        time.sleep(interval)


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------

def vless_link(ip):
    if not STATE["uuid"]:
        return None
    query = urlencode({
        "encryption": "none",
        "security": "tls",
        "sni": STATE["host"],
        "type": "ws",
        "host": STATE["host"],
        "path": STATE["path"],
    })
    return "vless://{}@{}:443?{}#CF-{}".format(
        STATE["uuid"], ip, query, ip.replace(".", "-"))


def snapshot():
    with LOCK:
        data = json.loads(json.dumps({k: v for k, v in STATE.items() if k != "history"}))
        data["history"] = STATE["history"][-10:]
    data["links"] = [vless_link(r["ip"]) for r in data["results"]] if STATE["uuid"] else []
    data["base"] = "/s/" + SUB_TOKEN
    data["uuid_set"] = bool(STATE["uuid"])
    return data


DASHBOARD = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>CF IP Picker</title>
<style>
 body{font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;margin:0;padding:20px;
      background:#0f1115;color:#e6e6e6}
 h1{font-size:18px;margin:0 0 4px}
 .sub{color:#8b93a1;font-size:13px;margin-bottom:16px}
 .card{background:#171a21;border:1px solid #242936;border-radius:10px;padding:14px;margin-bottom:14px}
 table{border-collapse:collapse;width:100%;font-size:14px}
 th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #232838}
 th{color:#8b93a1;font-weight:500}
 .best{color:#4ade80;font-weight:600}
 code{background:#0b0d11;padding:2px 6px;border-radius:4px;font-size:13px}
 .link{color:#60a5fa;word-break:break-all;font-size:12px}
</style></head><body>
<h1>Cloudflare 边缘 IP 选择器</h1>
<div class="sub" id="sub">加载中…</div>
<div class="card"><table>
<thead><tr><th>#</th><th>IP</th><th>成功率</th><th>平均耗时</th><th>状态码</th></tr></thead>
<tbody id="rows"></tbody></table></div>
<div class="card">
  <div class="sub" style="margin-bottom:8px">订阅地址（v2rayN：订阅 → 添加）</div>
  <code id="suburl"></code>
  <div class="sub" style="margin-top:6px">加 <code>?n=3</code> 只取前 3 条（默认输出全部可达 IP），例如
    <code id="suburl3"></code></div>
  <div class="sub" style="margin-top:10px;margin-bottom:4px">当前最优节点的分享链接</div>
  <div class="link" id="bestlink">-</div>
</div>
<script>
async function tick(){
  try{
    const r = await fetch('api/best',{cache:'no-store'});
    const d = await r.json();
    const t = d.updated_at ? new Date(d.updated_at*1000).toLocaleString() : '尚未扫描';
    const uuidNote = d.uuid_set ? 'CF_UUID 已配置' : 'CF_UUID 未配置';
    document.getElementById('sub').textContent =
      `域名 ${d.host}${d.path} · ${uuidNote} · 上次扫描 ${t} · 共测 ${d.tested} 个 · 可达 ${d.reachable} 个 · 订阅 ${d.links.length} 条`;
    const rows = d.results.slice(0,10).map((x,i)=>
      `<tr><td>${i+1}</td><td class="${i===0?'best':''}">${x.ip}</td>
       <td>${x.ok}/${x.total}</td><td>${x.avg_ms} ms</td><td>${x.code}</td></tr>`).join('');
    document.getElementById('rows').innerHTML = rows || '<tr><td colspan="5">扫描中或暂无可用 IP</td></tr>';
    document.getElementById('suburl').textContent = location.origin + d.base + '/sub';
    document.getElementById('suburl3').textContent = location.origin + d.base + '/sub?n=3';
    const first = d.links && d.links[0];
    document.getElementById('bestlink').textContent = first ? first
      : (d.uuid_set
          ? (d.scanning ? '正在首轮扫描，稍后自动刷新…' : '暂无可用 IP，等下一轮扫描')
          : '未设置 CF_UUID（检查容器环境变量）');
  }catch(e){ document.getElementById('sub').textContent = '读取失败: '+e; }
}
tick(); setInterval(tick, 15000);
</script></body></html>"""


def requested_top(query):
    """?n=3 覆盖 CF_TOP；返回 0 表示不限制条数。"""
    raw = (query.get("n") or [None])[0]
    if raw is not None:
        try:
            return max(int(raw), 0)
        except (TypeError, ValueError):
            pass
    return max(CF_TOP, 0)


class Handler(BaseHTTPRequestHandler):
    server_version = "cf-ip-picker"

    def log_message(self, fmt, *args):  # 静音访问日志
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        # 健康检查不需要口令，方便 NAS / 编排探活
        if path.rstrip("/") == "/healthz":
            return self._send(200, json.dumps({"ok": True}))

        segs = [s for s in path.split("/") if s]
        if len(segs) < 2 or segs[0] != "s" or not hmac.compare_digest(segs[1], SUB_TOKEN):
            # 口令不对就和不存在一样，不暴露任何信息
            return self._send(404, json.dumps({"error": "not found"}))

        rest = segs[2:]
        if not rest and not path.endswith("/"):
            # /s/<口令> 补上结尾斜杠，页面里的相对路径才不会跑偏
            self.send_response(302)
            self.send_header("Location", "/s/{}/".format(SUB_TOKEN))
            self.send_header("Content-Length", "0")
            self.end_headers()
            return None

        route = "/".join(rest)
        data = snapshot()

        if route in ("", "index.html"):
            return self._send(200, DASHBOARD, "text/html; charset=utf-8")
        if route == "api/best":
            return self._send(200, json.dumps(data, ensure_ascii=False))
        if route == "sub":
            links = [l for l in data["links"] if l]
            limit = requested_top(query)
            if limit:
                links = links[:limit]
            return self._send(200, "\n".join(links) + ("\n" if links else ""),
                              "text/plain; charset=utf-8")
        if route == "best.txt":
            rows = data["results"]
            limit = requested_top(query)
            if limit:
                rows = rows[:limit]
            lines = ["{} {}ms {}/{}".format(r["ip"], r["avg_ms"], r["ok"], r["total"])
                     for r in rows]
            return self._send(200, "\n".join(lines) + "\n", "text/plain; charset=utf-8")
        return self._send(404, json.dumps({"error": "not found"}))


def main():
    print("cf-ip-picker  host={} path={} interval={}s port={}".format(
        CF_HOST or "(未设置)", CF_PATH, CF_INTERVAL, PORT), flush=True)
    if not CF_HOST:
        print("警告：CF_HOST 未设置，扫描会失败。请通过环境变量传入域名。", flush=True)
    print("UUID:      {}".format("已配置" if CF_UUID else "未配置，只测速不生成订阅"), flush=True)

    threading.Thread(target=scheduler, daemon=True).start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("看板:      http://<本机IP>:{}/s/{}/".format(PORT, SUB_TOKEN), flush=True)
    print("订阅地址:  http://<本机IP>:{}/s/{}/sub".format(PORT, SUB_TOKEN), flush=True)
    print("口令来源:  {}（可用 CF_SUB_TOKEN 固定，落盘文件 {}）".format(
        "环境变量" if os.environ.get("CF_SUB_TOKEN", "").strip() else "自动生成/复用",
        _token_file_path()), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
