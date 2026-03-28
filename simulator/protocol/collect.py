"""
protocol/collect.py – Segmentation for the Viessmann E3 "collect" protocol.

This is the unsolicited broadcast protocol used by E3 devices to push
datapoint values to listening clients at a fixed schedule.  It is entirely
independent of ISO-TP and UDS.

Frame structure
---------------
Every frame is exactly 8 bytes.  The first byte (v0) is a sequence counter:

  * The **first frame** of every message always has v0 = 0x21.
  * Subsequent frames increment v0, wrapping in the range 0x20..0x2F:
      0x21 → 0x22 → … → 0x2F → 0x20 → 0x21 → …

First-frame layout (v0 = 0x21):
  v0      : 0x21  (fixed)
  v1, v2  : DID low-byte, DID high-byte  (little-endian)
  v3      : length code (see below)
  v4..v7  : start of payload (or extended length fields)

Length encoding (v3):
  0xB1..0xBF  payload length = v3 - 0xB0  (1..15 bytes); payload starts at v4
  0xB0        length ≥ 16 or == 0xC1:
                if v4 == 0xC1: payload length = v5; payload starts at v6
                else:          payload length = v4; payload starts at v5

Padding: the last frame is padded with 0x55 to 8 bytes.

Note on the sequence counter wrap
----------------------------------
The counter wraps modulo 16 in the range [0x20, 0x2F].  The first frame of
every *new message* is always 0x21 – not 0x20.  The value 0x20 only appears
as a continuation frame when the counter wraps around after 0x2F.

Reference examples (from docs/protocol.md)
-------------------------------------------
Single Frame, DID 0x09BE, payload length 4:
  21 BE 09 B4 95 0E 00 00

Multi Frame, DID 0x0224, payload length 24 (0x18):
  21 24 02 B0 18 55 00 00
  22 00 1A 03 00 00 5F 0A
  23 00 00 38 0F 00 00 9B
  24 32 00 00 57 5E 00 00

Multi Frame, DID 0x0509, payload length 181 (0xB5 = 0xC1 clash):
  21 09 05 B0 C1 B5 00 00   ← v4=C1 signals the extra length byte
  22 00 00 …
"""

from __future__ import annotations

import logging
from typing import List

logger = logging.getLogger(__name__)

FRAME_LEN   = 8
PADDING_BYTE = 0x55

# Sequence counter: first frame of each message is always 0x21;
# continuation frames follow in 0x20..0x2F (wrapping).
_SEQ_FIRST = 0x21
_SEQ_MIN   = 0x20
_SEQ_MAX   = 0x2F
_SEQ_RANGE = _SEQ_MAX - _SEQ_MIN + 1  # 16

# Length-code base added to short payload lengths (1..15)
_LEN_BASE        = 0xB0
_LEN_SHORT_MAX   = 15           # payload lengths that fit in v3
_LEN_ESCAPE      = 0xC1         # special value that clashes with length encoding
_LEN_ESCAPE_LIST = [0xB5, 0xC1] # list of special values that needs specific length encoding


def _pad(data: bytes) -> bytes:
    """Pad *data* to exactly FRAME_LEN bytes with PADDING_BYTE."""
    shortage = FRAME_LEN - len(data)
    if shortage < 0:
        raise AssertionError(f"Frame overflow: {len(data)} > {FRAME_LEN}")
    return data + bytes([PADDING_BYTE] * shortage)


def _next_seq(current: int) -> int:
    """Advance the sequence counter, wrapping 0x2F → 0x20."""
    nxt = current + 1
    if nxt > _SEQ_MAX:
        nxt = _SEQ_MIN
    return nxt


def segment_collect(did: int, payload: bytes) -> List[bytes]:
    """
    Segment *payload* for DID *did* into collect-protocol CAN frames.

    Parameters
    ----------
    did :
        16-bit data identifier (encoded little-endian in the first frame).
    payload :
        Raw value bytes for the datapoint.  Must not be empty.

    Returns
    -------
    list[bytes]
        Each element is exactly 8 bytes.  The first element has v0 = 0x21.

    Raises
    ------
    ValueError
        If *payload* is empty.
    """
    if not payload:
        raise ValueError("collect segment: empty payload is not supported")

    length = len(payload)
    did_lo = did & 0xFF
    did_hi = (did >> 8) & 0xFF

    # ------------------------------------------------------------------
    # Build the header bytes (v3 and optionally v4/v5) and the first
    # chunk of payload that fits into the first frame.
    # ------------------------------------------------------------------
    if length <= _LEN_SHORT_MAX:
        # v3 = B0+length, payload starts at v4 → 4 bytes remain in first frame
        header = bytes([_LEN_BASE + length])
        first_chunk_size = 4   # v4..v7
    else:
        # v3 = B0; extended length field
        if length in _LEN_ESCAPE_LIST:
            # Special case: length 0xC1 would be ambiguous, so add extra byte.
            # v3=B0, v4=C1, v5=length, payload starts at v6 → 2 bytes remain
            header = bytes([_LEN_BASE, _LEN_ESCAPE, length])
            first_chunk_size = 2   # v6..v7
        else:
            # v3=B0, v4=length, payload starts at v5 → 3 bytes remain
            header = bytes([_LEN_BASE, length])
            first_chunk_size = 3   # v5..v7

    # ------------------------------------------------------------------
    # First frame: 0x21 | DID_LO | DID_HI | header | first_chunk
    # ------------------------------------------------------------------
    prefix = bytes([_SEQ_FIRST, did_lo, did_hi]) + header
    # prefix is 3 + len(header) bytes; first_chunk fills up to FRAME_LEN
    first_chunk = payload[:first_chunk_size]
    frames: List[bytes] = [_pad(prefix + first_chunk)]

    # ------------------------------------------------------------------
    # Continuation frames: up to 7 payload bytes each
    # ------------------------------------------------------------------
    remaining = payload[first_chunk_size:]
    seq = _SEQ_FIRST
    while remaining:
        seq = _next_seq(seq)
        chunk = remaining[:7]
        remaining = remaining[7:]
        frames.append(_pad(bytes([seq]) + chunk))

    return frames
