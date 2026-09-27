FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HF_HOME=/data/models/hf \
    OMP_NUM_THREADS=4

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip freeze | grep -i '^torch==' > /tmp/constraints.txt \
    && pip install -r requirements.txt -c /tmp/constraints.txt

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/codex /data/work /data/models /media \
    && chown -R app:app /data /media /app
COPY --chown=app:app memesearch ./memesearch

USER app
VOLUME ["/data"]
CMD ["python", "-m", "memesearch.worker"]
