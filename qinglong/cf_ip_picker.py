#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cf_ip_picker.py —— 青龙面板 / cron 版 Cloudflare 边缘 IP 优选

适用场景：Cloudflare Tunnel + 3X-UI(VLESS + WS + TLS) 节点。
同一个域名会解析到多个 Cloudflare 边缘 IP，国内可达性差异极大
（有的 4/4 全通 0.4s，有的 4/4 全丢），本脚本挑出当前最好用的几个，
直接在日志里给出可导入 v2rayN 的 vless 链接。

纯标准库，青龙面板（Python 3）无需安装任何依赖。

需要的环境变量（青龙面板 → 环境变量）：
  CF_HOST      节点域名，必填，如 link.example.com
  CF_PATH      WebSocket 路径，默认 /link
  CF_UUID      节点 UUID，填了才会输出 vless 链接
  CF_RANDOM    每轮随机探测 IP 数，默认 60
  CF_TIMEOUT   单次探测超时秒数，默认 3
  CF_WORKERS   并发数，默认 32
  CF_TOP       日志里输出前几名，默认 5
  CF_OUTPUT    结果文件目录，默认脚本同目录

通知（有哪个填哪个，都没填就只写日志）：
  BARK_PUSH            Bark 完整地址，如 https://api.day.app/xxxx
  PUSH_PLUS_TOKEN      PushPlus token
  TG_BOT_TOKEN + TG_CHAT_ID    Telegram
  WECOM_WEBHOOK        企业微信机器人 webhook
  SERVERCHAN_SENDKEY   Server酱 SendKey
