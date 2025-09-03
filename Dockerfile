# Python slim base
FROM python:3.13-slim

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
COPY PUMA13_OKD.py /app/PUMA13_OKD.py
COPY PUMA3_RFA.py /app/PUMA3_RFA.py
COPY PUMA4_RFPO.py /app/PUMA4_RFPO.py
COPY PUMA17_PO.py /app/PUMA17_PO.py
COPY PUMA6_RR.py /app/PUMA6_RR.py
COPY PUMA5_RFPS.py /app/PUMA5_RFPS.py
COPY PUMA1_DR.py /app/PUMA1_DR.py

# Entrypoint writes secrets from env and runs the master
COPY cloud_entrypoint.py /app/cloud_entrypoint.py

CMD ["python", "/app/cloud_entrypoint.py"]
