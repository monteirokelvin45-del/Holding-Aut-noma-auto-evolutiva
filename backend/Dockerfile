# =============================================================================
# Imagem de produção do backend da Automaton Holding Corp.
# Multi-stage: as dependências compilam numa camada; a imagem final corre
# como utilizador não-root e sem toolchain.
# =============================================================================
FROM python:3.11-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libssl-dev \
        libffi-dev \
        cargo \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY backend/requirements.txt /build/requirements.txt
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install -r requirements.txt


FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    APP_ENV=production \
    HOST=0.0.0.0 \
    PORT=8000 \
    VAULT_DIR=/app/vault_data

# ffmpeg + fontes (legendas) + curl para healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        curl \
        ca-certificates \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

# Utilizador sem privilégios
RUN groupadd --system --gid 1001 automaton \
    && useradd --system --uid 1001 --gid automaton --create-home automaton

WORKDIR /app
COPY --chown=automaton:automaton backend /app/backend
COPY --chown=automaton:automaton vault_data /app/vault_data

RUN mkdir -p /app/vault_data/runtime /app/vault_data/media /app/vault_data/artifacts \
    && chown -R automaton:automaton /app/vault_data

USER automaton

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--workers", "1"]
