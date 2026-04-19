# Spyoncino — CPU inference. GPU image: Dockerfile.gpu (CI publishes :latest and :latest-gpu).
# Expects a mounted data directory with config/recipe.yaml and config/secrets.yaml.
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# PyTorch CPU wheels (Ultralytics/YOLO); keep separate layer for cache reuse.
RUN pip install --upgrade pip && \
    pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN pip install .

# Recipe uses data_root: "data" relative to cwd; secrets_path is cwd-relative (e.g. data/config/secrets.yaml).
VOLUME ["/app/data"]

EXPOSE 8000

# Matches default webapp port in sample recipe; disable or override if web is off or port differs.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/health || exit 1

CMD ["spyoncino", "data/config/recipe.yaml"]
