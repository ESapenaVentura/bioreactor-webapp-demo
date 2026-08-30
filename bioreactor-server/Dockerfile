# syntax=docker/dockerfile:1
# The OPC-UA bioreactor simulator. asyncua is pure-Python; numpy ships a cp314
# wheel — no build toolchain needed.
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LOG_LEVEL=WARNING

WORKDIR /sim
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY server_bioreactor.py serve.py ./

RUN useradd --system --uid 10002 sim
USER sim

EXPOSE 4840

# OPC-UA has no HTTP endpoint — just check the port accepts a connection.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 4840), 2).close()"]

CMD ["python", "serve.py"]
