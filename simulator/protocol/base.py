"""
protocol/base.py – Abstract base class for protocol handlers.

Every protocol handler receives a fully reassembled UDS payload (bytes) and
the DatapointStore of the device it is serving, and must return the response
payload bytes (or None to send no response).

The physical framing (ISO-TP single/multi-frame, CAN arbitration IDs) is
handled by the transport layer in isotp.py and is invisible to the handler.

Extension guide
---------------
To add a new protocol (e.g. a proprietary Viessmann broadcast protocol):

1. Create ``simulator/protocol/myproto.py``.
2. Sub-class ``ProtocolHandler`` and implement ``handle()``.
3. Register the handler in device.py by passing the new class as the
   ``protocol_class`` argument to ``SimulatedDevice``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from simulator.datastore import DatapointStore


class ProtocolHandler(ABC):
    """
    Abstract base class for all protocol handlers.

    A handler is stateless with respect to datapoint storage – it receives
    the store as a parameter so the same handler class can be shared across
    multiple device instances.

    Parameters
    ----------
    (none at construction time – handlers are instantiated without arguments
    so they can be used as a strategy object.)
    """

    @abstractmethod
    def handle(self, payload: bytes, store: DatapointStore) -> Optional[bytes]:
        """
        Process a fully reassembled diagnostic request payload.

        Parameters
        ----------
        payload :
            Raw request bytes as delivered by the transport layer (ISO-TP).
            The first byte is the UDS service ID (or equivalent).
        store :
            The DatapointStore of the device being addressed.

        Returns
        -------
        bytes
            The response payload to be sent back via the transport layer.
        None
            Suppress the response entirely (no reply on the bus).
        """

    @property
    def name(self) -> str:
        """Human-readable protocol name, used in log output."""
        return self.__class__.__name__
