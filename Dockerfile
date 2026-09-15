FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY pyproject.toml README.md ./
COPY core ./core
COPY wb_mcp ./wb_mcp
COPY ozon_mcp ./ozon_mcp
COPY ozon_perf_mcp ./ozon_perf_mcp

RUN pip install --no-cache-dir . \
    && mkdir -p /opt/yandex \
    && python -c "import urllib.request; urllib.request.urlretrieve('https://storage.yandexcloud.net/cloud-certs/CA.pem','/opt/yandex/YandexInternalRootCA.crt')"

ENV PORT=8080
EXPOSE 8080

CMD ["python", "-m", "core.remote"]
