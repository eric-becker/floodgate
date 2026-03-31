FROM python:3.13-slim AS builder

WORKDIR /build

# build deps
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# download + generate protobufs
COPY scripts/download_protobufs.sh scripts/
RUN chmod +x scripts/download_protobufs.sh && ./scripts/download_protobufs.sh protobufs

COPY proto/ proto/
COPY scripts/generate_protos.sh scripts/
RUN chmod +x scripts/generate_protos.sh && ./scripts/generate_protos.sh

# runtime
FROM python:3.13-slim

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

EXPOSE 9000

USER nobody

ENTRYPOINT ["python", "-m", "floodgate"]
