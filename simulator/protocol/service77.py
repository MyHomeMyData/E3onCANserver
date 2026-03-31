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
Request:
    [0x77] [DID_HIGH] [DID_LOW] [6 BYTES PREFIX] [DATA ...]

Positive response:
    [0x77] [0x04] [DID_HIGH] [DID_LOW]

Negative response (reuses the UDS encoding):
    [0x7F] [0x77] [NRC]

The confirmation byte 0x04 in the positive response is Viessmann-specific and
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
            Expected format: [0x77] [DID_HIGH] [DID_LOW] [DATA ...]

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

        if len(payload) < 4:
            logger.debug("[Service77] payload too short (%d bytes)", len(payload))
            return _negative_response(SID_SERVICE77, NRC_SUBFUNCTION_NOT_SUPPORTED)

        did  = (payload[1] << 8) | payload[2]
        data = payload[9:]  # Service 77 has 6 bytes additional prefix - ignored

        success = store.write(did, data)
        if not success:
            logger.debug("[Service77] DID 0x%04X not found in store", did)
            return _negative_response(SID_SERVICE77, NRC_REQUEST_OUT_OF_RANGE)

        logger.debug(
            "[Service77] DID 0x%04X ← %s", did, data.hex(" ")
        )
        return bytes([SID_SERVICE77, payload[1], payload[2], S77_CONFIRM_BYTE])
