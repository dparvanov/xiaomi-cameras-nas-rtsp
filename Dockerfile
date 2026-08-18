FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY xiaomi_rtsp_bridge.py settings_store.py webapp.py healthcheck.sh /app/
COPY config.example.json /app/config.default.json

# The registry image ships this safe production default. The local Compose route
# still mounts /config/config.json and therefore does not use it by default.
RUN RTSP_PUBLISH_PASSWORD=build-validation-password \
    python -c "from pathlib import Path; from xiaomi_rtsp_bridge import BridgeConfig; BridgeConfig.from_json(Path('/app/config.default.json'))"
RUN chmod 0555 /app/healthcheck.sh

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--access-logfile", "-", "--error-logfile", "-", "webapp:create_app()"]
