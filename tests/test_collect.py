"""
tests/test_collect.py – Tests for the Viessmann collect protocol.

Covers:
  - segment_collect(): frame structure, length encoding, padding, seq wrap
  - Reference examples from docs/protocol.md
  - RawEncoder and LocaltimeEncoder
"""

import time
import pytest
from pathlib import Path

from simulator.protocol.collect import (
    segment_collect, FRAME_LEN, PADDING_BYTE,
    _LEN_BASE, _LEN_ESCAPE, _SEQ_FIRST, _SEQ_MIN, _SEQ_MAX,
)
from simulator.protocol.encoders import RawEncoder, LocaltimeEncoder, Encoder
from simulator.datastore import DatapointStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(content: str) -> DatapointStore:
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(content)
        fname = f.name
    return DatapointStore.from_file(fname)


def v0(frame: bytes) -> int:
    return frame[0]

def did_from_frame(frame: bytes) -> int:
    """Decode DID from the first frame (little-endian v1, v2)."""
    return frame[1] | (frame[2] << 8)


# ---------------------------------------------------------------------------
# segment_collect – basic structure
# ---------------------------------------------------------------------------

class TestSegmentCollect:

    def test_all_frames_8_bytes(self):
        for length in (1, 4, 7, 8, 15, 16, 24, 100, 181):
            frames = segment_collect(0x0100, bytes(length))
            for f in frames:
                assert len(f) == FRAME_LEN, f"frame length {len(f)} for payload {length}"

    def test_first_frame_always_0x21(self):
        for length in (1, 9, 24, 181):
            frames = segment_collect(0x0100, bytes(length))
            assert v0(frames[0]) == _SEQ_FIRST

    def test_did_encoded_little_endian(self):
        did = 0x09BE
        frames = segment_collect(did, bytes(4))
        assert did_from_frame(frames[0]) == did

    def test_empty_payload_raises(self):
        with pytest.raises(ValueError):
            segment_collect(0x0100, b"")

    def test_last_frame_padded_with_0x55(self):
        # Single frame, payload length 3 → 1 padding bytes at v7
        frames = segment_collect(0x09BE, bytes(3))
        assert frames[-1][-1] == PADDING_BYTE

    def test_seq_counter_increments(self):
        # Payload large enough to need several continuation frames
        frames = segment_collect(0x0100, bytes(30))
        assert v0(frames[0]) == 0x21
        assert v0(frames[1]) == 0x22
        assert v0(frames[2]) == 0x23

    def test_seq_wraps_2f_to_20(self):
        # First frame = 0x21, each CF carries 7 bytes of a 6+7*n payload.
        # After 14 CFs: seq goes 21,22,...,2F,20,21,...
        # Build a payload requiring exactly 15 CFs: 6 + 15*7 = 111 bytes.
        # But first frame only holds variable bytes depending on header size.
        # For short payload (len<=15): header=1 byte, first_chunk=4 bytes.
        # For long payload (len>15, !=0xC1): header=2 bytes, first_chunk=3 bytes.
        # 111 bytes, len>15 → first_chunk=3, then 15 CFs of 7 = 105+3=108+3=111 ✓
        payload = bytes(range(111))
        frames = segment_collect(0x0100, payload)
        # frames[0]=FF(0x21), frames[1..15]=CFs
        seqs = [v0(f) for f in frames]
        assert seqs[0] == 0x21
        # Check the wrap: after 0x2F comes 0x20
        assert 0x2F in seqs
        idx_2f = seqs.index(0x2F)
        assert seqs[idx_2f + 1] == 0x20

    # -----------------------------------------------------------------------
    # Reference examples from docs/protocol.md
    # -----------------------------------------------------------------------

    def test_reference_single_frame_0x09BE_len4(self):
        """
        Single Frame, DID 0x09BE, length 4:
          21 BE 09 B4 95 0E 00 00
        """
        payload = bytes([0x95, 0x0E, 0x00, 0x00])
        frames = segment_collect(0x09BE, payload)
        assert len(frames) == 1
        f = frames[0]
        assert f[0] == 0x21
        assert f[1] == 0xBE and f[2] == 0x09        # DID little-endian
        assert f[3] == _LEN_BASE + 4                 # 0xB4
        assert f[4:8] == bytes([0x95, 0x0E, 0x00, 0x00])

    def test_reference_multi_frame_0x011A_len9(self):
        """
        Multi Frame, DID 0x011A, length 9:
          21 1A 01 B9 90 01 D4 00
          22 E5 01 82 01 00 55 55
        """
        payload = bytes([0x90, 0x01, 0xD4, 0x00, 0xE5, 0x01, 0x82, 0x01, 0x00])
        frames = segment_collect(0x011A, payload)
        assert len(frames) == 2
        assert frames[0][0] == 0x21
        assert frames[0][1] == 0x1A and frames[0][2] == 0x01
        assert frames[0][3] == _LEN_BASE + 9         # 0xB9
        assert frames[0][4:8] == bytes([0x90, 0x01, 0xD4, 0x00])
        assert frames[1][0] == 0x22
        assert frames[1][1:6] == bytes([0xE5, 0x01, 0x82, 0x01, 0x00])

    def test_reference_multi_frame_0x0224_len24(self):
        """
        Multi Frame, DID 0x0224, length 24 (0x18):
          21 24 02 B0 18 55 00 00
          22 00 1A 03 00 00 5F 0A
          23 00 00 38 0F 00 00 9B
          24 32 00 00 57 5E 00 00
        """
        payload = bytes([
            0x55, 0x00, 0x00,               # first frame (v5,v6,v7)
            0x00, 0x1A, 0x03, 0x00, 0x00, 0x5F, 0x0A,   # CF1
            0x00, 0x00, 0x38, 0x0F, 0x00, 0x00, 0x9B,   # CF2
            0x32, 0x00, 0x00, 0x57, 0x5E, 0x00, 0x00,   # CF3
        ])
        assert len(payload) == 24
        frames = segment_collect(0x0224, payload)
        assert len(frames) == 4
        f0 = frames[0]
        assert f0[0] == 0x21
        assert f0[1] == 0x24 and f0[2] == 0x02
        assert f0[3] == _LEN_BASE             # 0xB0 – extended length
        assert f0[4] == 24                    # length = 0x18, not 0xC1
        assert f0[5:8] == bytes([0x55, 0x00, 0x00])
        assert frames[1][0] == 0x22
        assert frames[2][0] == 0x23
        assert frames[3][0] == 0x24

    def test_reference_multi_frame_0x0509_len181(self):
        """
        Multi Frame, DID 0x0509, length 181 (0xB5 = 0xC1 special case):
          21 09 05 B0 C1 B5 00 00
          22 00 ...
        """
        payload = bytes(181)
        frames = segment_collect(0x0509, payload)
        f0 = frames[0]
        assert f0[0] == 0x21
        assert f0[1] == 0x09 and f0[2] == 0x05
        assert f0[3] == _LEN_BASE             # 0xB0
        assert f0[4] == _LEN_ESCAPE           # 0xC1  – special marker
        assert f0[5] == 181                   # actual length in v5
        # Payload starts at v6 → 2 bytes in first frame
        assert f0[6:8] == bytes([0x00, 0x00])

    def test_len_0xC1_collision_handled(self):
        """Payload of exactly 0xC1=193 bytes must use the escape encoding."""
        payload = bytes(0xC1)
        frames = segment_collect(0x0100, payload)
        f0 = frames[0]
        assert f0[3] == _LEN_BASE
        assert f0[4] == _LEN_ESCAPE
        assert f0[5] == 0xC1

    def test_roundtrip_payload_integrity(self):
        """Reassemble all frame payloads and verify original data is intact."""
        original = bytes(range(200))
        did = 0x0224
        frames = segment_collect(did, original)

        # Extract payload bytes from frames
        # First frame: header tells us where payload starts
        f0 = frames[0]
        v3 = f0[3]
        if v3 != _LEN_BASE:
            # short: payload at v4
            length = v3 - _LEN_BASE
            collected = bytearray(f0[4:4 + min(4, length)])
        else:
            if f0[4] == _LEN_ESCAPE:
                length = f0[5]
                collected = bytearray(f0[6:8])
            else:
                length = f0[4]
                collected = bytearray(f0[5:8])

        for frame in frames[1:]:
            collected.extend(frame[1:8])

        assert bytes(collected[:length]) == original


