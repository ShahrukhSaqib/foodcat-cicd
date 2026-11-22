# One image per service. The same recipe builds all of them, and a build argument picks the service:
#   docker build --build-arg SERVICE=order_service -t foodcat-order .
# SERVICE is the name of a file in the foodcat folder: gateway, user_service, order_service,
# payment_service, delivery_service or alert_lambda.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    FOODCAT_DATA_DIR=/data \
    FOODCAT_QUEUE_DIR=/queues

# A normal user instead of root. /data holds the service's own database, /queues the shared queue file.
RUN useradd --system --uid 10001 --no-create-home app \
 && mkdir /data /queues \
 && chown app:app /data /queues

WORKDIR /app

# The packages come first, so Docker can reuse this layer until requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY foodcat ./foodcat
COPY web ./web
COPY VERSION ./

USER app

ARG SERVICE=gateway
ENV SERVICE=${SERVICE}

# Compose sets PORT for every container. The check asks the service's own /health route.
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('PORT', '8000'), timeout=2)"]

CMD ["sh", "-c", "exec python -m foodcat.${SERVICE}"]
