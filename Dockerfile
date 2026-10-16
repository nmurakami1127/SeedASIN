FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MARKETPLACE=""

WORKDIR /app

COPY pyproject.toml ./
COPY . .

RUN pip install --no-cache-dir .

# 実行対象（batch/admin/apiのどのエントリポイントか、MARKETPLACE=US|JPのどちらを固定するか）は
# CronJob/Deployment側でCMDを上書きして指定する。1イメージをUS/JP・バッチ/オンライン全用途で共用する。
CMD ["python", "-c", "print('SeedASIN image ready. Override CMD, e.g.: python -m batch.b3_discovery --marketplace=US --source=<name>')"]
