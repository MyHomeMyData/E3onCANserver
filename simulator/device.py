"""
device.py – SimulatedDevice: one CAN device running in its own asyncio task.

Each SimulatedDevice:
  1. Registers its TX CAN-ID with the shared CANBus for incoming requests.
  2. Runs an asyncio task that:
     a. Receives frames from the bus (via an asyncio.Queue fed by the RX callback).
     b. Passes them through the ISO-TP assembler.
     c. Hands complete UDS payloads to the protocol handler.
     d. Segments and transmits the response via ISO-TP on the response CAN-ID
        (tx_id + 0x10).
  3. Optionally runs a CyclicTask that sends unsolicited collect-protocol
     messages at configured intervals (separate CAN-ID, separate protocol).

Extension points
----------------
* Dynamic value injection: the DatapointStore's resolver API lets external
  code register callables per DID before the device starts.
* Multiple protocol handlers: the device could route requests to different
  handlers based on service ID or CAN-ID range.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional, Type

import can

from simulator.bus import CANBus
from simulator.cyclic import CyclicTask
from simulator.datastore import DatapointStore
from simulator.protocol.base import ProtocolHandler
from simulator.protocol.isotp import ISOTPAssembler, segment
from simulator.protocol.uds import UDSHandler

logger = logging.getLogger(__name__)

# The response CAN-ID is always the request CAN-ID + this offset.
RESPONSE_ID_OFFSET = 0x10


class SimulatedDevice:
    """
    Represents one simulated Viessmann E3 device on the CAN bus.

    Parameters
    ----------
    name :
        Human-readable device name (from the devices.json key, e.g. "vcal").
    tx_id :
        CAN arbitration ID on which the *client* sends requests to this device.
    dp_list_path :
        Path to the datapoint list file (dpList, currently informational).
    dp_values_path :
        Path to the datapoint values text file (virtdata_xxx.txt).
    bus :
        Shared CANBus instance.
    protocol_class :
        Protocol handler class to instantiate for this device.
        Defaults to UDSHandler.
    cyclic_task :
        Optional CyclicTask for unsolicited broadcast messages on a separate
        CAN-ID using the collect protocol.  Pass None (default) to disable.
    """

    def __init__(
        self,
        name: str,
        tx_id: int,
        dp_list_path: Path,
        dp_values_path: Path,
        bus: CANBus,
        protocol_class: Type[ProtocolHandler] = UDSHandler,
        cyclic_task: Optional[CyclicTask] = None,
    ) -> None:
        self.name = name
        self.tx_id = tx_id
        self.rx_id = tx_id + RESPONSE_ID_OFFSET  # we *send* on this ID
        self._bus = bus
        self._store = DatapointStore.from_file(dp_values_path)
        self._handler: ProtocolHandler = protocol_class()
        self._assembler = ISOTPAssembler()
        self._rx_queue: asyncio.Queue[can.Message] = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._cyclic_task: Optional[CyclicTask] = cyclic_task

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self) -> None:
        """
        Register this device's RX callback with the shared bus.

        Must be called before ``start()``.
        """
        self._bus.register_rx(self.tx_id, self._on_frame_received)
        logger.info(
            "[%s] registered – listening on 0x%03X, responding on 0x%03X",
            self.name, self.tx_id, self.rx_id,
        )

    async def start(self) -> None:
        """Launch the device's asyncio processing task and optional cyclic TX."""
        self._task = asyncio.create_task(self._run(), name=f"device-{self.name}")
        if self._cyclic_task is not None:
            await self._cyclic_task.start()

    async def stop(self) -> None:
        """Cancel the device task (and cyclic task) and wait for them to finish."""
        if self._cyclic_task is not None:
            await self._cyclic_task.stop()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def datastore(self) -> DatapointStore:
        """Expose the DatapointStore for external access (e.g. dynamic resolvers)."""
        return self._store

    # ------------------------------------------------------------------
    # Internal: RX callback + processing loop
    # ------------------------------------------------------------------

    def _on_frame_received(self, msg: can.Message) -> None:
        """
        CAN RX callback – called by CANBus from the event loop thread.

        Puts the frame into an asyncio queue for consumption by ``_run()``.
        """
        self._rx_queue.put_nowait(msg)

    async def _run(self) -> None:
        """
        Main processing loop for this device.

        Waits for incoming CAN frames, reassembles ISO-TP messages, invokes
        the protocol handler, and transmits the ISO-TP segmented response.
        """
        logger.debug("[%s] task started", self.name)
        try:
            while True:
                msg = await self._rx_queue.get()
                await self._process_frame(msg)
        except asyncio.CancelledError:
            logger.debug("[%s] task cancelled", self.name)
            raise

    async def _process_frame(self, msg: can.Message) -> None:
        """Process a single incoming CAN frame through ISO-TP and the protocol handler."""
        data = bytes(msg.data)
        payload, fc_frame = self._assembler.feed(data)

        # If the assembler needs us to send a Flow Control frame, do it now.
        if fc_frame is not None:
            logger.debug("[%s] sending FC", self.name)
            await self._bus.send(self.rx_id, fc_frame)

        # If we have a complete payload, hand it to the protocol handler.
        if payload is not None:
            await self._handle_payload(payload)

    async def _handle_payload(self, payload: bytes) -> None:
        """Invoke the protocol handler and transmit the segmented response."""
        logger.debug(
            "[%s] dispatching payload to %s: %s",
            self.name, self._handler.name, payload.hex(" "),
        )
        response = self._handler.handle(payload, self._store)

        if response is None:
            logger.debug("[%s] handler returned no response", self.name)
            return

        frames = segment(response)

        if len(frames) == 1:
            # Single Frame – send immediately.
            await self._bus.send(self.rx_id, frames[0])
        else:
            # Multi-frame: send FF, wait for FC from client, then send CFs.
            await self._bus.send(self.rx_id, frames[0])  # First Frame
            fc = await self._wait_for_flow_control()
            if fc is None:
                logger.warning("[%s] no Flow Control received, aborting TX", self.name)
                return
            for cf in frames[1:]:
                await self._bus.send(self.rx_id, cf)

    async def _wait_for_flow_control(self, timeout: float = 1.0) -> Optional[bytes]:
        """
        Wait for a Flow Control frame from the client after sending a First Frame.

        Returns the raw FC data bytes, or None on timeout.
        """
        try:
            msg = await asyncio.wait_for(self._rx_queue.get(), timeout=timeout)
            data = bytes(msg.data)
            if (data[0] >> 4) == 0x3:
                logger.debug("[%s] received FC: %s", self.name, data.hex(" "))
                return data
            logger.warning(
                "[%s] expected FC frame, got 0x%02X", self.name, data[0]
            )
            return None
        except asyncio.TimeoutError:
            logger.warning("[%s] timeout waiting for Flow Control", self.name)
            return None

    def __repr__(self) -> str:
        return (
            f"SimulatedDevice(name={self.name!r}, "
            f"tx_id=0x{self.tx_id:03X}, "
            f"store={self._store!r})"
        )
