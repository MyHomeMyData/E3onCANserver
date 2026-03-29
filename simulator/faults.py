"""
faults.py – Configurable delay and fault injection for UDS response frames.

Design
------
FaultConfig is a pure data class that holds the delay and error settings for
one device.  It is instantiated once per device from the devices.json config
and/or the command-line defaults, then passed to SimulatedDevice.

The actual injection happens in ``FaultInjector.apply()``, which receives the
**complete** ISO-TP frame list (FF + all CFs) produced by the normal
segmentation path and returns a (possibly modified) list.  When error_pct is
0 the method is a guaranteed no-op – no random calls, no overhead.

Important: ``send_frames()`` receives the complete frame list and also handles
the Flow Control handshake internally for multi-frame responses.  This keeps
the fault injection logic coherent: every fault type sees the full picture.

ISO-TP frame layout (reference for byte positions)
---------------------------------------------------
Single Frame (SF):
  byte 0 : 0x0n  (n = payload length, 1–7)
  byte 1 : UDS service byte  (e.g. 0x62 for positive ReadDataByIdentifier)
  byte 2 : DID high byte
  byte 3 : DID low byte
  byte 4+: payload data / padding

First Frame (FF):
  byte 0 : 0x1H  (H = high nibble of total UDS payload length)
  byte 1 : low byte of total UDS payload length
  byte 2 : UDS service byte
  byte 3 : DID high byte
  byte 4 : DID low byte
  byte 5+: first bytes of payload data

Consecutive Frame (CF):
  byte 0 : 0x2n  (n = sequence number 1–15, then 0)
  byte 1+: payload data / padding

Fault types
-----------
SF-capable faults (work on any response)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
WRONG_DID      Replace DID bytes with a random different DID.
               SF: bytes 2,3 of frame[0].
               FF: bytes 3,4 of frame[0].
WRONG_SERVICE  Replace the UDS service byte with 0x00.
               SF: byte 1 of frame[0].
               FF: byte 2 of frame[0].
SHORT_PAYLOAD  Truncate the last frame by padding 1–3 bytes earlier.
WRONG_PADDING  Replace all 0xCC padding bytes with 0xAA.

MF-only faults (require at least one CF)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
DROP_RANDOM_CF  Drop a randomly chosen CF.
DROP_LAST_CF    Drop the last CF.
WRONG_SEQ       Corrupt the sequence nibble of one CF by +1.
DUPLICATE_CF    Send one CF twice consecutively.
TRUNCATED_MF    Send only the FF, drop all CFs.
WRONG_LEN       Inflate the total-length field in the FF header by 1.
                FF: bits [11:8] in byte 0, bits [7:0] in byte 1.

Extension guide
---------------
1. Add a name to ``FaultType``.
2. Implement ``_apply_<name>`` on ``FaultInjector``.
3. Register it in ``_SF_FAULTS`` or ``_MF_FAULTS`` (or both).
"""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass
from enum import Enum, auto
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DELAY_MAX_MS  = 200
ERROR_PCT_MAX = 20.0
CAN_DLC       = 8

# Nibble masks for ISO-TP frame type detection
_ISO_TP_FF_TYPE = 0x1
_ISO_TP_CF_TYPE = 0x2
_ISO_TP_CF_MASK = 0x20   # high nibble of CF byte


# ---------------------------------------------------------------------------
# FaultConfig
# ---------------------------------------------------------------------------

