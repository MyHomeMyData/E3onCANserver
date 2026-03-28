"""
cyclic.py – CyclicTask: unsolicited broadcast scheduler for one device.

A CyclicTask manages a list of scheduled messages.  Each message has:
  - a DID
  - an Encoder that produces the payload bytes
  - a ``schedule`` interval in seconds

All messages of one device are sent on the same CAN-ID (``tx_id`` from the
``"cyclic"`` block in devices.json) using the collect protocol framing.

The scheduler is deliberately simple: each message gets its own independent
asyncio.sleep loop.  This matches the real device behaviour where each
datapoint is sent at its own cadence independently of the others.

Extension points
----------------
* A future ``jitter`` parameter could add random delay to avoid all devices
  transmitting in lockstep.
* The encoder list can be extended; see ``simulator/protocol/encoders.py``.
* A ``count`` parameter could limit the number of transmissions (useful for
  testing).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional

from simulator.bus import CANBus
from simulator.datastore import DatapointStore
from simulator.protocol.collect import segment_collect
from simulator.protocol.encoders import Encoder

logger = logging.getLogger(__name__)


@dataclass
class CyclicMessage:
    """
    One entry in the cyclic broadcast schedule.

    Attributes
    ----------
    did :
        Data identifier to broadcast.
    schedule :
        Transmission interval in seconds.
    encoder :
        Encoder instance that produces the payload bytes.
    """
    did: int
    schedule: float
    encoder: Encoder


class CyclicTask:
    """
    Sends unsolicited collect-protocol messages for one device.

    Parameters
    ----------
    device_name :
        Human-readable device name (for logging).
    tx_id :
        CAN arbitration ID on which to send broadcast frames.
    messages :
        List of CyclicMessage descriptors.
    bus :
        Shared CANBus instance.
    store :
        DatapointStore of the owning device (read-only by encoders).
    """

    def __init__(
        self,
        device_name: str,
        tx_id: int,
        messages: List[CyclicMessage],
        bus: CANBus,
        store: DatapointStore,
    ) -> None:
        self._device_name = device_name
        self._tx_id = tx_id
        self._messages = messages
        self._bus = bus
        self._store = store
        self._tasks: List[asyncio.Task] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Launch one asyncio task per scheduled message."""
        for msg in self._messages:
            task = asyncio.create_task(
                self._run_message(msg),
                name=f"cyclic-{self._device_name}-did{msg.did:04X}",
            )
            self._tasks.append(task)
        logger.info(
            "[%s] cyclic TX started – %d message(s) on CAN-ID 0x%03X",
            self._device_name, len(self._messages), self._tx_id,
        )

    async def stop(self) -> None:
        """Cancel all per-message tasks and wait for them to finish."""
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("[%s] cyclic TX stopped", self._device_name)

    # ------------------------------------------------------------------
    # Internal: per-message broadcast loop
    # ------------------------------------------------------------------

    async def _run_message(self, msg: CyclicMessage) -> None:
        """
        Repeatedly encode and broadcast one DID at its configured interval.

        The first transmission happens after the first ``schedule`` delay so
        that all devices have finished initialising before any data hits the
        bus.
        """
        logger.debug(
            "[%s] cyclic DID 0x%04X every %.1fs on 0x%03X",
            self._device_name, msg.did, msg.schedule, self._tx_id,
        )
        try:
            while True:
                await asyncio.sleep(msg.schedule)
                await self._send(msg)
        except asyncio.CancelledError:
            logger.debug(
                "[%s] cyclic DID 0x%04X cancelled", self._device_name, msg.did
            )
            raise

    async def _send(self, msg: CyclicMessage) -> None:
        """Encode the payload and transmit all collect-protocol frames."""
        payload: Optional[bytes] = msg.encoder.encode(msg.did, self._store)
        if payload is None:
            logger.debug(
                "[%s] cyclic DID 0x%04X: encoder returned None, skipping",
                self._device_name, msg.did,
            )
            return

        try:
            frames = segment_collect(msg.did, payload)
        except ValueError as exc:
            logger.warning(
                "[%s] cyclic DID 0x%04X: segmentation error – %s",
                self._device_name, msg.did, exc,
            )
            return

        for frame in frames:
            await self._bus.send(self._tx_id, frame)

        logger.debug(
            "[%s] cyclic DID 0x%04X: sent %d frame(s), payload %s",
            self._device_name, msg.did, len(frames), payload.hex(" "),
        )
