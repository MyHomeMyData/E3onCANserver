"""
protocol/uds.py – UDS (ISO 14229) protocol handler.

Implemented services
--------------------
* 0x22  ReadDataByIdentifier  (single DID per request)
* 0x2E  WriteDataByIdentifier

Negative response codes used
-----------------------------
* 0x11  serviceNotSupported
* 0x12  subFunctionNotSupported (used for malformed requests)
* 0x31  requestOutOfRange       (DID not present in store)
* 0x33  securityAccessDenied   (placeholder, not enforced in v0.1)

UDS message structure (relevant subset)
----------------------------------------
Request  ReadDataByIdentifier:
    [0x22] [DID_HIGH] [DID_LOW]

Response ReadDataByIdentifier (positive):
    [0x62] [DID_HIGH] [DID_LOW] [DATA ...]

Request  WriteDataByIdentifier:
    [0x2E] [DID_HIGH] [DID_LOW] [DATA ...]

Response WriteDataByIdentifier (positive):
    [0x6E] [DID_HIGH] [DID_LOW]

Negative response (any service):
    [0x7F] [SERVICE_ID] [NRC]

Extension notes
---------------
* Security access (0x27), session control (0x10), and routine control (0x31)
  are intentionally left out of v0.1 but the NRC stubs are already in place.
* To add a service: add a new ``_handle_<name>`` method and register it in
  ``_HANDLERS`` at the bottom of this file.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, Optional

from simulator.datastore import DatapointStore
from simulator.protocol.base import ProtocolHandler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# UDS constants
# ---------------------------------------------------------------------------

# Service IDs
SID_READ_DATA_BY_ID  = 0x22
SID_WRITE_DATA_BY_ID = 0x2E
SID_NEGATIVE_RESP    = 0x7F
POSITIVE_OFFSET      = 0x40  # positive response SID = request SID + 0x40

# Negative Response Codes (NRC)
NRC_SERVICE_NOT_SUPPORTED      = 0x11
NRC_SUBFUNCTION_NOT_SUPPORTED  = 0x12
NRC_REQUEST_OUT_OF_RANGE       = 0x31
NRC_SECURITY_ACCESS_DENIED     = 0x33


def _negative_response(service_id: int, nrc: int) -> bytes:
    """Build a UDS negative response frame."""
    return bytes([SID_NEGATIVE_RESP, service_id, nrc])


class UDSHandler(ProtocolHandler):
    """
    Minimal UDS handler covering ReadDataByIdentifier and WriteDataByIdentifier.

    Both services operate on 2-byte (16-bit) Data Identifiers (DIDs) encoded
    as big-endian in the request frame.
    """

    @property
    def name(self) -> str:
        return "UDSonCAN"

    def handle(self, payload: bytes, store: DatapointStore) -> Optional[bytes]:
        """
        Dispatch an incoming UDS request to the appropriate service handler.

        Unknown service IDs return a ``serviceNotSupported`` negative response.
        """
        if not payload:
            logger.warning("Received empty UDS payload, ignoring")
            return None

        sid = payload[0]
        handler_fn = self._HANDLERS.get(sid)

        if handler_fn is None:
            logger.debug("Unsupported service 0x%02X", sid)
            return _negative_response(sid, NRC_SERVICE_NOT_SUPPORTED)

        return handler_fn(self, payload, store)

    # ------------------------------------------------------------------
    # Service implementations
    # ------------------------------------------------------------------

    def _handle_read_data_by_id(
        self, payload: bytes, store: DatapointStore
    ) -> bytes:
        """
        Handle UDS service 0x22 – ReadDataByIdentifier.

        Request:  [0x22] [DID_HIGH] [DID_LOW]
        Response: [0x62] [DID_HIGH] [DID_LOW] [DATA ...]
        """
        if len(payload) < 3:
            logger.debug("ReadDataByIdentifier: payload too short (%d bytes)", len(payload))
            return _negative_response(SID_READ_DATA_BY_ID, NRC_SUBFUNCTION_NOT_SUPPORTED)

        did = (payload[1] << 8) | payload[2]
        data = store.read(did)

        if data is None:
            logger.debug("ReadDataByIdentifier: DID 0x%04X not found", did)
            return _negative_response(SID_READ_DATA_BY_ID, NRC_REQUEST_OUT_OF_RANGE)

        logger.debug(
            "ReadDataByIdentifier: DID 0x%04X → %s", did, data.hex(" ")
        )
        return bytes([SID_READ_DATA_BY_ID + POSITIVE_OFFSET, payload[1], payload[2]]) + data

    def _handle_write_data_by_id(
        self, payload: bytes, store: DatapointStore
    ) -> bytes:
        """
        Handle UDS service 0x2E – WriteDataByIdentifier.

        Request:  [0x2E] [DID_HIGH] [DID_LOW] [DATA ...]
        Response: [0x6E] [DID_HIGH] [DID_LOW]
        """
        if len(payload) < 4:
            logger.debug("WriteDataByIdentifier: payload too short (%d bytes)", len(payload))
            return _negative_response(SID_WRITE_DATA_BY_ID, NRC_SUBFUNCTION_NOT_SUPPORTED)

        did = (payload[1] << 8) | payload[2]
        data = payload[3:]

        success = store.write(did, data)
        if not success:
            logger.debug("WriteDataByIdentifier: DID 0x%04X not found", did)
            return _negative_response(SID_WRITE_DATA_BY_ID, NRC_REQUEST_OUT_OF_RANGE)

        logger.debug(
            "WriteDataByIdentifier: DID 0x%04X ← %s", did, data.hex(" ")
        )
        return bytes([SID_WRITE_DATA_BY_ID + POSITIVE_OFFSET, payload[1], payload[2]])

    # ------------------------------------------------------------------
    # Dispatch table – maps SID → bound method
    # Extension point: add new services here without touching handle().
    # ------------------------------------------------------------------
    _HANDLERS: Dict[int, Callable[["UDSHandler", bytes, DatapointStore], bytes]] = {
        SID_READ_DATA_BY_ID:  _handle_read_data_by_id,
        SID_WRITE_DATA_BY_ID: _handle_write_data_by_id,
    }
