# Base image pinned by digest (multi-arch index); Dependabot proposes bumps.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f AS builder

WORKDIR /build

# build deps
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# grpcio-tools generates the stubs; it is build-only and never reaches the
# runtime stage. Keep it at the same version as grpcio.
RUN pip install --no-cache-dir -r requirements.txt grpcio-tools==1.84.0

# download + generate protobufs (at the commit pinned in the script)
COPY scripts/download_protobufs.sh scripts/
RUN chmod +x scripts/download_protobufs.sh && ./scripts/download_protobufs.sh protobufs

COPY proto/ proto/
COPY scripts/generate_protos.sh scripts/
RUN chmod +x scripts/generate_protos.sh && ./scripts/generate_protos.sh

# runtime
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --from=builder /build/generated/ /app/generated/
COPY src/ /app/src/
COPY config.yaml /app/config.yaml

ENV PYTHONPATH=/app/src:/app/generated
ENV FLOODGATE_CONFIG=/app/config.yaml
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

EXPOSE 9000 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

USER nobody

ENTRYPOINT ["python", "-m", "floodgate"]
