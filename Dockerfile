FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CACHE_PATH=/cache

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY immich_sync.py processor.py frame_client.py ./

VOLUME ["/cache"]

# Healthy while the heartbeat (touched after each photo and each finished cycle) is fresh:
# two sync intervals plus 5 minutes of slack.
HEALTHCHECK --interval=5m --timeout=10s --start-period=10m \
  CMD python -c "import os,sys,time; p='/cache/heartbeat'; limit=int(os.getenv('SYNC_INTERVAL_MINUTES','15'))*120+300; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < limit else 1)"

CMD ["python", "-u", "immich_sync.py"]
