#!/usr/bin/env bash
# Stop Pact Broker + Jaeger
COMPOSE_FILE="$(dirname "$0")/../docker-compose.yml"
cd "$(dirname "$0")/.."
docker compose -f "$COMPOSE_FILE" down
