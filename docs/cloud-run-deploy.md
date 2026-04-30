# Google Cloud Run 部署指南

本文档介绍如何把 `daily_stock_analysis_server` 部署到 Google Cloud Run，并通过 HTTP 接口触发分析。

服务端入口为根目录的 [`server.py`](../server.py)（复用 `api.app:app` 全部接口，并叠加 Cloud Run 友好端点），Dockerfile 为根目录的 [`Dockerfile`](../Dockerfile)。

---

## 1. Cloud Run 接口速览

| 方法 | 路径 | 鉴权 | 说明 |
| --- | --- | --- | --- |
| GET | `/` | Admin Cookie | 前端 SPA 或引导页 |
| GET | `/login` | 否 | 管理员登录页 |
| POST | `/login` | 否 | 提交密码登录 |
| GET/POST | `/logout` | Admin Cookie | 清空登录态 |
| GET | `/info` | 否 | Cloud Run JSON 服务信息 |
| GET | `/health` | 否 | 健康检查，返回 `{"status":"healthy"}` |
| GET | `/api/health` | 否 | 同上，兼容旧路径 |
| GET | `/docs` | 生产需 Admin Cookie | OpenAPI 文档 |
| POST | `/analyze` | API_TOKEN Bearer | 同步分析 |
| POST | `/analyze/async` | API_TOKEN Bearer | 异步分析，立即返回 `task_id` |
| GET | `/tasks/{task_id}` | API_TOKEN Bearer | 查询异步任务状态 |
| `*` | `/api/v1/*` | 视配置 | 完整 API（历史记录、回测、组合管理等） |

服务器同时提供两套独立的鉴权层：

| 鉴权层 | 保护对象 | 触发条件 |
| --- | --- | --- |
| **Admin 登录（Session Cookie）** | 浏览器访问的前端页面（`/`, SPA 路由），生产环境的 `/docs`、`/redoc`、`/openapi.json` | 设置 `ADMIN_PASSWORD` 后自动启用；登录页 `/login` |
| **API_TOKEN（Bearer Header）** | `/analyze`、`/analyze/async`、`/tasks/{id}` | 设置 `API_TOKEN` 后自动启用；请求需带 `Authorization: Bearer <API_TOKEN>` |

两套鉴权互不影响：API 调用方走 Bearer，浏览器使用方走 Cookie，部署到 Cloud Run 时建议 **同时配置** 两者。

---

## 2. 本地裸跑（不打 Docker）

```bash
pip install -r requirements.txt
cp .env.example .env   # 按需填入 API Key
uvicorn server:app --host 0.0.0.0 --port 8080
# 或：python server.py
```

健康检查：
```bash
curl http://127.0.0.1:8080/health
# {"status":"healthy"}
```

> 注意：`python main.py --serve-only` 现在会先读取 `PORT`，未设置时再读 `WEBUI_PORT`，最后回退到 `8000`。

---

## 3. 本地 Docker 测试

```bash
docker build -t daily-stock-analysis-server .
docker run --rm \
  -p 8080:8080 \
  --env-file .env \
  daily-stock-analysis-server
```

容器内会执行：
```
uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080}
```

调用接口（dry_run 不会真正消耗 LLM 配额）：
```bash
curl -X POST http://127.0.0.1:8080/analyze \
  -H "Content-Type: application/json" \
  -d '{"stocks":["NVDA"],"market":"us","dry_run":true}'
```

---

## 4. Web UI 管理员登录（Admin Login）

部署到 Cloud Run 之后，浏览器访问 `https://<your-cloud-run-url>/` 会被重定向到 `/login`。
未配置 `ADMIN_PASSWORD` 时前端开放（开发模式），生产环境强烈建议设置。

### 4.1 生成 SESSION_SECRET_KEY

```bash
python - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

把输出作为 `SESSION_SECRET_KEY`。这是给 cookie 做 HMAC 签名用的密钥，多实例必须共享同一个值，否则一个实例颁发的 cookie 在另一个实例就会失效。

### 4.2 设置环境变量

| 变量 | 作用 | 推荐值 |
| --- | --- | --- |
| `ADMIN_PASSWORD` | 管理员密码，不会被写入 cookie / log | 一个强密码 |
| `SESSION_SECRET_KEY` | cookie 签名密钥 | 上一步的随机串 |
| `ENV` | `production` 时启用 https-only cookie，并把 `/docs` 也纳入登录保护 | `production` |
| `SESSION_MAX_AGE_SECONDS` | 登录态 TTL（秒） | 默认 43200（12h） |

### 4.3 访问流程

1. 打开 `https://<url>/` → 自动跳到 `/login?next=/`
2. 输入 `ADMIN_PASSWORD` → 服务端用 `hmac.compare_digest` 校验
3. 校验通过 → `request.session["is_admin"] = True`，签名 cookie 落到浏览器，重定向到 `next`
4. 失败 → 仍停留在 `/login`，提示 `Invalid admin password`
5. `GET /logout` 或 `POST /logout` → 清空 session，跳回 `/login`

`/health`、`/api/health`、`/info`、`/login`、`/logout` 永远公开（保证 Cloud Run 健康检查与登录页可达）。
`/analyze*` 与 `/tasks/*` 走 `API_TOKEN` Bearer 校验，**不会被前端管理员登录拦截**，方便程序化调用。

> 注意：`--allow-unauthenticated` 只表示 Cloud Run URL 任何人都可以打开，但应用内部仍需要管理员密码登录，等价于"对外暴露 + 应用层鉴权"。需要更高强度可改为 Cloud Run IAM。

---

## 5. 从源码部署到 Cloud Run