@dataclass
class FaultConfig:
    """
    Holds the delay and error-injection settings for one device.

    Parameters
    ----------
    delay_ms :
        Inter-frame delay in milliseconds (0 = no delay, max 200).
    error_pct :
        Fraction of UDS responses that will be deliberately corrupted,
        expressed as a percentage (0.0 = never, max 20.0).

    Both values are clamped to their valid ranges on construction.
    """
    delay_ms:  int   = 0
    error_pct: float = 0.0

    def __post_init__(self) -> None:
        self.delay_ms  = max(0, min(int(self.delay_ms),  DELAY_MAX_MS))
        self.error_pct = max(0.0, min(float(self.error_pct), ERROR_PCT_MAX))

    @classmethod
    def from_config(
        cls,
        device_entry: dict,
        cli_delay_ms:  Optional[int]   = None,
        cli_error_pct: Optional[float] = None,
    ) -> "FaultConfig":
        """
        Build a FaultConfig from a device JSON entry and CLI defaults.

        Priority rule: device-level value wins over CLI value.
        CLI value wins over the built-in default (0 / 0.0).
        """
        delay_ms  = cli_delay_ms  if cli_delay_ms  is not None else 0
        error_pct = cli_error_pct if cli_error_pct is not None else 0.0

        if "delay"  in device_entry:
            delay_ms  = int(device_entry["delay"])
        if "errors" in device_entry:
            error_pct = float(device_entry["errors"])

        return cls(delay_ms=delay_ms, error_pct=error_pct)

    @property
    def has_delay(self) -> bool:
        return self.delay_ms > 0

    @property
    def has_errors(self) -> bool:
        return self.error_pct > 0.0


# ---------------------------------------------------------------------------
# FaultType enum
# ---------------------------------------------------------------------------

class FaultType(Enum):
    # SF-capable
    WRONG_DID      = auto()
    WRONG_SERVICE  = auto()
    SHORT_PAYLOAD  = auto()
    WRONG_PADDING  = auto()
    # MF-only
    DROP_RANDOM_CF = auto()
    DROP_LAST_CF   = auto()
    WRONG_SEQ      = auto()
    DUPLICATE_CF   = auto()
    TRUNCATED_MF   = auto()
    WRONG_LEN      = auto()


_SF_FAULTS: List[FaultType] = [
    FaultType.WRONG_DID,
    FaultType.WRONG_SERVICE,
    FaultType.SHORT_PAYLOAD,
    FaultType.WRONG_PADDING,
]

_MF_FAULTS: List[FaultType] = _SF_FAULTS + [
    FaultType.DROP_RANDOM_CF,
    FaultType.DROP_LAST_CF,
    FaultType.WRONG_SEQ,
    FaultType.DUPLICATE_CF,
    FaultType.TRUNCATED_MF,
    FaultType.WRONG_LEN,
]


# ---------------------------------------------------------------------------
# FaultInjector
# ---------------------------------------------------------------------------

SendFn = Callable[[bytes], Awaitable[None]]


