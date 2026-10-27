# The public demo (Render). It haunts public GitHub repos only; run `python -m seance` locally for your own.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir fastapi "uvicorn[standard]" httpx
COPY seance ./seance
COPY static ./static

ENV SEANCE_PUBLIC=1 \
    SEANCE_BACKEND=openai \
    SEANCE_CLONE_ROOT=/tmp/seance-repos \
    SEANCE_CACHE=/tmp/seance-cache.json \
    PYTHONUNBUFFERED=1

CMD ["sh", "-c", "uvicorn seance.server:app --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
