FROM python:3.12-slim AS build
RUN python -m venv /venv
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim
ARG VERSION=local
LABEL org.opencontainers.image.title="kia2mqtt" \
      org.opencontainers.image.description="Kia Connect (US) to MQTT bridge" \
      org.opencontainers.image.source="https://github.com/kwv/kia2mqtt" \
      org.opencontainers.image.version="$VERSION"
ENV APP_VERSION=$VERSION \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

RUN useradd --system --uid 1000 --create-home kia \
    && mkdir -p /data && chown kia /data
COPY --from=build /venv /venv
COPY kia2mqtt /app/kia2mqtt
WORKDIR /app
USER kia
VOLUME ["/data"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["/venv/bin/python", "-c", "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]
CMD ["/venv/bin/python", "-m", "kia2mqtt"]
