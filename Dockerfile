# bookworm закреплён: в нём есть libengine-gost-openssl (проверка подписи ГОСТ выписок ЕГРН),
# в trixie пакета нет.
FROM python:3.12-slim-bookworm

# OMP_THREAD_LIMIT=1: tesseract по одному потоку, документы распознаются параллельно (ocr.py)
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 OMP_THREAD_LIMIT=1
WORKDIR /app

# tesseract-ocr + rus: OCR сканов документов администраций («первоисточники», ТЗ п. 2.5)
RUN apt-get update && apt-get install -y --no-install-recommends openssl libengine-gost-openssl \
    tesseract-ocr tesseract-ocr-rus \
    && rm -rf /var/lib/apt/lists/*

ARG ASR_LOCAL=0
COPY pyproject.toml README.md constraints.txt ./
COPY landcheck ./landcheck
# constraints.txt: версии, с которыми принят прод — сборка без правок кода не тянет новые пакеты
RUN if [ "$ASR_LOCAL" = "1" ]; then pip install --no-cache-dir -c constraints.txt ".[asr-local]"; \
    else pip install --no-cache-dir -c constraints.txt .; fi

ENV LANDCHECK_DB=/data/landcheck.sqlite \
    LANDCHECK_CACHE=/data/nspd-cache.sqlite \
    HF_HOME=/data/hf
VOLUME ["/data"]
EXPOSE 8000
CMD ["landcheck", "web", "--host", "0.0.0.0", "--port", "8000"]
