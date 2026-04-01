#!/bin/sh
# Start E3onCANserver in DoIP mode.
# Run from the project root directory.
cd "$(dirname "$0")/.."
docker compose -f docker-compose.yml -f docker-compose.doip.yml "$@"
