FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY landcheck ./landcheck
RUN pip install --no-cache-dir .

ENV LANDCHECK_DB=/data/landcheck.sqlite \
    LANDCHECK_CACHE=/data/nspd-cache.sqlite
VOLUME ["/data"]
EXPOSE 8000
CMD ["landcheck", "web", "--host", "0.0.0.0", "--port", "8000"]
