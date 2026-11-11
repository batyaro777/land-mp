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
# Слой зависимостей стоит ДО кода и зависит только от pyproject.toml и constraints.txt. Пока список
# зависимостей не менялся, Docker берёт этот слой из кэша: правка кода пересобирает лишь последний,
# маленький слой. Раньше `COPY landcheck` стоял выше `pip install`, и каждая выкладка заново
# собирала ~750 МБ зависимостей — по ~2 ГБ образа и кэша на сервере за выкладку.
COPY pyproject.toml constraints.txt ./
RUN python -c "import os, tomllib; \
project = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
extra = project['optional-dependencies']['asr-local'] if os.environ['ASR_LOCAL'] == '1' else []; \
print('\n'.join(project['dependencies'] + extra))" > /tmp/requirements.txt \
    && pip install --no-cache-dir -c constraints.txt -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY README.md ./
COPY landcheck ./landcheck
# Та же команда, что и раньше: зависимости уже стоят из слоя выше, поэтому здесь ставится только сам
# landcheck. Если список выше разойдётся с pyproject, недостающее доставится тут — итог не изменится.
# constraints.txt: версии, с которыми принят прод — сборка без правок кода не тянет новые пакеты
RUN if [ "$ASR_LOCAL" = "1" ]; then pip install --no-cache-dir -c constraints.txt ".[asr-local]"; \
    else pip install --no-cache-dir -c constraints.txt .; fi

ENV LANDCHECK_DB=/data/landcheck.sqlite \
    LANDCHECK_CACHE=/data/nspd-cache.sqlite \
    HF_HOME=/data/hf
VOLUME ["/data"]
EXPOSE 8000
CMD ["landcheck", "web", "--host", "0.0.0.0", "--port", "8000"]
