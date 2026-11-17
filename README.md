# cf-ip-picker

给你的 **Cloudflare Tunnel + VLESS/WS/TLS 节点** 挑当前最快、最稳的 Cloudflare 边缘 IP。

## 它解决什么问题

把节点域名（例如 `link.example.com`）解析到 Cloudflare，本质上仍然是 DNS 把域名指向若干 CF 边缘 IP。
这些 IP 在国内的可达性差别极大 —— 实测同样的 SNI，有的 IP 4/4 全通、TLS 0.4 秒，
有的 IP 4/4 全丢、TCP 都连不上。客户端写死域名时会被随机分到劣质 IP，
表现就是「隧道本身没坏（`/link` 能 101 升级），但代理极慢或直接超时」。

本容器做的事情：

1. 拿到候选 IP —— 域名自己的 A 记录 + 一批经验好 IP + 随机抽样；
2. 并发对每个 IP 做真实探测（SNI 用你的域名、Host 用你的域名、请求你的 WS 路径）；
3. 对存活的前 N 个复测多轮，按「成功率 → 平均耗时」排序；
4. 起一个网页看结果，并生成**可以直接导进 v2rayN 的订阅链接**（每个 IP 一条 VLESS 节点）。

## 快速开始

```bash
docker run -d --name cf-ip-picker \
  -p 8080:8080 \
  -e CF_HOST=link.example.com \
  -e CF_PATH=/link \
  -e CF_UUID=你的-UUID \
  -e CF_INTERVAL=3600 \
  ghcr.io/zhaoshuocool/cf-ip-picker:latest
```

打开 `http://<设备IP>:8080/` 就能看到排名。

或者用 compose（推荐，见 `docker-compose.yml`）：

```bash
docker compose up -d
```

### 部署到飞牛 NAS（fnOS）

fnOS 自带 Docker 管理，两种方式都行：

**方式一：Docker 应用 → 项目（compose）**

1. 在文件管理器里建一个目录，例如 `/vol1/1000/docker/cf-ip-picker`；
2. 把本仓库的 `docker-compose.yml` 传进去，按需改 `CF_HOST` / `CF_PATH` / `CF_UUID`；
3. 「Docker」→「Compose」→「新增项目」，路径选该目录，项目名 `cf-ip-picker`，启动；
4. 浏览器访问 `http://<NAS地址>:8080/`。

**方式二：命令行**

```bash
ssh <你的用户名>@<NAS地址>
mkdir -p /vol1/1000/docker/cf-ip-picker && cd $_
# 把 docker-compose.yml 放进来后
docker compose up -d
```

镜像来自 GHCR 公共仓库，**不需要登录**。若 NAS 访问不了 ghcr.io，
可在 fnOS 的 Docker 设置里配置镜像加速，或在本机构建后 `docker save` / `docker load`。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `CF_HOST` | *(必填)* | 节点域名，同时用作 SNI 和 Host |
| `CF_PATH` | `/` | WebSocket 路径，必须和 xray 入站一致 |
| `CF_UUID` | 空 | 节点 UUID；填了才会生成 `/sub` 订阅与分享链接 |
| `CF_INTERVAL` | `3600` | 定时重扫间隔（秒），最低 60 |
| `CF_RANDOM` | `60` | 每轮随机探测的 IP 个数 |
| `CF_TIMEOUT` | `3` | 单次探测超时（秒） |
| `CF_WORKERS` | `32` | 并发线程数 |
| `CF_FINALISTS` | `20` | 进入复测的候选数 |
| `CF_ROUNDS` | `3` | 每个候选复测次数 |
| `CF_STATE_FILE` | 空 | 结果落盘路径，如 `/data/state.json` |
| `PORT` | `8080` | Web 端口 |

## 接口

| 路径 | 说明 |
| --- | --- |
| `/` | 网页仪表盘，15 秒自动刷新 |
| `/api/best` | JSON，完整排名与耗时 |
| `/sub` | **订阅地址**，纯文本，每行一条 `vless://`（可直接粘进 v2rayN） |
| `/best.txt` | 纯文本排名，方便脚本读取 |
| `/healthz` | 健康检查 |

### 在 v2rayN 里用

1. 打开 `http://<设备IP>:8080/`，确认有排名；
2. v2rayN → 订阅 → 订阅设置 → 添加，地址填 `http://<设备IP>:8080/sub`；
3. 更新订阅，会得到一组 `CF-<IP>` 节点，哪个快用哪个。

生成的链接长这样：

```
vless://<uuid>@104.26.10.78:443?encryption=none&security=tls&sni=link.example.com&type=ws&host=link.example.com&path=%2Flink#CF-104-26-10-78
```

## 客户端侧要注意的三件事

1. **地址改成 IP 之后必须显式填 SNI**，否则证书校验失败（域名对不上）。
2. **打开 Mux**（实测并发 8 时，平均握手从 2.3 秒降到 0.9 秒，下载从 0.58 MB/s 到 5.14 MB/s）。
3. Host 和 path 必须和入站配置完全一致，本容器只负责选 IP，不改变传输参数。

## 判定标准

探测返回 `101 / 200 / 204 / 400 / 404` 都算「通」。其中 `400` 是 xray 对普通 GET（没有 WebSocket 头）的正常拒绝，
`404` 是 xray 对未知路径的默认响应 —— 两者都证明 TLS 握手成功、请求确实打到了你的源站。

## 构建

镜像由 GitHub Actions 自动构建并推送到 GHCR（`linux/amd64` + `linux/arm64`）：

```bash
git push            # main 分支 → :latest
git tag v1.0.0 && git push --tags   # 版本 tag → :1.0.0
```

本地构建：

```bash
docker build -t cf-ip-picker .
```

## 许可

MIT
