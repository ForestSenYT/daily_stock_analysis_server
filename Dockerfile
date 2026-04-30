# ===================================
# Daily Stock Analysis - Cloud Run Dockerfile
# ===================================
# 设计目标：
# 1. 直接配合 ``gcloud run deploy --source .`` 使用
# 2. 监听 0.0.0.0:${PORT}，PORT 由 Cloud Run 注入（默认 8080）
# 3. 容器启动后持续运行 HTTP 服务（uvicorn）
# 4. 前端是可选的：未构建时由 api.app 兜底渲染引导页
#
# 如需同时打包前端（apps/dsa-web），请使用 docker/Dockerfile（多阶段构建）。

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    PORT=8080 \
    HOST=0.0.0.0 \
    LOG_DIR=/app/logs \
    DATABASE_PATH=/app/data/stock_analysis.db

WORKDIR /app

# 系统依赖：
# - gcc / build-essential: 部分轮子需要本地编译
# - curl: 健康检查 / 调试
# - libgl1 / libglib2.0-0: pandas / opencv 等可选依赖运行时
# - libxrender1 / libxext6 / libjpeg62-turbo / fontconfig: imgkit + wkhtmltopdf 渲染
# - tzdata: 设置时区
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        build-essential \
        curl \
        ca-certificates \
        tzdata \
        fontconfig \
        libjpeg62-turbo \
        libxrender1 \
        libxext6 \
        libgl1 \
        libglib2.0-0 \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

# 先单独复制 requirements，让依赖层可缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码（.dockerignore 会过滤掉 .env / venv / cache 等）
COPY . .

# 数据 / 日志目录
RUN mkdir -p /app/data /app/logs /app/reports

# Cloud Run 仅暴露一个端口，与 ${PORT} 保持一致
EXPOSE 8080

# 容器内健康检查（Cloud Run 自身会探测 /health）
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT:-8080}/health" || exit 1

# 启动命令：监听 0.0.0.0:${PORT}，默认 8080
# 注意：使用 sh -c 以便展开 ${PORT} 环境变量；Cloud Run 会注入 PORT
CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips=*"]
