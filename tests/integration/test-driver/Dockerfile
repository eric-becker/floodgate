FROM python:3.13-slim

WORKDIR /test-driver

RUN pip install --no-cache-dir \
    "paho-mqtt>=2.0,<3" \
    "meshtastic>=2.5,<3" \
    "cryptography>=42" \
    "requests>=2.32" \
    "pyyaml>=6"

COPY run.py /test-driver/run.py

ENTRYPOINT ["python", "/test-driver/run.py"]
