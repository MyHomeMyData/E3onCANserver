"""
device.py – SimulatedDevice: one CAN device running in its own asyncio task.

Each SimulatedDevice runs three independent workers:

1. UDS worker – listens on ``tx_id``, responds on ``tx_id + 0x10``.
   Handles ReadDataByIdentifier (0x22) and WriteDataByIdentifier (0x2E).
   Writes to DIDs in the Service 77 protection list are rejected with
   NRC 0x22 (conditionsNotCorrect).

2. Service 77 worker – listens on ``tx_id + 0x02``, responds on
   ``tx_id + 0x12``.  Always active.  Handles proprietary Viessmann
   Service 77 write requests, including protected DIDs.

3. Cyclic worker (optional) – sends unsolicited collect-protocol messages
   at configured intervals on a separate CAN-ID.

All workers share the same DatapointStore, so a write via Service 77 is
immediately visible to UDS reads and vice versa.

Fault injection (delay, error rate) applies to UDS and Service 77 responses.
Cyclic messages are never faulted.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import FrozenSet, Optional, Type

import can

from simulator.bus import CANBus
from simulator.cyclic import CyclicTask
from simulator.datastore import DatapointStore
from simulator.faults import FaultConfig, FaultInjector
from simulator.protocol.base import ProtocolHandler
from simulator.protocol.isotp import ISOTPAssembler, segment
from simulator.protocol.service77 import (
    S77_REQUEST_ID_OFFSET,
    S77_RESPONSE_ID_OFFSET,
    Service77Handler,
)
from simulator.protocol.uds import UDSHandler

logger = logging.getLogger(__name__)

UDS_RESPONSE_ID_OFFSET = 0x10


class SimulatedDevice:
    """
    Represents one simulated Viessmann E3 device on the CAN bus.

    Parameters
    ----------
    name :
        Human-readable device name (e.g. "vcal").
    tx_id :
        CAN arbitration ID for UDS requests (client → device).
    dp_list_path :
        Path to the datapoint list file (informational, not yet parsed).
    dp_values_path :
        Path to the virtdata_xxx.txt values file.
    bus :
        Shared CANBus instance.
    protocol_class :
        UDS protocol handler class. Defaults to UDSHandler.
    cyclic_task :
        Optional CyclicTask for unsolicited broadcast messages.
    fault_config :
        Delay and error-injection settings.
    service77_dids :
        Set of DID integers protected against normal WriteDataByIdentifier.
        Writes to these DIDs via UDS return NRC 0x22; Service 77 always
        accepts them.  Empty set (default) means no protection.
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
        fault_config: Optional[FaultConfig] = None,
        service77_dids: Optional[FrozenSet[int]] = None,
    ) -> None:
        self.name    = name
        self.tx_id   = tx_id
        self.rx_id   = tx_id + UDS_RESPONSE_ID_OFFSET

        # Service 77 CAN-IDs
        self.s77_tx_id = tx_id + S77_REQUEST_ID_OFFSET   # we *listen* here
        self.s77_rx_id = tx_id + S77_RESPONSE_ID_OFFSET  # we *respond* here

        self._bus   = bus
        self._store = DatapointStore.from_file(dp_values_path)

        # UDS handler receives the protection list so it can enforce NRC 0x22.
        s77_set = service77_dids or frozenset()
        self._uds_handler  = UDSHandler(service77_dids=s77_set)
        self._s77_handler  = Service77Handler()

        # Each worker has its own ISO-TP assembler (independent state).
        self._uds_assembler = ISOTPAssembler()
        self._s77_assembler = ISOTPAssembler()

        self._uds_queue: asyncio.Queue[can.Message] = asyncio.Queue()
        self._s77_queue: asyncio.Queue[can.Message] = asyncio.Queue()

        self._uds_task: Optional[asyncio.Task] = None
        self._s77_task: Optional[asyncio.Task] = None
        self._cyclic_task: Optional[CyclicTask] = cyclic_task

        self._fault_config = fault_config or FaultConfig()
        self._injector     = FaultInjector(self._fault_config, name)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def register(self) -> None:
        """Register both CAN RX callbacks with the shared bus."""
        self._bus.register_rx(self.tx_id,    self._on_uds_frame)
        self._bus.register_rx(self.s77_tx_id, self._on_s77_frame)
        logger.info(
            "[%s] registered – UDS 0x%03X→0x%03X | S77 0x%03X→0x%03X"
            " (delay=%dms, errors=%.1f%%)",
            self.name,
            self.tx_id, self.rx_id,
            self.s77_tx_id, self.s77_rx_id,
            self._fault_config.delay_ms, self._fault_config.error_pct,
        )

    async def start(self) -> None:
        """Launch all worker tasks."""
        self._uds_task = asyncio.create_task(
            self._run(self._uds_queue, self._uds_assembler,
                      self._uds_handler, self.rx_id, "UDS"),
            name=f"device-{self.name}-uds",
        )
        self._s77_task = asyncio.create_task(
            self._run(self._s77_queue, self._s77_assembler,
                      self._s77_handler, self.s77_rx_id, "S77"),
            name=f"device-{self.name}-s77",
        )
        if self._cyclic_task is not None:
            await self._cyclic_task.start()

    async def stop(self) -> None:
        """Cancel all worker tasks."""
        if self._cyclic_task is not None:
            await self._cyclic_task.stop()
        for task in (self._uds_task, self._s77_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @property
    def datastore(self) -> DatapointStore:
        return self._store

    # ------------------------------------------------------------------
    # RX callbacks – post frames into the appropriate queue
    # ------------------------------------------------------------------

    def _on_uds_frame(self, msg: can.Message) -> None:
        self._uds_queue.put_nowait(msg)

    def _on_s77_frame(self, msg: can.Message) -> None:
        self._s77_queue.put_nowait(msg)

    # ------------------------------------------------------------------
    # Generic worker – shared by UDS and Service 77
    # ------------------------------------------------------------------

    async def _run(
        self,
        queue: asyncio.Queue,
        assembler: ISOTPAssembler,
        handler: ProtocolHandler,
        response_can_id: int,
        label: str,
    ) -> None:
        """
        Generic ISO-TP receive / dispatch / respond loop.

        Parameters
        ----------
        queue :
            Frame queue fed by the corresponding RX callback.
        assembler :
            ISO-TP assembler dedicated to this worker.
        handler :
            Protocol handler to invoke on complete payloads.
        response_can_id :
            CAN-ID used for outgoing frames.
        label :
            Short label for log messages ("UDS" or "S77").
        """
        logger.debug("[%s/%s] task started", self.name, label)
        try:
            while True:
                msg = await queue.get()
                await self._process_frame(
                    msg, assembler, handler, response_can_id, label
                )
        except asyncio.CancelledError:
            logger.debug("[%s/%s] task cancelled", self.name, label)
            raise

    async def _process_frame(
        self,
        msg: can.Message,
        assembler: ISOTPAssembler,
        handler: ProtocolHandler,
        response_can_id: int,
        label: str,
    ) -> None:
        data = bytes(msg.data)
        payload, fc_frame = assembler.feed(data)

        if fc_frame is not None:
            logger.debug("[%s/%s] sending FC", self.name, label)
            await self._bus.send(response_can_id, fc_frame)

        if payload is not None:
            await self._handle_payload(
                payload, handler, response_can_id, label
            )

    async def _handle_payload(
        self,
        payload: bytes,
        handler: ProtocolHandler,
        response_can_id: int,
        label: str,
    ) -> None:
        logger.debug(
            "[%s/%s] dispatching: %s", self.name, label, payload.hex(" ")
        )
        response = handler.handle(payload, self._store)

        if response is None:
            logger.debug("[%s/%s] handler returned no response", self.name, label)
            return

        frames = segment(response)
        await self._injector.send_frames(
            frames,
            send_fn=lambda f: self._bus.send(response_can_id, f),
            wait_for_fc=(
                self._make_fc_waiter(
                    self._uds_queue if label == "UDS" else self._s77_queue
                )
                if len(frames) > 1 else None
            ),
        )

    def _make_fc_waiter(self, queue: asyncio.Queue):
        """Return a coroutine factory that waits for FC on the given queue."""
        async def _wait(timeout: float = 1.0) -> Optional[bytes]:
            try:
                msg  = await asyncio.wait_for(queue.get(), timeout=timeout)
                data = bytes(msg.data)
                if (data[0] >> 4) == 0x3:
                    return data
                logger.warning(
                    "[%s] expected FC frame, got 0x%02X", self.name, data[0]
                )
                return None
            except asyncio.TimeoutError:
                logger.warning("[%s] timeout waiting for Flow Control", self.name)
                return None
        return _wait

    def __repr__(self) -> str:
        return (
            f"SimulatedDevice(name={self.name!r}, "
            f"tx_id=0x{self.tx_id:03X}, "
            f"s77_tx_id=0x{self.s77_tx_id:03X}, "
            f"store={self._store!r})"
        )