"""

import json
import os
import random
import socket
import ssl
import statistics
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

HOST = os.environ.get("CF_HOST", "").strip()
PATH = os.environ.get("CF_PATH", "/link").strip() or "/"
UUID = os.environ.get("CF_UUID", "").strip()
RANDOM_N = int(os.environ.get("CF_RANDOM", "60"))
TIMEOUT = float(os.environ.get("CF_TIMEOUT", "3"))
WORKERS = int(os.environ.get("CF_WORKERS", "32"))
TOP_N = int(os.environ.get("CF_TOP", "5"))
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("CF_OUTPUT", "").strip() or SCRIPT_DIR
STATE_FILE = os.path.join(OUT_DIR, "cf_picker_state.json")

OK_CODES = {101, 200, 204, 400, 404}

CURATED = [
    "172.67.73.120", "172.67.73.121", "172.67.72.120",
    "104.26.10.78", "104.26.11.78", "104.24.0.1", "104.25.0.1",
    "104.27.0.1", "104.20.0.1", "104.22.0.1", "104.19.0.1", "104.17.0.1",
]

PREFIXES = [
    "104.16", "104.17", "104.18", "104.19", "104.20", "104.21",
    "104.22", "104.23", "104.24", "104.25", "104.26", "104.27",
    "172.64", "172.65", "172.66", "172.67",
]


def log(msg):
    print(msg, flush=True)


def probe(ip, timeout):
    """连到指定边缘 IP，SNI 与 Host 都用真实域名，请求 WS 路径。"""
    started = time.perf_counter()
    sock = None
    try:
        sock = socket.create_connection((ip, 443), timeout=timeout)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        tls = ctx.wrap_socket(sock, server_hostname=HOST)
        sock = None
        tls.settimeout(timeout)
        req = ("GET {} HTTP/1.1\r\nHost: {}\r\n"
               "User-Agent: Mozilla/5.0 (cf-ip-picker)\r\n"
               "Accept: */*\r\nConnection: close\r\n\r\n").format(PATH, HOST)
        tls.sendall(req.encode())
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
        return code in OK_CODES, code, (time.perf_counter() - started) * 1000.0
    except Exception:
        return False, 0, None
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass


def candidates():
    ips, seen = [], set()
    try:
        for info in socket.getaddrinfo(HOST, 443, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip not in seen:
                seen.add(ip)
                ips.append(ip)
    except Exception:
        pass
    for ip in CURATED:
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    for _ in range(RANDOM_N):
        ip = "{}.{}.{}".format(random.choice(PREFIXES),
                               random.randint(1, 254), random.randint(1, 254))
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


def vless_link(ip):
    if not UUID:
        return ""
    query = urllib.parse.urlencode({
        "encryption": "none", "security": "tls", "sni": HOST,
        "type": "ws", "host": HOST, "path": PATH,
    })
    return "vless://{}@{}:443?{}#CF-{}".format(
        UUID, ip, query, ip.replace(".", "-"))


def _github(method, url, payload, token):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers={
        "Authorization": "Bearer " + token,
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "cf-ip-picker",
    })
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())


def publish_subscription(text):
    """把订阅内容发布到一个可被 v2rayN 直接订阅的 URL。

    支持两种目标：
      1. GitHub Gist（CF_GIST_ID + GITHUB_TOKEN）：最省事，raw 链接就是订阅地址；
      2. 任意自建地址（CF_PUSH_URL）：POST 纯文本过去，适合推到自己的静态站。
    返回订阅 URL，失败返回 None。
    """
    name = os.environ.get("CF_GIST_FILENAME", "cf-sub.txt").strip() or "cf-sub.txt"
    token = (os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "").strip()
    gist_id = os.environ.get("CF_GIST_ID", "").strip()

    if token and (gist_id or os.environ.get("CF_GIST_CREATE", "").strip() in ("1", "true", "yes")):
        try:
            if gist_id:
                data = _github("PATCH", "https://api.github.com/gists/" + gist_id,
                               {"files": {name: {"content": text}}}, token)
            else:
                data = _github("POST", "https://api.github.com/gists", {
                    "description": "cf-ip-picker subscription",
                    "public": False,
                    "files": {name: {"content": text}},
                }, token)
                log("已创建新 Gist，把它填进环境变量后再跑一次就不用重建：")
                log("  CF_GIST_ID={}".format(data.get("id")))
            owner = (data.get("owner") or {}).get("login", "")
            url = "https://gist.githubusercontent.com/{}/{}/raw/{}".format(
                owner, data.get("id"), name)
            log("订阅地址：{}".format(url))
            return url
        except Exception as exc:
            log("Gist 发布失败：{}".format(exc))

    push_url = os.environ.get("CF_PUSH_URL", "").strip()
    if push_url:
        try:
            req = urllib.request.Request(
                push_url, data=text.encode("utf-8"), method="POST",
                headers={"Content-Type": "text/plain; charset=utf-8",
                         "User-Agent": "cf-ip-picker"})
            with urllib.request.urlopen(req, timeout=25) as resp:
                resp.read()
            log("订阅已推送到：{}".format(push_url))
            return push_url
        except Exception as exc:
            log("推送订阅失败：{}".format(exc))
    return None


def notify(title, content):
    """尽量用面板里已配置的推送渠道发一条通知；失败不影响任务结果。"""
    def post(url, data=None, payload=None, headers=None):
        body = None
        hdrs = {"User-Agent": "cf-ip-picker"}
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        elif data is not None:
            body = urllib.parse.urlencode(data).encode("utf-8")
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=body, headers={**hdrs, **(headers or {})})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read()

    def guard(fn):
        try:
            fn()
            return True
        except Exception as exc:
            log("  通知渠道失败: {}".format(exc))
            return False

    sent = False
    bark = os.environ.get("BARK_PUSH", "").strip()
    if bark:
        url = bark.rstrip("/")
        if url.startswith("http"):
            sent |= guard(lambda: post("{}/{}/{}".format(
                url, urllib.parse.quote(title), urllib.parse.quote(content))))
    token = os.environ.get("PUSH_PLUS_TOKEN", "").strip()
    if token:
        sent |= guard(lambda: post("https://www.pushplus.plus/send",
                                   payload={"token": token, "title": title,
                                            "content": content.replace("\n", "<br>"),
                                            "template": "html"}))
    tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip()
    if tg_token and tg_chat:
        sent |= guard(lambda: post(
            "https://api.telegram.org/bot{}/sendMessage".format(tg_token),
            payload={"chat_id": tg_chat, "text": title + "\n" + content}))
    wecom = os.environ.get("WECOM_WEBHOOK", "").strip()
    if wecom:
        sent |= guard(lambda: post(wecom, payload={
            "msgtype": "text", "text": {"content": title + "\n" + content}}))
    sendkey = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if sendkey:
        sent |= guard(lambda: post("https://sctapi.ftqq.com/{}.send".format(sendkey),
                                   data={"title": title, "desp": content}))
    if not sent:
        log("（未配置推送渠道，仅写日志）")


def main():
    log("=" * 52)
    log("cf-ip-picker  {}".format(time.strftime("%Y-%m-%d %H:%M:%S")))
    if not HOST:
        log("错误：CF_HOST 未设置")
        sys.exit(1)
    log("域名 {}\n路径 {}\n随机探测 {} 个 IP，并发 {}".format(
        HOST, PATH, RANDOM_N, WORKERS))
    log("=" * 52)

    started = time.time()
    pool_cands = candidates()
    log("候选 IP：{} 个（含域名自身解析）".format(len(pool_cands)))

    reachable = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(probe, ip, TIMEOUT): ip for ip in pool_cands}
        for fut in as_completed(futures):
            ip = futures[fut]
            ok, _code, ms = fut.result()
            if ok and ms is not None:
                reachable[ip] = ms

    log("粗扫完成：{} 个可通，耗时 {:.1f}s".format(len(reachable), time.time() - started))
    if not reachable:
        log("没有任何候选 IP 可通 —— 检查域名解析、CF_HOST / CF_PATH 是否正确")
        notify("CF 优选失败", "{} 本轮全部不通".format(HOST))
        sys.exit(1)

    ranked = []
    finalists = sorted(reachable.items(), key=lambda kv: kv[1])[:20]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(finalists) * 3)) as pool:
        jobs = [(ip, pool.submit(probe, ip, TIMEOUT + 1))
                for ip, _ in finalists for _ in range(3)]
        samples, codes = {}, {}
        for ip, fut in jobs:
            ok, code, ms = fut.result()
            codes[ip] = code
            if ok and ms is not None:
                samples.setdefault(ip, []).append(ms)
    for ip, _ in finalists:
        got = samples.get(ip, [])
        if not got:
            continue
        ranked.append({
            "ip": ip, "ok": len(got), "total": 3,
            "rate": round(len(got) / 3, 2),
            "avg_ms": round(statistics.mean(got)),
            "code": codes.get(ip, 0),
        })
    ranked.sort(key=lambda r: (-r["rate"], r["avg_ms"]))

    top = ranked[:TOP_N]
    log("")
    log("排名   IP                成功率   平均耗时   状态码")
    for idx, item in enumerate(top, 1):
        log("{:<5}  {:<16}  {}/{}     {:>5} ms   {}".format(
            idx, item["ip"], item["ok"], item["total"], item["avg_ms"], item["code"]))

    lines = ["{} {}ms {}/{}".format(r["ip"], r["avg_ms"], r["ok"], r["total"]) for r in ranked]
    links = [vless_link(r["ip"]) for r in top]

    # 先读上一轮结果，再覆盖写，否则永远判定为"未变化"
    prev = None
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            prev = json.load(fh)
    except Exception:
        pass
    prev_results = (prev or {}).get("results") or []
    prev_rec = prev_results[0].get("ip") if prev_results else None
    prev_avg = prev_results[0].get("avg_ms") if prev_results else None

    try:
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(OUT_DIR, "cf_best.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        with open(os.path.join(OUT_DIR, "cf_sub.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(l for l in links if l) + "\n")
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump({"at": int(time.time()), "host": HOST, "results": ranked[:20]},
                      fh, ensure_ascii=False, indent=2)
    except Exception as exc:
        log("写文件失败（不影响结果）：{}".format(exc))

    sub_text = "".join(l + "\n" for l in links if l)
    if sub_text:
        publish_subscription(sub_text)

    if links and links[0]:
        log("")
        log("首选节点分享链接（可直接导入 v2rayN）：")
        log(links[0])
        log("")
        log("全部前 {} 名链接已写入 {}".format(
            TOP_N, os.path.join(OUT_DIR, "cf_sub.txt")))

    # 只在「值得换」的时候推送：上一轮推荐的 IP 挂了，或者新首选明显更快（>20%）
    best = top[0]["ip"]
    new_avg = top[0]["avg_ms"]
    force = os.environ.get("CF_NOTIFY_ALWAYS", "").strip().lower() in ("1", "true", "yes")
    rec_alive = prev_rec in reachable
    improved = bool(prev_avg) and new_avg < prev_avg * 0.8
    changed = force or prev_rec is None or (not rec_alive) or improved

    body = "首选 {} ({}ms)\n".format(best, new_avg)
    body += "\n".join("{} {}ms {}/{}".format(r["ip"], r["avg_ms"], r["ok"], r["total"])
                      for r in top)
    if links and links[0]:
        body += "\n\n" + links[0]
    if changed:
        notify("CF 优选：推荐 IP 已更新", body)
    else:
        log("推荐 IP {} 仍可用（{}ms，新首选 {}ms），变化不足 20%，本轮不推送".format(
            prev_rec, prev_avg, new_avg))
        log("想每轮都收到消息就设 CF_NOTIFY_ALWAYS=1")

    log("完成，总耗时 {:.1f}s".format(time.time() - started))


if __name__ == "__main__":
    main()
