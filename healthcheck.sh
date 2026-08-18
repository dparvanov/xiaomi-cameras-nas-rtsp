#!/bin/sh
# Uses the local Compose config mount when present, otherwise the image default.
set -eu
config_path=${BRIDGE_CONFIG:-/config/config.json}
exec python /app/xiaomi_rtsp_bridge.py --config "$config_path" --healthcheck
