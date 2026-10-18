# syntax=docker/dockerfile:1

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    DATA_DIR=/data \
    PORT=8888 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖层单独缓存：只有 requirements 变化才重装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Chromium + 系统库，供 WAF 站点使用浏览器通道
RUN python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* /root/.cache

COPY app ./app

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /data /app

USER appuser

EXPOSE 8888

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8888/healthz',timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8888", "--no-access-log"]
