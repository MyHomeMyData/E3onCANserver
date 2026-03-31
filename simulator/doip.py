"""
doip.py – DoIP (Diagnostics over IP, ISO 13400) server.

Purpose
-------
Allows open3e (and any other UDS-over-DoIP client) to communicate with the
simulator over TCP instead of CAN.  The implementation covers exactly the
subset of DoIP that ``doipclient`` (used by open3e) requires:

  1. Routing Activation handshake
  2. Diagnostic Message (UDS request → UDS response)

Everything else (Vehicle Announcement, Entity Status, etc.) is out of scope
for a simulator targeting open3e.

Protocol overview (ISO 13400)
------------------------------
Every DoIP frame starts with an 8-byte generic header:

  Byte 0   : Protocol version (0x02)
  Byte 1   : Inverse protocol version (~0x02 = 0xFD, used as sync check)
  Byte 2-3 : Payload type (big-endian uint16)
  Byte 4-7 : Payload length (big-endian uint32, bytes after the header)

Payload types used here:

  0x0005  Routing Activation Request   (client → server)
  0x0006  Routing Activation Response  (server → client)
  0x8001  Diagnostic Message           (both directions)
  0x8002  Diagnostic Message Positive ACK (server → client)

Diagnostic Message layout (payload, after the 8-byte header):

  Byte 0-1 : Source address  (logical address of sender, big-endian)
  Byte 2-3 : Target address  (logical address of target ECU, big-endian)
  Byte 4+  : UDS payload

The target address in the client's request maps directly to the device's
``tx_id`` (e.g. open3e passes ``--ecutx 0x680``, which becomes target address
0x0680).  The server responds with source/target swapped.

Routing Activation
------------------
``doipclient`` sends a Routing Activation Request immediately after the TCP
connection is established.  We respond with code 0x10 (Successfully activated)
regardless of the content – no authentication is implemented.

After successful activation the client may send Diagnostic Messages.

Limitations (by design – sufficient for open3e testing)
--------------------------------------------------------
* One TCP connection at a time per server instance.  A new connection silently
  replaces an existing one.
* No Vehicle Announcement broadcasts (UDP).
* No TLS.
* No Routing Activation authentication.
* Fault injection (delay, error rate) applies to DoIP responses the same way
  it does to CAN responses, because both go through FaultInjector.send_frames.
  The injector works on the UDS payload level; DoIP framing is added after.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DoIP constants
# ---------------------------------------------------------------------------

DOIP_VERSION        = 0x02
DOIP_VERSION_INV    = 0xFD   # ~0x02
DOIP_HEADER_LEN     = 8
DOIP_DIAG_ADDR_LEN  = 4      # source (2) + target (2) before UDS payload

# Payload types
PT_ROUTING_ACTIVATION_REQ  = 0x0005
PT_ROUTING_ACTIVATION_RESP = 0x0006
PT_DIAGNOSTIC_MSG          = 0x8001
PT_DIAGNOSTIC_ACK          = 0x8002

# Routing Activation response codes
RA_ACTIVATED    = 0x10   # Successfully activated
RA_DENIED       = 0x00   # Denied – not used here, but documented

# Diagnostic ACK/NACK codes
DIAG_ACK        = 0x00   # Message accepted
DIAG_NACK_UNKNOWN_TARGET = 0x03

DEFAULT_HOST    = "127.0.0.1"
DEFAULT_PORT    = 13400

# Logical address used by the server as source in responses.
# 0x0001 is a common DoIP gateway address.
SERVER_LOGICAL_ADDR = 0x0001


# ---------------------------------------------------------------------------
# Frame builders
# ---------------------------------------------------------------------------

def _header(payload_type: int, payload_len: int) -> bytes:
    """Build an 8-byte DoIP generic header."""
    return struct.pack(
        "!BBHI",
        DOIP_VERSION,
        DOIP_VERSION_INV,
        payload_type,
        payload_len,
    )


def _routing_activation_response(client_addr: int) -> bytes:
    """
    Build a Routing Activation Response (0x0006).

    Payload:
      Byte 0-1 : Client logical address (echoed back)
      Byte 2-3 : Server logical address
      Byte 4   : Response code (0x10 = activated)
      Byte 5-8 : Reserved (0x00000000)
    """
    payload = struct.pack("!HHBI", client_addr, SERVER_LOGICAL_ADDR, RA_ACTIVATED, 0)
    return _header(PT_ROUTING_ACTIVATION_RESP, len(payload)) + payload


def _diagnostic_ack(source_addr: int, target_addr: int, ack_code: int = DIAG_ACK) -> bytes:
    """
    Build a Diagnostic Message Positive ACK (0x8002).

    Payload:
      Byte 0-1 : Source address (echoed target from request)
      Byte 2-3 : Target address (echoed source from request)
      Byte 4   : ACK/NACK code
    """
    payload = struct.pack("!HHB", source_addr, target_addr, ack_code)
    return _header(PT_DIAGNOSTIC_ACK, len(payload)) + payload


def _diagnostic_message(source_addr: int, target_addr: int, uds_payload: bytes) -> bytes:
    """
    Build a Diagnostic Message (0x8001) carrying a UDS response.

    Payload:
      Byte 0-1 : Source address
      Byte 2-3 : Target address
      Byte 4+  : UDS payload
    """
    addr_bytes = struct.pack("!HH", source_addr, target_addr)
    payload    = addr_bytes + uds_payload
    return _header(PT_DIAGNOSTIC_MSG, len(payload)) + payload


# ---------------------------------------------------------------------------
# DoIPServer
# ---------------------------------------------------------------------------

class DoIPServer:
    """
    Minimal DoIP server that dispatches UDS requests to SimulatedDevice instances.

    Parameters
    ----------
    devices :
        Mapping of ``tx_id → SimulatedDevice``.  The ``tx_id`` is used as
        the logical ECU address (e.g. 0x680 for the main device).
    host :
        TCP bind address (default ``"127.0.0.1"``).
    port :
        TCP port (default 13400, the DoIP standard port).
    """

    def __init__(
        self,
        devices: Dict[int, object],   # tx_id → SimulatedDevice
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._devices = devices
        self._host    = host
        self._port    = port
        self._server: Optional[asyncio.AbstractServer] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the TCP server."""
        self._server = await asyncio.start_server(
            self._handle_connection,
            self._host,
            self._port,
        )
        logger.info(
            "DoIP server listening on %s:%d (%d device(s): %s)",
            self._host, self._port,
            len(self._devices),
            ", ".join(f"0x{a:03X}" for a in sorted(self._devices)),
        )

    async def stop(self) -> None:
        """Stop the TCP server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            logger.info("DoIP server stopped")

    # ------------------------------------------------------------------
    # Connection handler
    # ------------------------------------------------------------------

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        logger.info("DoIP: new connection from %s", peer)
        try:
            await self._session(reader, writer)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            logger.info("DoIP: connection from %s closed", peer)
        except Exception as exc:
            logger.error("DoIP: error in session from %s: %s", peer, exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _session(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """
        Process one DoIP session: read frames until connection closes.

        A session consists of:
          1. One Routing Activation Request → Response
          2. Any number of Diagnostic Message exchanges
        """
        activated = False

        while True:
            # Read the fixed 8-byte header first.
            header = await reader.readexactly(DOIP_HEADER_LEN)
            version, version_inv, payload_type, payload_len = struct.unpack(
                "!BBHI", header
            )

            # Basic sync check.
            if version != DOIP_VERSION or version_inv != DOIP_VERSION_INV:
                logger.warning(
                    "DoIP: invalid header sync (0x%02X/0x%02X), dropping frame",
                    version, version_inv,
                )
                if payload_len:
                    await reader.readexactly(payload_len)   # drain
                continue

            payload = await reader.readexactly(payload_len) if payload_len else b""

            if payload_type == PT_ROUTING_ACTIVATION_REQ:
                activated = True
                # Extract client logical address from first 2 bytes of payload.
                client_addr = struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else 0x0E00
                logger.debug("DoIP: Routing Activation from client 0x%04X", client_addr)
                writer.write(_routing_activation_response(client_addr))
                await writer.drain()

            elif payload_type == PT_DIAGNOSTIC_MSG:
                if not activated:
                    logger.warning("DoIP: Diagnostic Message before Routing Activation, ignoring")
                    continue

                if len(payload) < DOIP_DIAG_ADDR_LEN + 1:
                    logger.warning("DoIP: Diagnostic Message too short (%d bytes)", len(payload))
                    continue

                source_addr = struct.unpack("!H", payload[0:2])[0]
                target_addr = struct.unpack("!H", payload[2:4])[0]
                uds_payload = payload[4:]

                logger.debug(
                    "DoIP: Diagnostic 0x%04X → 0x%04X  UDS: %s",
                    source_addr, target_addr, uds_payload.hex(" "),
                )

                device = self._devices.get(target_addr)
                if device is None:
                    logger.warning(
                        "DoIP: unknown target address 0x%04X", target_addr
                    )
                    writer.write(
                        _diagnostic_ack(target_addr, source_addr, DIAG_NACK_UNKNOWN_TARGET)
                    )
                    await writer.drain()
                    continue

                # Send ACK first (required by doipclient before it reads the response).
                writer.write(_diagnostic_ack(target_addr, source_addr))
                await writer.drain()

                # Dispatch UDS payload and get response.
                uds_response = await device.handle_uds_payload(uds_payload)

                if uds_response is not None:
                    frame = _diagnostic_message(target_addr, source_addr, uds_response)
                    writer.write(frame)
                    await writer.drain()
                    logger.debug(
                        "DoIP: response 0x%04X → 0x%04X  UDS: %s",
                        target_addr, source_addr, uds_response.hex(" "),
                    )

            else:
                logger.debug(
                    "DoIP: unhandled payload type 0x%04X (%d bytes), ignoring",
                    payload_type, payload_len,
                )
