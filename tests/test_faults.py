"""
tests/test_faults.py – Tests for FaultConfig and FaultInjector.

Key guarantees verified
-----------------------
* All fault types operate only on header/framing bytes, never on payload data.
* WRONG_DID and WRONG_SERVICE hit the correct byte positions for both SF and FF.
* MF-only faults are safe no-ops on SF input.
* error_pct=0 is a guaranteed no-op (same list object returned).
* Delay: asyncio.sleep is called after every frame, with the correct duration.
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import patch

from simulator.faults import (
    FaultConfig, FaultInjector, FaultType,
    DELAY_MAX_MS, ERROR_PCT_MAX, CAN_DLC,
)
from simulator.protocol.isotp import segment


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(payload_len: int) -> bytes:
    """Build a realistic UDS positive ReadDataByIdentifier response."""
    # [0x62][DID_HI=0x01][DID_LO=0x00][data...]
    return bytes([0x62, 0x01, 0x00]) + bytes(range(0x10, 0x10 + payload_len))


def sf_frames():
    """Single-frame response: payload fits in one ISO-TP frame (≤7 UDS bytes)."""
    return segment(_make_response(4))   # 3 header + 4 data = 7 bytes → SF


def mf_frames():
    """Multi-frame response: needs FF + several CFs."""
    return segment(_make_response(40))  # 3 header + 40 data = 43 bytes → MF


def inj(delay_ms=0, error_pct=0.0) -> FaultInjector:
    return FaultInjector(FaultConfig(delay_ms=delay_ms, error_pct=error_pct), "test")


# ---------------------------------------------------------------------------
# FaultConfig
# ---------------------------------------------------------------------------

class TestFaultConfig:

    def test_defaults(self):
        c = FaultConfig()
        assert c.delay_ms == 0 and c.error_pct == 0.0

    def test_clamp_delay_max(self):
        assert FaultConfig(delay_ms=999).delay_ms == DELAY_MAX_MS

    def test_clamp_delay_negative(self):
        assert FaultConfig(delay_ms=-5).delay_ms == 0

    def test_clamp_error_max(self):
        assert FaultConfig(error_pct=99.9).error_pct == ERROR_PCT_MAX

    def test_clamp_error_negative(self):
        assert FaultConfig(error_pct=-1.0).error_pct == 0.0

    def test_has_delay_false(self):
        assert not FaultConfig(delay_ms=0).has_delay

    def test_has_delay_true(self):
        assert FaultConfig(delay_ms=10).has_delay

    def test_has_errors_false(self):
        assert not FaultConfig(error_pct=0.0).has_errors

    def test_has_errors_true(self):
        assert FaultConfig(error_pct=5.0).has_errors

    def test_from_config_defaults(self):
        c = FaultConfig.from_config({})
        assert c.delay_ms == 0 and c.error_pct == 0.0

    def test_from_config_cli_wins_over_default(self):
        c = FaultConfig.from_config({}, cli_delay_ms=50, cli_error_pct=5.0)
        assert c.delay_ms == 50 and c.error_pct == 5.0

    def test_from_config_device_wins_over_cli(self):
        c = FaultConfig.from_config({"delay": 30, "errors": 2.5},
                                    cli_delay_ms=100, cli_error_pct=15.0)
        assert c.delay_ms == 30 and c.error_pct == 2.5

    def test_from_config_partial_override(self):
        c = FaultConfig.from_config({"delay": 20},
                                    cli_delay_ms=100, cli_error_pct=8.0)
        assert c.delay_ms == 20 and c.error_pct == 8.0

    def test_from_config_clamping_applied(self):
        c = FaultConfig.from_config({"delay": 999, "errors": 99.0})
        assert c.delay_ms == DELAY_MAX_MS and c.error_pct == ERROR_PCT_MAX


# ---------------------------------------------------------------------------
# No-op guarantee at 0%
# ---------------------------------------------------------------------------

class TestNoOp:

    def test_zero_error_returns_same_object_sf(self):
        frames = sf_frames()
        assert inj(error_pct=0.0)._maybe_inject(frames) is frames

    def test_zero_error_returns_same_object_mf(self):
        frames = mf_frames()
        assert inj(error_pct=0.0)._maybe_inject(frames) is frames


# ---------------------------------------------------------------------------
# Byte-position correctness
# ---------------------------------------------------------------------------

class TestBytePositions:
    """
    Verify that each fault touches exactly the right bytes and never corrupts
    payload data.
    """

    def _data_bytes_sf(self, frames):
        """Return the actual payload data bytes from an SF response."""
        # SF: [len][svc][did_hi][did_lo][data...padding]
        # svc=0x62, did_hi=0x01, did_lo=0x00, data starts at byte 4
        return frames[0][4:]

    def _data_bytes_mf(self, frames):
        """Return the actual payload data bytes from an MF response."""
        # FF: [0x1x][len_lo][svc][did_hi][did_lo][data bytes 0..2]
        # CFs: [seq][data bytes 3..9] ...
        data = bytearray(frames[0][5:8])        # first 3 data bytes from FF
        for cf in frames[1:]:
            data.extend(cf[1:8])                # 7 data bytes per CF
        return bytes(data)

    # WRONG_SERVICE – SF
    def test_wrong_service_sf_byte1_only(self):
        frames = sf_frames()
        result = inj()._apply(frames, FaultType.WRONG_SERVICE)
        # byte 0 = length nibble, unchanged
        assert result[0][0] == frames[0][0]
        # byte 1 = service → corrupted to 0x00
        assert result[0][1] == 0x00
        # bytes 2,3 = DID, unchanged
        assert result[0][2] == frames[0][2]
        assert result[0][3] == frames[0][3]
        # data bytes 4+ unchanged
        assert result[0][4:] == frames[0][4:]

    # WRONG_SERVICE – FF
    def test_wrong_service_ff_byte2_only(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.WRONG_SERVICE)
        # byte 0,1 = FF length field, unchanged
        assert result[0][0] == frames[0][0]
        assert result[0][1] == frames[0][1]
        # byte 2 = service → corrupted
        assert result[0][2] == 0x00
        # bytes 3,4 = DID, unchanged
        assert result[0][3] == frames[0][3]
        assert result[0][4] == frames[0][4]
        # data bytes 5+ unchanged
        assert result[0][5:] == frames[0][5:]
        # CFs completely unchanged
        assert result[1:] == frames[1:]

    # WRONG_DID – SF
    def test_wrong_did_sf_bytes23_only(self):
        frames = sf_frames()
        result = inj()._apply(frames, FaultType.WRONG_DID)
        # byte 0 = length, unchanged; byte 1 = service, unchanged
        assert result[0][0] == frames[0][0]
        assert result[0][1] == frames[0][1]
        # bytes 2,3 = DID → changed
        new_did = (result[0][2] << 8) | result[0][3]
        orig_did = (frames[0][2] << 8) | frames[0][3]
        assert new_did != orig_did
        # data bytes 4+ unchanged
        assert result[0][4:] == frames[0][4:]

    # WRONG_DID – FF
    def test_wrong_did_ff_bytes34_only(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.WRONG_DID)
        # bytes 0,1,2 = FF header + service, unchanged
        assert result[0][0] == frames[0][0]
        assert result[0][1] == frames[0][1]
        assert result[0][2] == frames[0][2]
        # bytes 3,4 = DID → changed
        new_did = (result[0][3] << 8) | result[0][4]
        orig_did = (frames[0][3] << 8) | frames[0][4]
        assert new_did != orig_did
        # data bytes 5+ unchanged
        assert result[0][5:] == frames[0][5:]
        # CFs completely unchanged
        assert result[1:] == frames[1:]

    # WRONG_LEN – FF only
    def test_wrong_len_ff_bytes01_only(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.WRONG_LEN)
        orig_len = ((frames[0][0] & 0x0F) << 8) | frames[0][1]
        new_len  = ((result[0][0] & 0x0F) << 8) | result[0][1]
        assert new_len == orig_len + 1
        # rest of FF (svc, DID, data) unchanged
        assert result[0][2:] == frames[0][2:]
        # CFs unchanged
        assert result[1:] == frames[1:]

    # WRONG_SEQ – only CF byte 0 (sequence nibble)
    def test_wrong_seq_only_seq_nibble_changed(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.WRONG_SEQ)
        # Exactly one CF must have its sequence nibble changed
        corrupted = [
            i for i in range(1, len(frames))
            if result[i][0] != frames[i][0]
        ]
        assert len(corrupted) == 1
        idx = corrupted[0]
        # Only byte 0 changed; bytes 1-7 (data) unchanged
        assert result[idx][1:] == frames[idx][1:]
        # High nibble still 0x2x (CF marker preserved)
        assert (result[idx][0] >> 4) == 0x2
        # All other CFs completely unchanged
        for i in range(1, len(frames)):
            if i != idx:
                assert result[i] == frames[i]

    # SHORT_PAYLOAD – only last frame's trailing bytes
    def test_short_payload_only_last_frame_modified(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.SHORT_PAYLOAD)
        # All frames except the last: completely unchanged
        assert result[:-1] == frames[:-1]
        # Last frame: at least one of the last 3 bytes is padding
        assert any(result[-1][i] == 0xCC for i in range(5, 8))

    # WRONG_PADDING – only 0xCC bytes changed
    def test_wrong_padding_only_replaces_0xCC(self):
        frames = sf_frames()
        original_non_pad = [b for b in frames[0] if b != 0xCC]
        result = inj()._apply(frames, FaultType.WRONG_PADDING)
        result_non_pad = [b for b in result[0] if b != 0xAA]
        assert result_non_pad == original_non_pad
        assert 0xCC not in result[0]


# ---------------------------------------------------------------------------
# Structural fault types
# ---------------------------------------------------------------------------

class TestStructuralFaults:

    def test_drop_random_cf_reduces_count_by_one(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.DROP_RANDOM_CF)
        assert len(result) == len(frames) - 1
        assert result[0] == frames[0]      # FF untouched

    def test_drop_last_cf(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.DROP_LAST_CF)
        assert len(result) == len(frames) - 1
        assert result[-1] == frames[-2]

    def test_duplicate_cf_increases_count_by_one(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.DUPLICATE_CF)
        assert len(result) == len(frames) + 1
        assert result[0] == frames[0]

    def test_truncated_mf_sends_ff_only(self):
        frames = mf_frames()
        result = inj()._apply(frames, FaultType.TRUNCATED_MF)
        assert len(result) == 1
        assert result[0] == frames[0]

    def test_wrong_did_changes_value(self):
        frames = sf_frames()
        orig_did = (frames[0][2] << 8) | frames[0][3]
        result = inj()._apply(frames, FaultType.WRONG_DID)
        new_did = (result[0][2] << 8) | result[0][3]
        assert new_did != orig_did


# ---------------------------------------------------------------------------
# MF-only faults are safe no-ops on SF
# ---------------------------------------------------------------------------

class TestMFOnlySafety:

    MF_ONLY = [
        FaultType.DROP_RANDOM_CF,
        FaultType.DROP_LAST_CF,
        FaultType.WRONG_SEQ,
        FaultType.DUPLICATE_CF,
        FaultType.TRUNCATED_MF,
        FaultType.WRONG_LEN,
    ]

    def test_mf_faults_on_sf_return_original_unchanged(self):
        frames = sf_frames()
        for fault in self.MF_ONLY:
            result = inj()._apply(frames, fault)
            assert result == frames, \
                f"{fault.name} modified an SF frame list – must be a no-op"


# ---------------------------------------------------------------------------
# Frame size invariant: all frames must always be 8 bytes
# ---------------------------------------------------------------------------

class TestFrameSizeInvariant:

    def test_all_faults_preserve_8_byte_frames(self):
        for fault in FaultType:
            for frames in [sf_frames(), mf_frames()]:
                result = inj()._apply(frames, fault)
                for i, frame in enumerate(result):
                    assert len(frame) == CAN_DLC, \
                        f"{fault.name} produced frame[{i}] of length {len(frame)}"


# ---------------------------------------------------------------------------
# Delay behaviour
# ---------------------------------------------------------------------------

class TestDelay:

    @pytest.mark.asyncio
    async def test_no_delay_no_sleep(self):
        i = inj(delay_ms=0)
        sent = []
        async def fake_send(f): sent.append(f)

        import simulator.faults as _fm
        orig = _fm.asyncio.sleep
        sleep_log = []
        async def fake_sleep(s): sleep_log.append(s)
        _fm.asyncio.sleep = fake_sleep
        try:
            await i.send_frames(sf_frames(), fake_send)
        finally:
            _fm.asyncio.sleep = orig

        assert len(sent) == 1
        assert sleep_log == []

    @pytest.mark.asyncio
    async def test_delay_sleep_per_frame(self):
        i = inj(delay_ms=50, error_pct=0.0)
        frames = mf_frames()
        sent = []
        sleep_log = []
        async def fake_send(f): sent.append(f)

        import simulator.faults as _fm
        orig = _fm.asyncio.sleep
        async def fake_sleep(s): sleep_log.append(s)
        _fm.asyncio.sleep = fake_sleep

        fc_called = []
        async def fake_fc():
            fc_called.append(True)
            return bytes([0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

        try:
            await i.send_frames(frames, fake_send, wait_for_fc=fake_fc)
        finally:
            _fm.asyncio.sleep = orig

        assert len(sent) == len(frames)
        assert len(sleep_log) == len(frames)
        assert all(abs(s - 0.05) < 1e-9 for s in sleep_log)
        assert fc_called  # FC was awaited

    @pytest.mark.asyncio
    async def test_zero_errors_sends_all_frames(self):
        i = inj(error_pct=0.0)
        frames = mf_frames()
        sent = []
        async def fake_send(f): sent.append(f)
        async def fake_fc():
            return bytes([0x30, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

        import simulator.faults as _fm
        orig = _fm.asyncio.sleep
        async def fake_sleep(s): pass
        _fm.asyncio.sleep = fake_sleep
        try:
            await i.send_frames(frames, fake_send, wait_for_fc=fake_fc)
        finally:
            _fm.asyncio.sleep = orig

        assert sent == frames
