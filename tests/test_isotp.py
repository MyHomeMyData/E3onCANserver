"""Tests for simulator.protocol.isotp (ISO-TP segmentation and reassembly).

Key changes vs v0.1:
  - Sequence counter wraps 0–15 (not 1–15): seq 15 → next is 0, not 1.
  - Added test_seq_wrap_at_15 to explicitly cover the boundary.
  - Added test_roundtrip_very_long to exercise a message requiring > 15 CFs,
    which forces seq to wrap through 0.
"""

import pytest
from simulator.protocol.isotp import segment, ISOTPAssembler, CAN_DLC, PADDING_BYTE


# ------------------------------------------------------------------
# segment() – TX side
# ------------------------------------------------------------------

def test_single_frame_short():
    payload = bytes([0x22, 0x01, 0x00])
    frames = segment(payload)
    assert len(frames) == 1
    assert len(frames[0]) == CAN_DLC
    assert frames[0][0] == 0x03          # SF, length=3
    assert frames[0][1:4] == payload


def test_single_frame_max():
    payload = bytes(range(7))
    frames = segment(payload)
    assert len(frames) == 1
    assert frames[0][0] == 0x07


def test_multi_frame_ff_and_cfs():
    payload = bytes(range(14))           # 14 bytes → FF + 2 CFs
    frames = segment(payload)
    assert len(frames) == 3
    assert (frames[0][0] >> 4) == 0x1
    total_len = ((frames[0][0] & 0x0F) << 8) | frames[0][1]
    assert total_len == 14
    assert (frames[1][0] >> 4) == 0x2
    assert (frames[2][0] >> 4) == 0x2
    assert (frames[1][0] & 0x0F) == 1   # seq=1
    assert (frames[2][0] & 0x0F) == 2   # seq=2


def test_all_frames_padded():
    for length in (1, 7, 8, 14, 63):
        for frame in segment(bytes(length)):
            assert len(frame) == CAN_DLC


def test_padding_byte():
    payload = bytes([0xAB])             # 1-byte payload → 6 pad bytes
    frame = segment(payload)[0]
    assert frame[2:] == bytes([PADDING_BYTE] * 6)


def test_payload_too_large():
    with pytest.raises(ValueError):
        segment(bytes(4096))


def test_seq_wrap_at_15():
    """
    Sequence counter must wrap from 15 → 0 (not from 15 → 1).

    A payload of 6 + 15*7 + 1 = 112 bytes produces exactly 16 CFs.
    CF index 15 (0-based) carries seq=0 (after wrapping from 15).
    """
    # FF carries 6 bytes, each CF carries 7 bytes.
    # To get 16 CFs: 6 + 16*7 = 118 bytes total.
    payload = bytes(range(118))
    frames = segment(payload)
    # frames[0] = FF, frames[1..16] = CF seq 1..15 then 0
    assert len(frames) == 17
    seq_values = [(f[0] & 0x0F) for f in frames[1:]]
    expected = list(range(1, 16)) + [0]   # 1,2,…,15,0
    assert seq_values == expected


# ------------------------------------------------------------------
# ISOTPAssembler – RX side
# ------------------------------------------------------------------

@pytest.fixture
def asm():
    return ISOTPAssembler()


def test_single_frame_reassembly(asm):
    data = bytes([0x03, 0x22, 0x01, 0x00, 0xCC, 0xCC, 0xCC, 0xCC])
    payload, fc = asm.feed(data)
    assert payload == bytes([0x22, 0x01, 0x00])
    assert fc is None


def test_multi_frame_reassembly(asm):
    original = bytes(range(14))
    frames = segment(original)

    payload, fc = asm.feed(frames[0])
    assert payload is None
    assert fc is not None
    assert (fc[0] >> 4) == 0x3          # FC frame type

    payload, fc = asm.feed(frames[1])
    assert payload is None

    payload, fc = asm.feed(frames[2])
    assert payload == original


def test_roundtrip_long(asm):
    original = bytes(range(63))
    frames = segment(original)
    result = None
    for i, frame in enumerate(frames):
        payload, fc = asm.feed(frame)
        if i == 0:
            assert fc is not None
        if payload is not None:
            result = payload
    assert result == original


def test_roundtrip_very_long(asm):
    """
    Roundtrip for a 118-byte payload that requires seq to wrap 15 → 0.
    Verifies that both segmentation and reassembly handle the wrap correctly.
    """
    original = bytes(range(118))
    frames = segment(original)
    result = None
    for i, frame in enumerate(frames):
        payload, fc = asm.feed(frame)
        if i == 0:
            assert fc is not None        # FC after FF
        if payload is not None:
            result = payload
    assert result == original


def test_sf_invalid_length(asm):
    data = bytes([0x00, 0x22, 0x01, 0x00, 0xCC, 0xCC, 0xCC, 0xCC])
    payload, fc = asm.feed(data)
    assert payload is None


def test_cf_without_ff(asm):
    data = bytes([0x21, 0x00, 0x01, 0x02, 0xCC, 0xCC, 0xCC, 0xCC])
    payload, fc = asm.feed(data)
    assert payload is None
    assert fc is None
