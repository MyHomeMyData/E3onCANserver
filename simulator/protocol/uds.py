"""
protocol/uds.py – UDS (ISO 14229) protocol handler.

Implemented services
--------------------
* 0x22  ReadDataByIdentifier  (single DID per request)
* 0x2E  WriteDataByIdentifier (with optional Service 77 protection list)

Negative response codes used
-----------------------------
* 0x11  serviceNotSupported
* 0x12  subFunctionNotSupported (used for malformed requests)
* 0x22  conditionsNotCorrect    (DID is protected; use Service 77 instead)
* 0x31  requestOutOfRange       (DID not present in store)
* 0x33  securityAccessDenied   (placeholder, not enforced in v0.1)

Service 77 protection
---------------------
An optional set of DID integers (``service77_dids``) can be passed at
construction time.  Any WriteDataByIdentifier request targeting a DID in this
set is rejected with NRC 0x22 (conditionsNotCorrect), mirroring the behaviour
of real Viessmann devices that protect those data points from normal writes.
Clients that receive 0x22 may retry via the Service 77 handler on the
dedicated CAN-ID pair (tx_id + 0x02 / tx_id + 0x12).

Extension notes
---------------
* To add a service: add a new ``_handle_<n>`` method and register it in
  ``_HANDLERS`` at the bottom of this file.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, FrozenSet, Optional

from simulator.datastore import DatapointStore
from simulator.protocol.base import ProtocolHandler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# UDS constants
# ---------------------------------------------------------------------------

SID_READ_DATA_BY_ID  = 0x22
SID_WRITE_DATA_BY_ID = 0x2E
SID_NEGATIVE_RESP    = 0x7F
POSITIVE_OFFSET      = 0x40

NRC_SERVICE_NOT_SUPPORTED     = 0x11
NRC_SUBFUNCTION_NOT_SUPPORTED = 0x12
NRC_CONDITIONS_NOT_CORRECT    = 0x22   # DID protected – use Service 77
NRC_REQUEST_OUT_OF_RANGE      = 0x31
NRC_SECURITY_ACCESS_DENIED    = 0x33


def _negative_response(service_id: int, nrc: int) -> bytes:
    """Build a UDS negative response frame."""
    return bytes([SID_NEGATIVE_RESP, service_id, nrc])


class UDSHandler(ProtocolHandler):
    """
    Minimal UDS handler covering ReadDataByIdentifier and WriteDataByIdentifier.

    Parameters
    ----------
    service77_dids :
        Set of DID integers protected against normal WriteDataByIdentifier.
        A write request targeting any of these DIDs returns NRC 0x22
        (conditionsNotCorrect).  Pass an empty set (the default) for no
        protection.
    """

    def __init__(self, service77_dids: Optional[FrozenSet[int]] = None) -> None:
        self._protected: FrozenSet[int] = service77_dids or frozenset()

    @property
    def name(self) -> str:
        return "UDSonCAN"

    def handle(self, payload: bytes, store: DatapointStore) -> Optional[bytes]:
        """Dispatch an incoming UDS request to the appropriate service handler."""
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

        did  = (payload[1] << 8) | payload[2]
        data = store.read(did)

        if data is None:
            logger.debug("ReadDataByIdentifier: DID 0x%04X not found", did)
            return _negative_response(SID_READ_DATA_BY_ID, NRC_REQUEST_OUT_OF_RANGE)

        logger.debug("ReadDataByIdentifier: DID 0x%04X → %s", did, data.hex(" "))
        return bytes([SID_READ_DATA_BY_ID + POSITIVE_OFFSET, payload[1], payload[2]]) + data

    def _handle_write_data_by_id(
        self, payload: bytes, store: DatapointStore
    ) -> bytes:
        """
        Handle UDS service 0x2E – WriteDataByIdentifier.

        Request:  [0x2E] [DID_HIGH] [DID_LOW] [DATA ...]
        Response: [0x6E] [DID_HIGH] [DID_LOW]

        If the DID is in the Service 77 protection list, the request is
        rejected with NRC 0x22 (conditionsNotCorrect) before any write is
        attempted.
        """
        if len(payload) < 4:
            logger.debug("WriteDataByIdentifier: payload too short (%d bytes)", len(payload))
            return _negative_response(SID_WRITE_DATA_BY_ID, NRC_SUBFUNCTION_NOT_SUPPORTED)

        did  = (payload[1] << 8) | payload[2]
        data = payload[3:]

        # Service 77 protection: reject writes to guarded DIDs.
        if did in self._protected:
            logger.debug(
                "WriteDataByIdentifier: DID 0x%04X is Service-77-protected"
                " → NRC 0x22", did,
            )
            return _negative_response(SID_WRITE_DATA_BY_ID, NRC_CONDITIONS_NOT_CORRECT)

        success = store.write(did, data)
        if not success:
            logger.debug("WriteDataByIdentifier: DID 0x%04X not found", did)
            return _negative_response(SID_WRITE_DATA_BY_ID, NRC_REQUEST_OUT_OF_RANGE)

        logger.debug("WriteDataByIdentifier: DID 0x%04X ← %s", did, data.hex(" "))
        return bytes([SID_WRITE_DATA_BY_ID + POSITIVE_OFFSET, payload[1], payload[2]])

    # ------------------------------------------------------------------
    # Dispatch table
    # ------------------------------------------------------------------
    _HANDLERS: Dict[int, Callable[["UDSHandler", bytes, DatapointStore], bytes]] = {
        SID_READ_DATA_BY_ID:  _handle_read_data_by_id,
        SID_WRITE_DATA_BY_ID: _handle_write_data_by_id,
    }
