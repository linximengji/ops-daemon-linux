#!/usr/bin/env bash
# Start Pact Broker + Jaeger via Docker Compose
set -e

COMPOSE_FILE="$(dirname "$0")/docker-compose.yml"

cd "$(dirname "$0")"

if ! docker info > /dev/null 2>&1; then
    echo "Docker is not running. Start Docker Desktop first."
    exit 1
fi

echo "Starting Pact Broker (port 9292) + Jaeger (port 16686/4317)"
docker compose -f "$COMPOSE_FILE" up -d

echo ""
echo "Pact Broker: http://localhost:9292"
echo "Jaeger UI:   http://localhost:16686"
echo "OTLP gRPC:   http://localhost:4317"
