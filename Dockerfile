# Container image for running CLIO on Google Cloud Run.
# Data (NetCDF + Chroma DB) is not baked in; it is mounted from Cloud Storage
# at runtime. See deploy/README.md.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
COPY frontend ./frontend

# Editable install keeps the package at /app/src, so app.py still finds
# /app/frontend relative to its own location.
RUN pip install -e ".[deploy]"

COPY deploy/entrypoint.sh ./deploy/entrypoint.sh
RUN sed -i 's/\r$//' deploy/entrypoint.sh \
    && chmod +x deploy/entrypoint.sh \
    && useradd --create-home --uid 10001 clio

USER clio

CMD ["/app/deploy/entrypoint.sh"]
