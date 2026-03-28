"""
protocol/encoders.py – Encoder classes for cyclic (unsolicited) CAN messages.

An encoder is responsible for producing the raw payload bytes for one DID
at the moment the cyclic scheduler fires.  Encoders are intentionally kept
as simple callable objects so that new ones can be added without touching
the scheduler or the segmentation layer.

Available encoders
------------------
RawEncoder
    Returns the value stored in the DatapointStore for a given DID, or an
    optional fixed byte string supplied at configuration time.

LocaltimeEncoder
    Returns the current local wall-clock time encoded as 3 bytes
    [HH, MM, SS].  The ``format`` parameter selects the byte order;
    currently only ``"hhmmss"`` is defined.

Extension guide
---------------
To add a new encoder:
1. Sub-class ``Encoder``.
2. Implement ``encode(did, store) -> bytes``.
3. Register the class name (lower-case) in ``ENCODER_REGISTRY`` at the
   bottom of this file.
4. Add the corresponding ``"fct"`` value to the devices.json schema docs.
"""

from __future__ import annotations

import time
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type

from simulator.datastore import DatapointStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class Encoder(ABC):
    """
    Base class for all cyclic-message encoders.

    Parameters
    ----------
    args :
        Arbitrary keyword arguments from the ``"_args"`` section of the
        devices.json encoder block.  Sub-classes pick what they need.
    """

    def __init__(self, args: Dict[str, Any]) -> None:
        self._args = args

    @abstractmethod
    def encode(self, did: int, store: DatapointStore) -> Optional[bytes]:
        """
        Produce the payload bytes for *did*.

        Parameters
        ----------
        did :
            Data identifier of the datapoint to encode.
        store :
            DatapointStore of the sending device.

        Returns
        -------
        bytes
            Payload to be handed to the collect segmenter.
        None
            Skip this transmission (e.g. DID not found and no fallback).
        """

    @classmethod
    def from_config(cls, fct: str, args: Dict[str, Any]) -> "Encoder":
        """
        Factory: look up *fct* in ``ENCODER_REGISTRY`` and instantiate.

        Raises
        ------
        ValueError
            If *fct* is not a registered encoder name.
        """
        encoder_cls = ENCODER_REGISTRY.get(fct.lower())
        if encoder_cls is None:
            raise ValueError(
                f"Unknown encoder function '{fct}'. "
                f"Available: {list(ENCODER_REGISTRY)}"
            )
        return encoder_cls(args)


# ---------------------------------------------------------------------------
# RawEncoder
# ---------------------------------------------------------------------------

class RawEncoder(Encoder):
    """
    Return the raw bytes stored in the DatapointStore for the given DID.

    Optional configuration
    ----------------------
    ``_args.val`` : str
        A hex byte string (same format as virtdata_xxx.txt, e.g.
        ``"01 2C"`` or ``"012C"``).  When non-empty this fixed value is
        used instead of reading from the store.  Useful for constant
        broadcast values that don't correspond to a stored datapoint.

    Examples
    --------
    Read from store::

        {"fct": "raw", "_args": {"val": ""}}

    Fixed value::

        {"fct": "raw", "_args": {"val": "01 2C FF"}}
    """

    def __init__(self, args: Dict[str, Any]) -> None:
        super().__init__(args)
        raw_val: str = args.get("val", "").strip()
        if raw_val:
            # Parse the optional fixed hex string (spaces optional).
            hex_str = raw_val.replace(" ", "")
            if len(hex_str) % 2 != 0:
                raise ValueError(
                    f"RawEncoder: 'val' has odd number of hex digits: {raw_val!r}"
                )
            self._fixed: Optional[bytes] = bytes(
                int(hex_str[i:i+2], 16) for i in range(0, len(hex_str), 2)
            )
        else:
            self._fixed = None

    def encode(self, did: int, store: DatapointStore) -> Optional[bytes]:
        if self._fixed is not None:
            return self._fixed
        value = store.read(did)
        if value is None:
            logger.warning(
                "RawEncoder: DID 0x%04X not found in store, skipping", did
            )
        return value


# ---------------------------------------------------------------------------
# LocaltimeEncoder
# ---------------------------------------------------------------------------

class LocaltimeEncoder(Encoder):
    """
    Encode the current local wall-clock time as 3 bytes.

    Configuration
    -------------
    ``_args.format`` : str
        Byte layout of the time value.  Supported values:

        ``"hhmmss"``
            Byte 0 = hours (0–23), Byte 1 = minutes (0–59),
            Byte 2 = seconds (0–59).

    The DID and store arguments are ignored; the encoder always returns the
    current system time.

    Examples
    --------
    ::

        {"fct": "localtime", "_args": {"format": "hhmmss"}}
    """

    _SUPPORTED_FORMATS = ("hhmmss",)

    def __init__(self, args: Dict[str, Any]) -> None:
        super().__init__(args)
        self._format = args.get("format", "hhmmss").lower()
        if self._format not in self._SUPPORTED_FORMATS:
            raise ValueError(
                f"LocaltimeEncoder: unsupported format '{self._format}'. "
                f"Supported: {self._SUPPORTED_FORMATS}"
            )

    def encode(self, did: int, store: DatapointStore) -> Optional[bytes]:
        t = time.localtime()
        if self._format == "hhmmss":
            return bytes([t.tm_hour, t.tm_min, t.tm_sec])
        # Unreachable due to __init__ guard, but keeps mypy happy.
        return None  # pragma: no cover


# ---------------------------------------------------------------------------
# Registry – maps config "fct" strings to encoder classes
# Extension point: add new encoders here.
# ---------------------------------------------------------------------------

ENCODER_REGISTRY: Dict[str, Type[Encoder]] = {
    "raw":       RawEncoder,
    "localtime": LocaltimeEncoder,
}
