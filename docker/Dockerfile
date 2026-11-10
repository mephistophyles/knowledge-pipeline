# Pipeline image: orchestrator, workers, dashboard. Inference runs on the host.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# git is needed by the vault writer; ca-certificates for HTTPS.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first for layer caching. uv.lock pins the resolution.
COPY pyproject.toml uv.lock README.md ./
COPY pipeline ./pipeline
COPY dashboard ./dashboard
RUN uv sync --no-dev --frozen

# Config is bind-mounted in compose so edits don't require a rebuild; copy a
# default so the image also runs standalone.
COPY config ./config

ENV PATH="/app/.venv/bin:${PATH}"
ENTRYPOINT ["pipeline"]
CMD ["--help"]
