FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

ARG ASR_LOCAL=0
COPY pyproject.toml README.md ./
COPY landcheck ./landcheck
RUN if [ "$ASR_LOCAL" = "1" ]; then pip install --no-cache-dir ".[asr-local]"; \
    else pip install --no-cache-dir .; fi

ENV LANDCHECK_DB=/data/landcheck.sqlite \
    LANDCHECK_CACHE=/data/nspd-cache.sqlite \
    HF_HOME=/data/hf
VOLUME ["/data"]
EXPOSE 8000
CMD ["landcheck", "web", "--host", "0.0.0.0", "--port", "8000"]