class FaultInjector:
    """
    Applies delay and fault injection to a complete ISO-TP frame list.

    Parameters
    ----------
    config :
        FaultConfig for this device.
    device_name :
        Used in log messages.
    """

    def __init__(self, config: FaultConfig, device_name: str) -> None:
        self._cfg  = config
        self._name = device_name

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send_frames(
        self,
        frames: List[bytes],
        send_fn: SendFn,
        wait_for_fc: Optional[Callable[[], Awaitable[Optional[bytes]]]] = None,
    ) -> None:
        """
        Send *frames* applying delay and fault injection.

        Always receives the **complete** frame list (FF + all CFs for MF,
        or just the SF).  Handles the Flow Control handshake internally for
        multi-frame responses.

        Parameters
        ----------
        frames :
            Complete ISO-TP frame list from ``segment()``.
        send_fn :
            Async callable that transmits one 8-byte CAN frame.
        wait_for_fc :
            Async callable that waits for a FC frame from the client and
            returns its bytes, or None on timeout.  Required for MF responses;
            ignored for SF.
        """
        frames = self._maybe_inject(frames)
        is_multi = len(frames) > 1

        if not is_multi:
            # Single Frame – send and done.
            await send_fn(frames[0])
            if self._cfg.has_delay:
                await asyncio.sleep(self._cfg.delay_ms / 1000.0)
            return

        # Multi-Frame: send FF, wait for FC, then send CFs.
        await send_fn(frames[0])
        if self._cfg.has_delay:
            await asyncio.sleep(self._cfg.delay_ms / 1000.0)

        if wait_for_fc is not None:
            fc = await wait_for_fc()
            if fc is None:
                logger.warning(
                    "[%s] no Flow Control received after FF, aborting", self._name
                )
                return

        for cf in frames[1:]:
            await send_fn(cf)
            if self._cfg.has_delay:
                await asyncio.sleep(self._cfg.delay_ms / 1000.0)

    # ------------------------------------------------------------------
    # Fault selection
    # ------------------------------------------------------------------

    def _maybe_inject(self, frames: List[bytes]) -> List[bytes]:
        """Return *frames* unchanged, or a corrupted copy based on error_pct."""
        if not self._cfg.has_errors:
            return frames  # guaranteed no-op, no random call

        if random.random() * 100.0 >= self._cfg.error_pct:
            return frames  # this response stays clean

        is_multi = len(frames) > 1
        pool = _MF_FAULTS if is_multi else _SF_FAULTS
        fault = random.choice(pool)
        logger.debug(
            "[%s] injecting fault %s into %d-frame response",
            self._name, fault.name, len(frames),
        )
        return self._apply(frames, fault)

    def _apply(self, frames: List[bytes], fault: FaultType) -> List[bytes]:
        """Dispatch to the appropriate fault implementation."""
        handler = {
            FaultType.WRONG_DID:      self._wrong_did,
            FaultType.WRONG_SERVICE:  self._wrong_service,
            FaultType.SHORT_PAYLOAD:  self._short_payload,
            FaultType.WRONG_PADDING:  self._wrong_padding,
            FaultType.DROP_RANDOM_CF: self._drop_random_cf,
            FaultType.DROP_LAST_CF:   self._drop_last_cf,
            FaultType.WRONG_SEQ:      self._wrong_seq,
            FaultType.DUPLICATE_CF:   self._duplicate_cf,
            FaultType.TRUNCATED_MF:   self._truncated_mf,
            FaultType.WRONG_LEN:      self._wrong_len,
        }.get(fault)
        if handler is None:
            logger.error("[%s] unknown fault type %s", self._name, fault)
            return frames
        try:
            return handler(frames)
        except Exception as exc:
            logger.error("[%s] fault %s raised %s", self._name, fault.name, exc)
            return frames

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_ff(frame: bytes) -> bool:
        return (frame[0] >> 4) == _ISO_TP_FF_TYPE

    # ------------------------------------------------------------------
    # Fault implementations
    # Each receives the complete frame list and returns a modified copy.
    # All mutations are on bytearrays; results are returned as bytes.
    # No payload data bytes are ever touched.
    # ------------------------------------------------------------------

    def _wrong_did(self, frames: List[bytes]) -> List[bytes]:
        """
        Corrupt the DID bytes in the first frame header only.

        SF: DID at bytes [2, 3]   (after [len][svc])
        FF: DID at bytes [3, 4]   (after [0x1x][len_lo][svc])
        """
        f = bytearray(frames[0])
        if self._is_ff(f):
            hi_pos, lo_pos = 3, 4
        else:
            hi_pos, lo_pos = 2, 3

        orig = (f[hi_pos] << 8) | f[lo_pos]
        new_did = (orig + random.randint(1, 254)) & 0xFFFF
        f[hi_pos] = (new_did >> 8) & 0xFF
        f[lo_pos] = new_did & 0xFF
        logger.debug(
            "[%s] WRONG_DID: 0x%04X → 0x%04X (bytes %d,%d)",
            self._name, orig, new_did, hi_pos, lo_pos,
        )
        return [bytes(f)] + frames[1:]

    def _wrong_service(self, frames: List[bytes]) -> List[bytes]:
        """
        Replace the UDS service byte with 0x00.

        SF: service byte at index 1   (after [len])
        FF: service byte at index 2   (after [0x1x][len_lo])
        """
        f = bytearray(frames[0])
        svc_pos = 2 if self._is_ff(f) else 1
        orig = f[svc_pos]
        f[svc_pos] = 0x00
        logger.debug(
            "[%s] WRONG_SERVICE: 0x%02X → 0x00 at byte %d",
            self._name, orig, svc_pos,
        )
        return [bytes(f)] + frames[1:]

    def _short_payload(self, frames: List[bytes]) -> List[bytes]:
        """
        Truncate the last frame by replacing the final 1–3 bytes with padding.
        Only the last frame is modified; no mid-stream data bytes are touched.
        """
        result = [bytearray(f) for f in frames]
        last = result[-1]
        n = random.randint(1, 3)
        for i in range(CAN_DLC - 1, CAN_DLC - 1 - n, -1):
            last[i] = 0xCC
        logger.debug("[%s] SHORT_PAYLOAD: early-padded last %d byte(s)", self._name, n)
        return [bytes(f) for f in result]

    def _wrong_padding(self, frames: List[bytes]) -> List[bytes]:
        """Replace all 0xCC padding bytes with 0xAA in every frame."""
        result = []
        for frame in frames:
            f = bytearray(frame)
            for i in range(CAN_DLC):
                if f[i] == 0xCC:
                    f[i] = 0xAA
            result.append(bytes(f))
        logger.debug("[%s] WRONG_PADDING: 0xCC → 0xAA", self._name)
        return result

    def _drop_random_cf(self, frames: List[bytes]) -> List[bytes]:
        """Drop one randomly chosen CF (index ≥ 1 in the complete frame list)."""
        if len(frames) < 2:
            return frames
        idx = random.randint(1, len(frames) - 1)
        logger.debug("[%s] DROP_RANDOM_CF: dropping frame[%d]", self._name, idx)
        return frames[:idx] + frames[idx + 1:]

    def _drop_last_cf(self, frames: List[bytes]) -> List[bytes]:
        """Drop the last CF."""
        if len(frames) < 2:
            return frames
        logger.debug("[%s] DROP_LAST_CF: dropping frame[%d]", self._name, len(frames) - 1)
        return frames[:-1]

    def _wrong_seq(self, frames: List[bytes]) -> List[bytes]:
        """Corrupt the sequence nibble of one CF by +1 (modulo 16)."""
        if len(frames) < 2:
            return frames
        result = [bytearray(f) for f in frames]
        idx = random.randint(1, len(frames) - 1)
        orig = result[idx][0]
        bad  = _ISO_TP_CF_MASK | ((orig + 1) & 0x0F)
        result[idx][0] = bad
        logger.debug(
            "[%s] WRONG_SEQ: frame[%d] byte0 0x%02X → 0x%02X",
            self._name, idx, orig, bad,
        )
        return [bytes(f) for f in result]

    def _duplicate_cf(self, frames: List[bytes]) -> List[bytes]:
        """Insert a duplicate of one CF immediately after itself."""
        if len(frames) < 2:
            return frames
        idx = random.randint(1, len(frames) - 1)
        logger.debug("[%s] DUPLICATE_CF: duplicating frame[%d]", self._name, idx)
        return frames[:idx + 1] + [frames[idx]] + frames[idx + 1:]

    def _truncated_mf(self, frames: List[bytes]) -> List[bytes]:
        """Send only the FF – drop all CFs."""
        logger.debug("[%s] TRUNCATED_MF: sending FF only, dropping %d CF(s)",
                     self._name, len(frames) - 1)
        return frames[:1]

    def _wrong_len(self, frames: List[bytes]) -> List[bytes]:
        """
        Inflate the total-length field in the FF header by 1.

        FF length: bits [11:8] packed in byte 0 (low nibble),
                   bits [7:0]  in byte 1.
        """
        if len(frames) < 2:
            return frames
        f = bytearray(frames[0])
        orig_len = ((f[0] & 0x0F) << 8) | f[1]
        new_len  = min(orig_len + 1, 0xFFF)
        f[0] = (f[0] & 0xF0) | ((new_len >> 8) & 0x0F)
        f[1] = new_len & 0xFF
        logger.debug(
            "[%s] WRONG_LEN: FF announced length %d → %d",
            self._name, orig_len, new_len,
        )
        return [bytes(f)] + frames[1:]
