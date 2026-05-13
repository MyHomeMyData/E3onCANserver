"""
protocol/service77.py – Viessmann proprietary "Service 77" write protocol.

Background
----------
Service 77 is a Viessmann-proprietary write protocol discovered via reverse
engineering.  It operates in parallel with UDS on a separate CAN-ID pair and
allows writing of data points that are protected against normal
WriteDataByIdentifier (UDS service 0x2E).

Viessmann uses this to protect certain data points from accidental or
unauthorised writes.  A client that receives NRC 0x22 (conditionsNotCorrect)
on a normal write can retry using Service 77 on the dedicated CAN-ID.

CAN-ID mapping
--------------
For a device whose UDS request address is ``tx_id``:

  Service 77 request  CAN-ID = tx_id + 0x02   (e.g. 0x682 for main device)
  Service 77 response CAN-ID = tx_id + 0x12   (= request + 0x10)

Frame format
------------
Request (reassembled ISO-TP payload, 9+ bytes):
    Byte 0:    0x77                   (Service ID)
    Bytes 1-2: [DID_HIGH] [DID_LOW]   (CTR field; e3oncan encodes the DID
                                       big-endian here instead of a counter)
    Bytes 3-5: 0x43 0x01 0x82         (fixed Client ID, ignored by server)
    Bytes 6-7: [DID_LOW] [DID_HIGH]   (DID little-endian – authoritative)
    Byte  8:   length code or data     (if high nibble >= 0x8: length code,
                                       data starts at byte 9; otherwise this
                                       byte is the first data byte itself)
    Bytes 9+:  DATA (only when byte 8 is a length code)

Positive response:
    [0x77] [DID_HIGH] [DID_LOW] [0x44]

    Bytes 1-2 echo payload[1:3].  Since e3oncan places DID_HIGH and DID_LOW
    in the CTR field, the response effectively echoes the DID big-endian.
    The server must not validate the CTR value.

Negative response (reuses the UDS encoding):
    [0x7F] [0x77] [NRC]

The confirmation byte 0x44 in the positive response is Viessmann-specific and
has no equivalent in the UDS standard.

Relationship to UDS WriteDataByIdentifier
-----------------------------------------
Both services write to the same DatapointStore.  The ``service77_dids`` set
passed to ``UDSHandler`` lists data points that Service 77 "owns":

  * A normal WriteDataByIdentifier (0x2E) on a protected DID returns NRC 0x22
    (conditionsNotCorrect) instead of writing the value.
  * Service77Handler accepts writes for **all** known DIDs, including the
    protected ones.
  * The additional 6 bytes prefix within Request is not known in detail
    and ignored for data storage.

This mirrors the real device behaviour: protection is enforced at the UDS
layer, not at the storage layer.

Fault injection
---------------
Service 77 responses are passed through the same FaultInjector as UDS
responses.  This is intentional: robustness tests should cover both paths.
"""

from __future__ import annotations

import logging
from typing import Optional

from simulator.datastore import DatapointStore
from simulator.protocol.base import ProtocolHandler
from simulator.protocol.uds import (
    NRC_REQUEST_OUT_OF_RANGE,
    NRC_SUBFUNCTION_NOT_SUPPORTED,
    SID_NEGATIVE_RESP,
    _negative_response,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service 77 constants
# ---------------------------------------------------------------------------

SID_SERVICE77          = 0x77
S77_CONFIRM_BYTE       = 0x44   # fixed last byte of every positive response
S77_REQUEST_ID_OFFSET  = 0x02   # request CAN-ID  = device tx_id + 0x02
S77_RESPONSE_ID_OFFSET = 0x12   # response CAN-ID = device tx_id + 0x12
                                # (= request + standard UDS offset 0x10)


# ---------------------------------------------------------------------------
# Service77Handler
# ---------------------------------------------------------------------------

class Service77Handler(ProtocolHandler):
    """
    Handles Viessmann Service 77 write requests.

    Accepts writes for all DIDs known to the DatapointStore, including those
    protected against normal UDS WriteDataByIdentifier.

    Parameters
    ----------
    (none – instantiated without arguments, same as UDSHandler)
    """

    @property
    def name(self) -> str:
        return "Service77"

    def handle(self, payload: bytes, store: DatapointStore) -> Optional[bytes]:
        """
        Process a Service 77 request payload.

        Parameters
        ----------
        payload :
            Fully reassembled bytes from the ISO-TP layer.
            Expected format: see module docstring (9+ bytes).

        Returns
        -------
        bytes
            Positive or negative response payload.
        None
            If payload is empty (frame silently ignored).
        """
        if not payload:
            logger.warning("[Service77] received empty payload, ignoring")
            return None

        if payload[0] != SID_SERVICE77:
            # Should not happen – the bus callback only fires for S77 frames.
            logger.warning(
                "[Service77] unexpected service ID 0x%02X (expected 0x77)",
                payload[0],
            )
            return _negative_response(payload[0], NRC_SUBFUNCTION_NOT_SUPPORTED)

        if len(payload) < 9:  # SID(1) + CTR(2) + ClientID(3) + DID(2) + LenCode(1)
            logger.debug("[Service77] payload too short (%d bytes)", len(payload))
            return _negative_response(SID_SERVICE77, NRC_SUBFUNCTION_NOT_SUPPORTED)

        did  = payload[6] | (payload[7] << 8)   # DID little-endian at bytes 6-7
        # Byte 8 is a length code only when its high nibble is >= 0x8 (0x8x or 0xBx).
        # Otherwise the byte is the first data byte (observed for small values whose
        # high nibble < 0x8, e.g. a 1-byte value of 0x2B).
        if (payload[8] & 0xF0) >= 0x80:
            data = payload[9:]  # length code present; data starts at byte 9
        else:
            data = payload[8:]  # no length code; byte 8 is first data byte

        success = store.write(did, data)
        if not success:
            logger.debug("[Service77] DID 0x%04X not found in store", did)
            return _negative_response(SID_SERVICE77, NRC_REQUEST_OUT_OF_RANGE)

        logger.debug(
            "[Service77] DID 0x%04X ← %s", did, data.hex(" ")
        )
        # Echo payload[1:3]: e3oncan stores DID_HIGH, DID_LOW in the CTR field,
        # so this is effectively [0x77, DID_HIGH, DID_LOW, 0x44].
        return bytes([SID_SERVICE77, payload[1], payload[2], S77_CONFIRM_BYTE])