```bash
gcloud run deploy daily-stock-analysis-server \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --concurrency 4 \
  --set-env-vars ENV=production,ADMIN_PASSWORD=YOUR_STRONG_PASSWORD,SESSION_SECRET_KEY=YOUR_RANDOM_SECRET,API_TOKEN=YOUR_BEARER_TOKEN,STOCK_LIST=NVDA,AAPL,MARKET=us,DRY_RUN=false
```

> `--set-env-vars` 中含逗号的值需要转义；含敏感值时建议改用 `--env-vars-file` 或 Secret Manager。

要点：
- `--source .` 触发 Cloud Build 自动使用根目录 `Dockerfile`
- `--timeout 3600`：分析任务可能跑数分钟，最长可设到 60 分钟
- `--concurrency 4`：单实例并发数，默认 80 对量化任务过高
- `--allow-unauthenticated` 公网开放；如果用 IAM 鉴权请删除并改用 IAP / Cloud IAM

把 LLM Key 等敏感变量通过 `--set-env-vars` 传入很啰嗦，推荐 `--env-vars-file`。

---

## 6. 用 env 文件批量注入变量

`production.env`（请勿提交到 git）示例：

```yaml
# YAML 格式，gcloud 会按 --env-vars-file 解析
STOCK_LIST: "NVDA,AAPL,600519"
MARKET: "us"
DRY_RUN: "false"
API_TOKEN: "<your-strong-random-token>"

# AI Keys（任填一个即可）
GEMINI_API_KEY: ""
AIHUBMIX_KEY: ""
ANTHROPIC_API_KEY: ""
OPENAI_API_KEY: ""
OPENAI_BASE_URL: ""
OPENAI_MODEL: ""

# 推送（可选）
WECHAT_WEBHOOK_URL: ""
FEISHU_WEBHOOK_URL: ""
TELEGRAM_BOT_TOKEN: ""
TELEGRAM_CHAT_ID: ""
DISCORD_WEBHOOK_URL: ""
SLACK_BOT_TOKEN: ""
SLACK_CHANNEL_ID: ""
```

部署：
```bash
gcloud run deploy daily-stock-analysis-server \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --memory 2Gi \
  --cpu 2 \
  --timeout 3600 \
  --concurrency 4 \
  --env-vars-file production.env
```

---

## 7. 调用接口

```bash
URL=https://daily-stock-analysis-server-xxxxx.a.run.app
TOKEN=your-strong-random-token

# 健康检查
curl "$URL/health"

# 同步分析（一只股票，dry_run 演练）
curl -X POST "$URL/analyze" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"stocks":["NVDA"],"market":"us","notify":false,"dry_run":true}'

# 异步分析
curl -X POST "$URL/analyze/async" \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"stocks":["NVDA","AAPL"],"market":"us"}'
# => {"success": true, "task_id": "abc123...", "status": "pending", ...}

# 查询状态
curl -H "Authorization: Bearer $TOKEN" "$URL/tasks/abc123..."
```

---

## 8. 常见错误排查

| 现象 | 根因 | 处理 |
| --- | --- | --- |
| `Container failed to start and listen on PORT=8080` | 容器启动后没有进程监听 `${PORT}` 或绑定到了 127.0.0.1 | 确认 CMD 是 `uvicorn ... --host 0.0.0.0 --port ${PORT:-8080}`；不要硬编码 8000 |
| 健康检查 503 | 应用在 import 阶段抛错；常见为缺 API Key 或网络抖动 | 看 Cloud Run 日志，先用 `DRY_RUN=true` + 不填密钥跑通启动路径 |
| `/analyze` 504 超时 | 分析任务比 Cloud Run 请求超时长 | 用 `/analyze/async`；或调高 `--timeout`（最大 3600s） |
| 401 Unauthorized | 设置了 `API_TOKEN` 但请求没带 `Authorization: Bearer ...` | 加 header；本地调试可暂时清空 `API_TOKEN` |
| `ModuleNotFoundError: No module named 'fastapi'` | requirements 缺依赖（极少见） | 确认 `requirements.txt` 包含 `fastapi`、`uvicorn[standard]`、`pydantic` |
| 镜像构建超过 10 分钟超时 | Cloud Build 默认配额 | `gcloud builds submit --timeout=1800s` 或拆 `requirements.txt` |
| 港股 / 美股拉数据失败 | 数据源需要外网 + API Key | 确保 Longbridge / yfinance 等环境变量已配置；Cloud Run 出网默认走 Google IP |
| 浏览器一直被重定向回 `/login` | 多实例部署但 `SESSION_SECRET_KEY` 没显式设置 | 给所有实例配置同一个 `SESSION_SECRET_KEY`；不要让进程自动生成 |
| `/login` 提交后报 `Invalid admin password` | 输入与 `ADMIN_PASSWORD` 不一致；或 Cloud Run 还在跑旧版本 | 先 `gcloud run services describe ... --format='value(spec.template.spec.containers[0].env)'` 核实当前生效的环境变量 |
| 登录后浏览器没有 cookie | `ENV=production` + 实际是 http 访问 | 走 https，或本地调试时把 `ENV` 设为 `development` |

---

## 9. 与原有运行方式的关系

Cloud Run 部署只是**新增**一种部署方式，不破坏原有 CLI：

- `python main.py`、`--schedule`、`--stocks ...`、`--market-review`、`--webui` 全部保留
- `python main.py --serve-only` 现在会自动读取 `PORT` 环境变量（Cloud Run 注入）
- GitHub Actions 流水线不受影响

如需**同时打包前端**，请使用 [`docker/Dockerfile`](../docker/Dockerfile)（多阶段构建：node 编译 SPA + python 运行后端）。根目录 `Dockerfile` 是面向 Cloud Run 的精简版本：未构建前端时由 `api.app` 渲染引导页。