# ---------------------------------------------------------------------------
# Encoders
# ---------------------------------------------------------------------------

class TestRawEncoder:

    def test_reads_from_store(self):
        store = make_store("256 00 D7\n")
        enc = RawEncoder({"val": ""})
        assert enc.encode(256, store) == bytes([0x00, 0xD7])

    def test_fixed_value_overrides_store(self):
        store = make_store("256 00 D7\n")
        enc = RawEncoder({"val": "AA BB CC"})
        assert enc.encode(256, store) == bytes([0xAA, 0xBB, 0xCC])

    def test_fixed_value_no_spaces(self):
        store = make_store("256 00 D7\n")
        enc = RawEncoder({"val": "AABBCC"})
        assert enc.encode(256, store) == bytes([0xAA, 0xBB, 0xCC])

    def test_unknown_did_returns_none(self):
        store = make_store("256 00 D7\n")
        enc = RawEncoder({"val": ""})
        assert enc.encode(9999, store) is None

    def test_odd_hex_raises(self):
        with pytest.raises(ValueError):
            RawEncoder({"val": "ABC"})

    def test_empty_args(self):
        store = make_store("256 FF\n")
        enc = RawEncoder({})
        assert enc.encode(256, store) == bytes([0xFF])


class TestLocaltimeEncoder:

    def test_returns_3_bytes(self):
        store = make_store("256 00\n")
        enc = LocaltimeEncoder({"format": "hhmmss"})
        result = enc.encode(256, store)
        assert len(result) == 3

    def test_values_in_valid_range(self):
        store = make_store("256 00\n")
        enc = LocaltimeEncoder({"format": "hhmmss"})
        result = enc.encode(256, store)
        h, m, s = result
        assert 0 <= h <= 23
        assert 0 <= m <= 59
        assert 0 <= s <= 59

    def test_matches_current_time(self):
        store = make_store("256 00\n")
        enc = LocaltimeEncoder({"format": "hhmmss"})
        before = time.localtime()
        result = enc.encode(256, store)
        after = time.localtime()
        h, m, s = result
        # Hour and minute must match either before or after (handles second boundary)
        assert h in (before.tm_hour, after.tm_hour)
        assert m in (before.tm_min, after.tm_min)

    def test_unsupported_format_raises(self):
        with pytest.raises(ValueError):
            LocaltimeEncoder({"format": "iso8601"})

    def test_default_format_is_hhmmss(self):
        store = make_store("256 00\n")
        enc = LocaltimeEncoder({})
        result = enc.encode(256, store)
        assert len(result) == 3


class TestEncoderFactory:

    def test_raw_from_config(self):
        enc = Encoder.from_config("raw", {"val": "01 02"})
        assert isinstance(enc, RawEncoder)

    def test_localtime_from_config(self):
        enc = Encoder.from_config("localtime", {"format": "hhmmss"})
        assert isinstance(enc, LocaltimeEncoder)

    def test_case_insensitive(self):
        enc = Encoder.from_config("RAW", {"val": ""})
        assert isinstance(enc, RawEncoder)

    def test_unknown_fct_raises(self):
        with pytest.raises(ValueError, match="Unknown encoder"):
            Encoder.from_config("nonexistent", {})
