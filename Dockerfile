# syntax=docker/dockerfile:1

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data \
    OUTPUT_DIR=/output

WORKDIR /app

# Dependencies first so the layer is cached across code changes.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --upgrade pip && pip install .

COPY pricing.toml ./

# The database and the generated audio live on volumes.
RUN mkdir -p /data /output && \
    useradd --create-home --uid 10001 appuser && \
    chown -R appuser:appuser /app /data /output
USER appuser

VOLUME ["/data", "/output"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
