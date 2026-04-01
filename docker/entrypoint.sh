#!/bin/sh
# entrypoint.sh – Start E3onCANserver in CAN or DoIP mode.
#
# Environment variables
# ---------------------
# MODE          "can" (default) or "doip"
# DEVICES_FILE  Path to devices.json inside the container relative to docker home
#               (default: config/devices.json)
# CAN_INTERFACE python-can interface type (default: socketcan)
# CAN_CHANNEL   CAN channel name (default: vcan0)
# DOIP_ADDR     DoIP bind address, [HOST:]PORT (default: 0.0.0.0:13400)
# DELAY_MS      Inter-frame delay in ms (default: 0)
# ERROR_PCT     Fault injection rate in % (default: 0)
# LOG_LEVEL     DEBUG | INFO | WARNING | ERROR (default: INFO)

MODE="${MODE:-can}"
DEVICES_FILE="${DEVICES_FILE:-config/devices.json}"
CAN_INTERFACE="${CAN_INTERFACE:-socketcan}"
CAN_CHANNEL="${CAN_CHANNEL:-vcan0}"
DOIP_ADDR="${DOIP_ADDR:-0.0.0.0:13400}"
DELAY_MS="${DELAY_MS:-0}"
ERROR_PCT="${ERROR_PCT:-0}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

set -e

echo "E3onCANserver starting in ${MODE} mode"
echo "  devices file : ${DEVICES_FILE}"
echo "  log level    : ${LOG_LEVEL}"

BASE_ARGS="--devices ${DEVICES_FILE} --delay ${DELAY_MS} --errors ${ERROR_PCT} --log-level ${LOG_LEVEL}"

case "${MODE}" in
  doip)
    echo "  DoIP address : ${DOIP_ADDR}"
    exec python main.py ${BASE_ARGS} --doip "${DOIP_ADDR}"
    ;;
  can)
    echo "  CAN interface: ${CAN_INTERFACE}"
    echo "  CAN channel  : ${CAN_CHANNEL}"
    exec python main.py ${BASE_ARGS} --interface "${CAN_INTERFACE}" --channel "${CAN_CHANNEL}"
    ;;
  *)
    echo "ERROR: unknown MODE '${MODE}'. Use 'can' or 'doip'." >&2
    exit 1
    ;;
esac
