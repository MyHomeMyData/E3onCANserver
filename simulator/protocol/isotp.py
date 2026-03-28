"""
protocol/isotp.py – ISO 15765-2 (ISO-TP) transport layer implementation.

This module handles segmentation and reassembly of UDS payloads over CAN
frames of 8 bytes each.  It is intentionally separated from the UDS service
logic so that other protocols can reuse the same transport layer.

Frame types (first nibble of first data byte)
---------------------------------------------
0x0  Single Frame   (SF)  – payload ≤ 7 bytes
0x1  First Frame    (FF)  – first segment of a multi-frame message
0x2  Consecutive Frame (CF) – subsequent segments
0x3  Flow Control   (FC)  – sent by the receiver to authorise transmission

Single Frame layout (8 bytes total)
------------------------------------
Byte 0:  0x0n  (n = payload length, 1–7)
Bytes 1–n: payload

First Frame layout
------------------
Byte 0:  0x1H  (H = high nibble of total length)
Byte 1:  0xLL  (low byte of total length, max 4095)
Bytes 2–7: first 6 bytes of payload

Flow Control layout (sent by *receiver* after FF)
-------------------------------------------------
Byte 0:  0x30  (FC, flow status = ContinueToSend)
Byte 1:  0x00  (block size = 0, send all without further FC)
Byte 2:  0x00  (separation time = 0 ms)
Bytes 3–7: 0x00 (padding)

Consecutive Frame layout
------------------------
Byte 0:  0x2n  (n = sequence number 1–15, then wraps to 0)
Bytes 1–7: up to 7 bytes of payload

Limitations / known omissions in v0.1
--------------------------------------
* Only ContinueToSend (FC.FS = 0) is implemented; WaitFrame and Overflow are
  not handled.
* Block size > 0 (receiver requests FC after every N CFs) is not implemented.
* Separation time from FC is not enforced (CFs are sent back-to-back).
* Maximum payload is 4095 bytes (first-frame length field is 12-bit).
* Padding to 8 bytes uses 0xCC (common in automotive tools).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# CAN data bytes are always padded to this length
CAN_DLC = 8
PADDING_BYTE = 0xCC

# ISO-TP frame type identifiers (high nibble)
_SF = 0x0
_FF = 0x1
_CF = 0x2
_FC = 0x3


def _pad(data: bytes) -> bytes:
    """Pad *data* with PADDING_BYTE to exactly CAN_DLC bytes."""
    return data + bytes([PADDING_BYTE] * (CAN_DLC - len(data)))


# ---------------------------------------------------------------------------
# Segmentation (TX side) – split a payload into CAN frame data fields
# ---------------------------------------------------------------------------

def segment(payload: bytes) -> list[bytes]:
    """
    Segment *payload* into a list of 8-byte CAN data fields.

    For payloads up to 7 bytes a single Single Frame is returned.
    Longer payloads are split into a First Frame followed by Consecutive
    Frames.  The caller is responsible for sending them in order, inserting a
    wait for the Flow Control frame between the FF and the first CF.

    Parameters
    ----------
    payload :
        The complete UDS response bytes to be transmitted.

    Returns
    -------
    list[bytes]
        Each element is exactly 8 bytes (padded with PADDING_BYTE).
        Index 0 is always the SF or FF.  Indices 1+ are CFs.
        The FC slot is *not* included – the caller must handle FC reception
        before sending the CFs.
    """
    length = len(payload)

    # --- Single Frame ---
    if length <= 7:
        frame = bytes([length]) + payload
        return [_pad(frame)]

    # --- Multi Frame ---
    if length > 0xFFF:
        raise ValueError(f"ISO-TP payload too large: {length} bytes (max 4095)")

    frames: list[bytes] = []

    # First Frame
    high = (length >> 8) & 0x0F
    low  = length & 0xFF
    ff_payload = payload[:6]
    frames.append(_pad(bytes([0x10 | high, low]) + ff_payload))

    # Consecutive Frames
    remaining = payload[6:]
    seq = 1
    while remaining:
        chunk = remaining[:7]
        remaining = remaining[7:]
        frames.append(_pad(bytes([0x20 | (seq & 0x0F)]) + chunk))
        seq = ((seq + 1) % 16) # wraps 0–15

    return frames


# ---------------------------------------------------------------------------
# Reassembly (RX side) – stateful per-device reassembler
# ---------------------------------------------------------------------------

class ISOTPAssembler:
    """
    Stateful ISO-TP reassembler for one (sender_id, receiver_id) pair.

    Instances are created per simulated device and held for the lifetime of
    the device task.  Call ``feed()`` with every incoming CAN frame data field;
    it returns the complete payload when reassembly is done, or None otherwise.

    The assembler also produces the Flow Control frame that must be sent back
    to the client after receiving a First Frame.
    """

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._expected_length: int = 0
        self._buffer: bytearray = bytearray()
        self._next_seq: int = 1
        self._active: bool = False

    def feed(self, data: bytes) -> tuple[Optional[bytes], Optional[bytes]]:
        """
        Process one incoming CAN frame's data field.

        Parameters
        ----------
        data :
            Exactly 8 bytes from the CAN frame (padding included).

        Returns
        -------
        (payload, fc_frame)
            payload  – complete reassembled bytes if this frame completed the
                       message; None otherwise.
            fc_frame – 8-byte Flow Control frame to send back immediately if
                       this was a First Frame; None otherwise.
        """
        if not data:
            return None, None

        frame_type = (data[0] >> 4) & 0x0F

        if frame_type == _SF:
            return self._handle_sf(data)
        elif frame_type == _FF:
            return self._handle_ff(data)
        elif frame_type == _CF:
            return self._handle_cf(data)
        else:
            logger.debug("ISO-TP: unexpected frame type 0x%X, ignoring", frame_type)
            return None, None

    # ------------------------------------------------------------------

    def _handle_sf(self, data: bytes) -> tuple[Optional[bytes], Optional[bytes]]:
        length = data[0] & 0x0F
        if length == 0 or length > 7:
            logger.debug("ISO-TP SF: invalid length %d", length)
            return None, None
        self._reset()
        payload = bytes(data[1:1 + length])
        logger.debug("ISO-TP SF: %d bytes → %s", length, payload.hex(" "))
        return payload, None

    def _handle_ff(self, data: bytes) -> tuple[Optional[bytes], Optional[bytes]]:
        length = ((data[0] & 0x0F) << 8) | data[1]
        if length < 8:
            logger.debug("ISO-TP FF: implausible length %d", length)
            return None, None
        self._reset()
        self._expected_length = length
        self._buffer.extend(data[2:8])  # first 6 bytes
        self._next_seq = 1
        self._active = True
        logger.debug("ISO-TP FF: expecting %d bytes total, got 6 so far", length)
        fc = _pad(bytes([0x30, 0x00, 0x00]))  # ContinueToSend, BS=0, STmin=0
        return None, bytes(fc)

    def _handle_cf(self, data: bytes) -> tuple[Optional[bytes], Optional[bytes]]:
        if not self._active:
            logger.debug("ISO-TP CF received without active FF, ignoring")
            return None, None
        seq = data[0] & 0x0F
        if seq != self._next_seq:
            logger.warning(
                "ISO-TP CF: sequence mismatch (expected %d, got %d), aborting",
                self._next_seq, seq,
            )
            self._reset()
            return None, None

        remaining = self._expected_length - len(self._buffer)
        chunk = data[1:1 + min(7, remaining)]
        self._buffer.extend(chunk)
        self._next_seq = (self._next_seq + 1) % 16  # wraps 0–15

        if len(self._buffer) >= self._expected_length:
            payload = bytes(self._buffer[:self._expected_length])
            logger.debug(
                "ISO-TP reassembly complete: %d bytes → %s",
                len(payload), payload.hex(" "),
            )
            self._reset()
            return payload, None

        return None, None
