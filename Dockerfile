# syntax=docker/dockerfile:1

# ---------------------------------------------------------------- build
# Install dependencies into a self-contained virtualenv. build-essential /
# libffi-dev are a fallback for any package that lacks a cp314 wheel; they
# never reach the runtime image. If every wheel resolves, this apt layer can
# be dropped.
FROM python:3.14-slim AS build

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential libffi-dev \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /venv
ENV PATH="/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------------------------------------------------------------- test
# `docker build --target test .` runs the suite inside the image.
FROM build AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY . .
RUN python -m pytest -q

# ---------------------------------------------------------------- runtime
FROM python:3.14-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/venv/bin:$PATH" \
    # Where to find the OPC-UA server. Override per environment.
    SERVER_URL=opc.tcp://host.docker.internal:4840/ganymede/server/

COPY --from=build /venv /venv

WORKDIR /app
COPY app ./app

# Non-root, with a writable data dir for saved watchlists. Override the uid/gid
# at build time to match your host user if you'd rather bind-mount app/data:
#   docker compose build --build-arg APP_UID=$(id -u) --build-arg APP_GID=$(id -g)
ARG APP_UID=10001
ARG APP_GID=10001
RUN groupadd --system --gid ${APP_GID} app \
 && useradd --system --uid ${APP_UID} --gid ${APP_GID} --create-home app \
 && mkdir -p /app/app/data \
 && chown -R app:app /app/app/data
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=2)"]

# Single process on purpose: the Store, the WebSocket hub and the active
# watchlists are in-memory, so multiple uvicorn workers would each see a
# different slice of the world. Scale with more containers, not workers.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
