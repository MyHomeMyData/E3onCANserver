"""
bus.py – Asynchronous CAN bus wrapper built on python-can.

This module owns the single ``can.Bus`` instance shared by all simulated
devices.  It provides:

* A thread-safe transmit queue (``send()``) used by device tasks.
* A fan-out receive mechanism: each device registers a callback for its own
  CAN arbitration ID; the reader loop dispatches incoming frames accordingly.
* Clean start/stop lifecycle managed by ``CANBus.start()`` and
  ``CANBus.stop()``.

Design notes
------------
python-can's ``Bus.recv()`` is blocking.  To integrate it with asyncio we run
the receive loop in a dedicated executor thread and post frames into the event
loop via ``loop.call_soon_threadsafe()``.

Extension points (not yet implemented)
---------------------------------------
* Promiscuous mode: register a wildcard callback to capture all frames
  (useful for the planned ``tools/monitor.py``).
* TX scheduling: a priority queue could replace the simple asyncio.Queue to
  support the planned cyclic-send extension.
* Interface abstraction: the ``interface`` / ``channel`` parameters already
  allow switching from ``vcan`` to a real USB-CAN adapter without code changes.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Callable, Dict, Optional

import can

logger = logging.getLogger(__name__)

# Type alias: a receive callback takes a can.Message and returns nothing.
RxCallback = Callable[[can.Message], None]


class CANBus:
    """
    Shared CAN bus handle with async TX queue and per-ID RX dispatch.

    Parameters
    ----------
    interface :
        python-can interface name (default ``"virtual"`` for unit tests;
        use ``"socketcan"`` for vcan0 on Linux).
    channel :
        CAN channel / device name (default ``"vcan0"``).
    bitrate :
        Bus bitrate in bits/s.  Ignored for virtual and socketcan interfaces
        that derive the rate from the OS configuration.
    """

    def __init__(
        self,
        interface: str = "socketcan",
        channel: str = "vcan0",
        bitrate: int = 500_000,
    ) -> None:
        self._interface = interface
        self._channel = channel
        self._bitrate = bitrate

        self._bus: Optional[can.Bus] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._tx_queue: asyncio.Queue[can.Message] = asyncio.Queue()
        self._rx_callbacks: Dict[int, RxCallback] = {}

        self._rx_thread: Optional[threading.Thread] = None
        self._tx_task: Optional[asyncio.Task] = None
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Open the CAN interface and start the RX/TX background workers."""
        self._loop = asyncio.get_running_loop()
        logger.info(
            "Opening CAN bus: interface=%s channel=%s",
            self._interface, self._channel,
        )
        self._bus = can.Bus(
            interface=self._interface,
            channel=self._channel,
            bitrate=self._bitrate,
        )
        self._running = True
        self._rx_thread = threading.Thread(
            target=self._rx_loop, name="can-rx", daemon=True
        )
        self._rx_thread.start()
        self._tx_task = asyncio.create_task(self._tx_loop(), name="can-tx")
        logger.info("CAN bus started")

    async def stop(self) -> None:
        """Shut down workers and close the CAN interface."""
        self._running = False
        if self._tx_task:
            self._tx_task.cancel()
            try:
                await self._tx_task
            except asyncio.CancelledError:
                pass
        if self._bus:
            self._bus.shutdown()
            self._bus = None
        logger.info("CAN bus stopped")

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_rx(self, arb_id: int, callback: RxCallback) -> None:
        """
        Register *callback* to be called for every frame with *arb_id*.

        Must be called before ``start()``.  Registering the same ID twice
        overwrites the previous callback.
        """
        self._rx_callbacks[arb_id] = callback
        logger.debug("Registered RX callback for CAN ID 0x%03X", arb_id)

    # ------------------------------------------------------------------
    # TX
    # ------------------------------------------------------------------

    async def send(self, arb_id: int, data: bytes) -> None:
        """
        Enqueue a CAN frame for transmission.

        Parameters
        ----------
        arb_id :
            CAN arbitration (message) ID.
        data :
            Up to 8 bytes of frame payload.
        """
        msg = can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)
        await self._tx_queue.put(msg)

    async def _tx_loop(self) -> None:
        """Drain the TX queue and write frames to the bus."""
        while True:
            msg = await self._tx_queue.get()
            try:
                self._bus.send(msg)
                logger.debug(
                    "TX  0x%03X  %s",
                    msg.arbitration_id,
                    bytes(msg.data).hex(" "),
                )
            except can.CanError as exc:
                logger.error("CAN TX error: %s", exc)

    # ------------------------------------------------------------------
    # RX (runs in a separate OS thread)
    # ------------------------------------------------------------------

    def _rx_loop(self) -> None:
        """
        Blocking receive loop executed in a daemon thread.

        Incoming frames are dispatched to the registered asyncio callbacks via
        ``loop.call_soon_threadsafe`` so they run safely in the event loop.
        """
        while self._running and self._bus:
            try:
                msg = self._bus.recv(timeout=0.1)
            except can.CanError as exc:
                if self._running:
                    logger.error("CAN RX error: %s", exc)
                continue

            if msg is None:
                continue  # recv() timed out – check _running and retry

            logger.debug(
                "RX  0x%03X  %s",
                msg.arbitration_id,
                bytes(msg.data).hex(" "),
            )

            cb = self._rx_callbacks.get(msg.arbitration_id)
            if cb is not None:
                self._loop.call_soon_threadsafe(cb, msg)
            # Extension point: invoke promiscuous wildcard callback here.
