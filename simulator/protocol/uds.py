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
* 0x13  incorrectMessageLengthOrInvalidFormat (write with invalid data length)
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

Data length on WriteDataByIdentifier
------------------------------------
Real devices handle text datapoints (codec ``O3EUtf8``) in two ways:

* **Fixed length** (e.g. Vitocal): the value always has the same length,
  padded with ``00``.  A write is accepted only with exactly the currently
  stored length.
* **Variable length** (e.g. Vitodens): the value is the text plus one
  terminating ``00``, so its length changes when the text is changed.
  DIDs listed in ``variable_length_dids`` (mapping DID → max_len) accept any
  length from 1 up to max_len; the value is stored with exactly the written
  length and subsequent reads return that length.

Any other length is rejected with NRC 0x13.  ASSUMPTION: it is not verified
that real Viessmann devices answer an invalid write length with NRC 0x13;
it is the natural choice per ISO 14229 and is used by the simulator.

The checks are applied in this order: payload too short (0x12), Service 77
protection (0x22), unknown DID (0x31), data length (0x13).

Extension notes
---------------
* To add a service: add a new ``_handle_<n>`` method and register it in
  ``_HANDLERS`` at the bottom of this file.
"""

from __future__ import annotations

import logging
from typing import Callable, Dict, FrozenSet, Mapping, Optional

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
NRC_INCORRECT_MESSAGE_LENGTH  = 0x13   # incorrectMessageLengthOrInvalidFormat
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
    variable_length_dids :
        Mapping DID → max_len for DIDs whose value length may change on
        write (see module docstring).  All other DIDs are treated as fixed
        length.  Pass an empty mapping (the default) for fixed length only.
    """

    def __init__(
        self,
        service77_dids: Optional[FrozenSet[int]] = None,
        variable_length_dids: Optional[Mapping[int, int]] = None,
    ) -> None:
        self._protected: FrozenSet[int] = service77_dids or frozenset()
        self._variable_len: Dict[int, int] = dict(variable_length_dids or {})

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
        attempted.  The data length is then validated (see module docstring)
        and an invalid length is rejected with NRC 0x13 without modifying
        the stored value.
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

        stored_len = store.stored_length(did)
        if stored_len is None:
            logger.debug("WriteDataByIdentifier: DID 0x%04X not found", did)
            return _negative_response(SID_WRITE_DATA_BY_ID, NRC_REQUEST_OUT_OF_RANGE)

        # Length check.  ASSUMPTION: NRC 0x13 for an invalid length is not
        # confirmed on real Viessmann devices (see module docstring).
        if did in self._variable_len:
            max_len = self._variable_len[did]
            if not 1 <= len(data) <= max_len:
                logger.debug(
                    "WriteDataByIdentifier: DID 0x%04X (variable length) got %d"
                    " bytes, allowed 1..%d → NRC 0x13", did, len(data), max_len,
                )
                return _negative_response(SID_WRITE_DATA_BY_ID, NRC_INCORRECT_MESSAGE_LENGTH)
        elif len(data) != stored_len:
            logger.debug(
                "WriteDataByIdentifier: DID 0x%04X (fixed length) got %d bytes,"
                " expected %d → NRC 0x13", did, len(data), stored_len,
            )
            return _negative_response(SID_WRITE_DATA_BY_ID, NRC_INCORRECT_MESSAGE_LENGTH)

        store.write(did, data)

        logger.debug("WriteDataByIdentifier: DID 0x%04X ← %s", did, data.hex(" "))
        return bytes([SID_WRITE_DATA_BY_ID + POSITIVE_OFFSET, payload[1], payload[2]])

    # ------------------------------------------------------------------
    # Dispatch table
    # ------------------------------------------------------------------
    _HANDLERS: Dict[int, Callable[["UDSHandler", bytes, DatapointStore], bytes]] = {
        SID_READ_DATA_BY_ID:  _handle_read_data_by_id,
        SID_WRITE_DATA_BY_ID: _handle_write_data_by_id,
    }
