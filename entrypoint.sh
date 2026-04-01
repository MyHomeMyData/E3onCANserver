#!/bin/sh
# entrypoint.sh – start E3onCANserver in CAN or DoIP mode.
#
# Environment variables (all have defaults):
#
#   MODE         "can" or "doip"          (default: can)
#   DEVICES      path to devices.json     (default: config/devices.json)
#   CAN_IFACE    socketcan interface      (default: vcan0)
#   DOIP_ADDR    host:port for DoIP       (default: 0.0.0.0:13400)
#   DELAY        inter-frame delay ms     (default: 0)
#   ERRORS       fault injection rate %   (default: 0)
#   LOG_LEVEL    DEBUG/INFO/WARNING/ERROR (default: INFO)

MODE="${MODE:-can}"
DEVICES="${DEVICES:-config/devices.json}"
CAN_IFACE="${CAN_IFACE:-vcan0}"
DOIP_ADDR="${DOIP_ADDR:-0.0.0.0:13400}"
DELAY="${DELAY:-0}"
ERRORS="${ERRORS:-0}"
LOG_LEVEL="${LOG_LEVEL:-INFO}"

echo "E3onCANserver starting in ${MODE} mode"
echo "  devices : ${DEVICES}"
echo "  log     : ${LOG_LEVEL}"

case "${MODE}" in
  can)
    echo "  interface : ${CAN_IFACE}"
    exec python main.py \
      --devices   "${DEVICES}" \
      --interface socketcan \
      --channel   "${CAN_IFACE}" \
      --delay     "${DELAY}" \
      --errors    "${ERRORS}" \
      --log-level "${LOG_LEVEL}"
    ;;
  doip)
    echo "  listen  : ${DOIP_ADDR}"
    exec python main.py \
      --devices   "${DEVICES}" \
      --doip      "${DOIP_ADDR}" \
      --delay     "${DELAY}" \
      --errors    "${ERRORS}" \
      --log-level "${LOG_LEVEL}"
    ;;
  *)
    echo "ERROR: unknown MODE '${MODE}'. Use 'can' or 'doip'." >&2
    exit 1
    ;;
esac
