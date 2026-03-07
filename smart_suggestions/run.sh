#!/usr/bin/with-contenv bashio

export SUPERVISOR_TOKEN="${SUPERVISOR_TOKEN}"
export OLLAMA_URL="$(bashio::config 'ollama_url')"
export OLLAMA_MODEL="$(bashio::config 'ollama_model')"
export REFRESH_INTERVAL="$(bashio::config 'refresh_interval')"
export MAX_SUGGESTIONS="$(bashio::config 'max_suggestions')"
export HISTORY_HOURS="$(bashio::config 'history_hours')"

bashio::log.info "Starting Smart Suggestions add-on..."
exec python3 /app/main.py
