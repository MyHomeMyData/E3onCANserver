"""Tests for simulator.protocol.isotp (ISO-TP segmentation and reassembly)."""

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
    # First frame
    assert (frames[0][0] >> 4) == 0x1
    total_len = ((frames[0][0] & 0x0F) << 8) | frames[0][1]
    assert total_len == 14
    # Consecutive frames
    assert (frames[1][0] >> 4) == 0x2
    assert (frames[2][0] >> 4) == 0x2
    assert (frames[1][0] & 0x0F) == 1   # seq=1
    assert (frames[2][0] & 0x0F) == 2   # seq=2


def test_all_frames_padded():
    for length in (1, 7, 8, 14, 63):
        for frame in segment(bytes(length)):
            assert len(frame) == CAN_DLC


def test_payload_too_large():
    with pytest.raises(ValueError):
        segment(bytes(4096))


def test_padding_byte():
    payload = bytes([0xAB])             # 1-byte payload → 6 pad bytes
    frame = segment(payload)[0]
    assert frame[2:] == bytes([PADDING_BYTE] * 6)


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
    # Build the same payload that segment() produces, feed it back
    original = bytes(range(14))
    frames = segment(original)

    # FF
    payload, fc = asm.feed(frames[0])
    assert payload is None
    assert fc is not None                # FC expected
    assert (fc[0] >> 4) == 0x3          # FC frame type

    # CF1
    payload, fc = asm.feed(frames[1])
    assert payload is None

    # CF2 – completes the message
    payload, fc = asm.feed(frames[2])
    assert payload == original


def test_roundtrip_long(asm):
    original = bytes(range(63))
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
    # length nibble = 0 → invalid
    data = bytes([0x00, 0x22, 0x01, 0x00, 0xCC, 0xCC, 0xCC, 0xCC])
    payload, fc = asm.feed(data)
    assert payload is None


def test_cf_without_ff(asm):
    # Consecutive frame without a preceding First Frame must be ignored
    data = bytes([0x21, 0x00, 0x01, 0x02, 0xCC, 0xCC, 0xCC, 0xCC])
    payload, fc = asm.feed(data)
    assert payload is None
    assert fc is None
