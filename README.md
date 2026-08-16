# Pixoo64 工作状态看板

局域网内的 Pixoo64 实时显示个人工作状态，任意设备的浏览器都能修改。
每个状态下有多套高饱和动画图案，每 15 秒自动轮换。

## 图案

| 状态 | Pixoo64 显示 | 含义 |
| --- | --- | --- |
| 请勿打扰 | 蓝色系动画（禁止环 / 波纹 / 霓虹字） | 不想被打扰 |
| 可以找我 | 绿色系 + 彩虹点缀（笑脸 / 爱心 / 星光） | 可以打扰 |

全部图案不含红色：请勿打扰用蓝色系标识，可以找我用橙色、黄色、
绿色、青色、紫色。

![请勿打扰](designs_busy.png)
![可以找我](designs_free.png)

## 直接运行（本机）

依赖 Python 3.9+ 和 Pillow：

```bash
python3 -m pip install Pillow
python3 status_server.py
```

启动后：

- 本机访问 <http://127.0.0.1:8000>
- 局域网内手机/电脑访问 `http://<Mac 的局域网 IP>:8000`（启动时会打印）

网页上点按钮即可切换，状态和当前图案序号会保存在 `status.json`，
重启程序后自动恢复并重新同步到设备。轮播间隔可用 `STATUS_ROTATE`
环境变量调整（默认 15 秒）。

## Docker 部署（NAS）

项目自带中文字体（OFL 开源协议），容器内无需额外安装字体。

### 自动更新（CI/CD）

推送到 GitHub 的 `main` 分支后，GitHub Actions 会自动构建 Docker
镜像并推送到 `ghcr.io/miharia/pixoo64-status:latest`（同时支持
amd64 / arm64）。NAS 上运行的 Watchtower 每 12 小时检查一次新镜像，
发现更新就自动拉取并重启容器；网页上也有“检查更新”按钮可以
随时手动触发——之后的更新只需要：

```bash
git add -A && git commit -m "改动" && git push
```

部署完成后，第一次使用请打开网页，在“Pixoo 设备 IP”输入框里填上
设备的局域网地址并点“保存设备”，之后状态切换才会推送到设备。

NAS 端一次性配置：

1. **让 NAS 能拉取镜像**（二选一）：
   - 推荐：把仓库设为公开（`gh repo edit miharia/pixoo64-status
     --visibility public`），ghcr.io 镜像也随之公开，NAS 无需登录；
   - 保持私有：在 NAS 上执行 `echo <GitHub 令牌> | docker login
     ghcr.io -u miharia --password-stdin`（令牌需要 `read:packages`
     权限）。
2. 把仓库克隆/拷贝到 NAS 上，在项目目录执行：

```bash
docker compose up -d
```

`docker compose up -d` 会同时启动状态服务和 Watchtower。
浏览器打开 `http://<NAS 的局域网 IP>:10004` 即可使用。

状态和预览图会持久化在宿主机 `./data` 目录，容器重启、升级都不会丢。
也可以用 `docker compose up -d` 随时手动拉取最新镜像。
配置项：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `STATUS_PORT` | `8000` | 网页端口 |
| `STATUS_ROTATE` | `15` | 图案轮播间隔（秒） |
| `UPDATE_URL` | `http://watchtower:8080/v1/update` | 网页更新按钮的转发地址 |
| `UPDATE_TOKEN` | `pixoo-status-update` | 更新按钮与 Watchtower 的鉴权令牌 |
| `STATE_FILE` | `/app/data/status.json` | 状态文件路径 |
| `PREVIEW_FILE` | `/app/data/status_preview.png` | 预览图路径 |

注意：Pixoo 设备 IP 不在 compose 里配置，打开网页填写即可，会保存在
`./data/status.json` 中。

## 配置（直接运行时）

可用环境变量覆盖默认值：

```bash
STATUS_PORT=8000 STATUS_ROTATE=15 python3 status_server.py
```

设备 IP 在网页上配置；也可以用 `PIXOO_IP` 环境变量作为首次启动的初始值。

## 文件

- `status_server.py` — 网页服务 + 图案生成 + Pixoo 推送
- `fonts/` — 随项目分发的中文字体（ZCOOL KuaiLe，OFL 协议）
- `Dockerfile` / `docker-compose.yml` — NAS 容器化部署
- `designs_busy.png` / `designs_free.png` — 图案效果预览
