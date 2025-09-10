# Python slim base
FROM python:3.12-slim

# System deps (add more if needed, e.g., poppler-utils)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy orchestrator + step scripts
COPY PUMA_Master_v2.py /app/PUMA_Master_v2.py
COPY PUMA_OKD.py /app/PUMA_OKD.py
COPY PUMA_RFA.py /app/PUMA_RFA.py
COPY PUMA_RFPO.py /app/PUMA_RFPO.py
COPY PUMA_PO.py /app/PUMA_PO.py
COPY PUMA_RR.py /app/PUMA_RR.py
COPY PUMA_RFPS.py /app/PUMA_RFPS.py
COPY PUMA_DR.py /app/PUMA_DR.py

# Entrypoint writes secrets from env and runs the master
COPY cloud_entrypoint.py /app/cloud_entrypoint.py

CMD ["python", "/app/cloud_entrypoint.py"]

