"""
datastore.py – Storage and retrieval of datapoint values for a simulated device.

Design notes
------------
The DatapointStore is intentionally kept as a thin abstraction over a plain
dict so that it can be swapped out (or sub-classed) later without touching
device.py or the protocol handlers.

Planned extension points (not yet implemented):
  - Dynamic value resolvers: a per-DID callable that computes the response
    bytes at query time (e.g. current timestamp, counter, sine wave).
  - Write-back hooks: callbacks invoked after a successful WriteDataByIdentifier
    so external components (MQTT bridge, logging, etc.) can react.
  - Persistence: optional flush-to-disk after every write.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class DatapointStore:
    """
    Holds the raw byte values for every datapoint (DID) of one simulated device.

    Storage format (text file, one datapoint per line)::

        <decimal-DID> <hex-bytes>
        700 0A 1B 2C 3D
        1234 FF 00 AB

    Attributes
    ----------
    _data : dict[int, bytes]
        Static datapoint values loaded from the dpList file.
    _resolvers : dict[int, Callable[[], bytes]]
        Optional dynamic resolvers keyed by DID.  A resolver takes no
        arguments and returns bytes.  It overrides the static value when
        present.  (Reserved for future use – not called in v0.1.)
    """

    def __init__(self) -> None:
        self._data: Dict[int, bytes] = {}
        # Extension point: register a callable per DID to generate dynamic
        # response bytes at query time.  Example:
        #   store.register_resolver(0x0100, lambda: current_time_bytes())
        self._resolvers: Dict[int, Callable[[], bytes]] = {}

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    @classmethod
    def from_file(cls, path: Path | str) -> "DatapointStore":
        """
        Create a DatapointStore by parsing a dpList text file.

        Parameters
        ----------
        path :
            Path to the text file.  Each non-empty, non-comment line must
            follow the format ``<decimal-DID> <HEX HEX ...>``. Delimiter between bytes is optional.
            Lines starting with ``#`` are ignored.

        Returns
        -------
        DatapointStore
            Populated store ready to use.

        Raises
        ------
        FileNotFoundError
            If the file does not exist.
        ValueError
            If a line cannot be parsed.
        """
        store = cls()
        path = Path(path)
        logger.debug("Loading datapoints from %s", path)

        with path.open("r", encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 1:
                    raise ValueError(
                        f"{path}:{lineno}: expected '<DID> [<HEX ...>]', got {line!r}"
                    )
                try:
                    did = int(parts[0])
                    if len(parts) == 1:
                        dataStr = []  # DID with zero-length payload
                    elif len(parts) > 2:
                        dataStr = parts[1:]  # delimiter used between bytes
                    else:
                        it = iter(parts[1])  # no delimiter used
                        dataStr = ["".join(next(iter(it)) for idx in range(size)) for size in [2]*(len(parts[1])//2)]
                    data = bytes(int(b, 16) for b in dataStr)
                except ValueError as exc:
                    raise ValueError(
                        f"{path}:{lineno}: parse error – {exc}"
                    ) from exc

                store._data[did] = data
                logger.debug("  DID %d → %s", did, data.hex(" "))

        logger.info("Loaded %d datapoints from %s", len(store._data), path)
        return store

    # ------------------------------------------------------------------
    # Read / Write
    # ------------------------------------------------------------------

    def read(self, did: int) -> Optional[bytes]:
        """
        Return the current bytes for *did*, or ``None`` if unknown.

        Resolution order:
        1. Dynamic resolver (future use).
        2. Static value from ``_data``.
        """
        # Extension point: dynamic resolvers will be checked first.
        if did in self._resolvers:
            return self._resolvers[did]()
        return self._data.get(did)

    def write(self, did: int, value: bytes) -> bool:
        """
        Overwrite the stored value for *did*.

        Parameters
        ----------
        did :
            Datapoint identifier.
        value :
            New raw bytes to store.

        Returns
        -------
        bool
            ``True`` if the DID was known and the write succeeded,
            ``False`` if the DID is not present in this store (unknown DID).

        Notes
        -----
        Resolvers are *not* updated by a write; the static fallback value
        is updated instead.  This mirrors the behaviour of a real ECU where
        the persistent value is modified even if a dynamic override exists.
        """
        if did not in self._data:
            return False
        self._data[did] = value
        logger.debug("DID %d written: %s", did, value.hex(" "))
        # Extension point: invoke write-back hooks here in a future version.
        return True

    # ------------------------------------------------------------------
    # Extension helpers (reserved for future use)
    # ------------------------------------------------------------------

    def register_resolver(self, did: int, fn: Callable[[], bytes]) -> None:
        """
        Register a dynamic resolver for *did*.

        The resolver callable is invoked on every ``read()`` call and its
        return value overrides the static data.  Useful for datapoints whose
        value should reflect real-time state (clock, counters, sensor
        simulation).

        This method is part of the planned extension API and is not called
        by any other module in v0.1.
        """
        self._resolvers[did] = fn

    def known_dids(self) -> list[int]:
        """Return a sorted list of all statically known DIDs."""
        return sorted(self._data.keys())

    def __len__(self) -> int:
        return len(self._data)

    def __repr__(self) -> str:
        return f"DatapointStore({len(self._data)} DIDs)"
