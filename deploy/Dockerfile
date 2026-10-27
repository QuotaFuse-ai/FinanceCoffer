# syntax=docker/dockerfile:1

# Build stage: resolve and install the application into an isolated virtualenv.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src ./src

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install ".[api,db,etl,vector,s3,mcp]"

# Runtime stage: no compilers or build caches, non-root by default.
FROM python:3.12-slim AS runtime

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN groupadd --system --gid 10001 fcoffer \
    && useradd --system --uid 10001 --gid fcoffer --home-dir /app --shell /usr/sbin/nologin fcoffer \
    && mkdir -p /app \
    && chown fcoffer:fcoffer /app

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
USER 10001:10001

EXPOSE 8100

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=4)"]

ENTRYPOINT ["fcoffer"]
CMD ["--help"]
