#!/bin/sh
# Start E3onCANserver in CAN mode on vcan1 (second instance).
# Run from the project root directory.
cd "$(dirname "$0")/.."
docker compose -p e3oncanserver-vcan1 -f docker-compose.yml -f docker-compose.can.yml -f docker-compose.can-vcan1.yml "$@"
