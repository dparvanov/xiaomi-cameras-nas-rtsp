#!/bin/sh
# Validate local deployment prerequisites without sourcing or printing secrets.
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
env_file=$project_dir/.env

fail() {
    printf '%s\n' "Preflight failed: $*" >&2
    exit 1
}

value_for() {
    key=$1
    sed -n "s/^${key}=//p" "$env_file" | tail -n 1
}

require_secret() {
    key=$1
    value=$(value_for "$key")
    case $value in
        ''|*REPLACE_WITH*|*replace-with*|*change-me*|*password*|*PASSWORD*)
            fail "$key in .env is missing or still a placeholder"
            ;;
    esac
}

require_port() {
    key=$1
    value=$(value_for "$key")
    case $value in
        ''|*[!0-9]*) fail "$key must be a numeric TCP port" ;;
    esac
    [ "$value" -ge 1 ] && [ "$value" -le 65535 ] || fail "$key must be between 1 and 65535"
}

[ -f "$env_file" ] || fail "copy .env.example to .env and configure it first"
[ -f "$project_dir/config/config.json" ] || fail "copy config.example.json to config/config.json first"

require_secret RTSP_PUBLISH_PASSWORD
require_port RTSP_PORT
require_port SETUP_UI_PORT

recordings_path=$(value_for XIAOMI_RECORDINGS_PATH)
[ -n "$recordings_path" ] || fail "XIAOMI_RECORDINGS_PATH is required"
[ -d "$recordings_path" ] || fail "XIAOMI_RECORDINGS_PATH is not a readable host directory"
[ -r "$recordings_path" ] || fail "XIAOMI_RECORDINGS_PATH is not readable"

command -v docker >/dev/null 2>&1 || fail "docker is not available"
(cd "$project_dir" && docker compose --env-file .env config -q) || fail "Docker Compose configuration is invalid"

printf '%s\n' "Preflight passed: recordings path, required values, ports, and Compose configuration are valid."
