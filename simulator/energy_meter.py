"""
energy_meter.py – Periodic raw-frame broadcaster for energy meter simulation.

Each EnergyMeterTask sends a single fixed CAN frame at a configured interval.
No protocol framing (ISO-TP, collect) is applied – the bytes are transmitted
as-is, matching the E380 CA and E3100CB broadcast behaviour.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from simulator.bus import CANBus

logger = logging.getLogger(__name__)


class EnergyMeterTask:
    """
    Sends a fixed CAN frame at a fixed interval.

    Parameters
    ----------
    name :
        Human-readable name (for logging).
    tx_id :
        CAN arbitration ID.
    msg :
        Fixed payload bytes transmitted unchanged.
    schedule :
        Transmission interval in seconds.
    bus :
        Shared CANBus instance.
    """

    def __init__(
        self,
        name: str,
        tx_id: int,
        msg: bytes,
        schedule: float,
        bus: CANBus,
    ) -> None:
        self._name = name
        self._tx_id = tx_id
        self._msg = msg
        self._schedule = schedule
        self._bus = bus
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"energy-meter-{self._name}"
        )
        logger.info(
            "[%s] energy meter TX started – CAN-ID 0x%03X every %.1fs",
            self._name, self._tx_id, self._schedule,
        )

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        logger.info("[%s] energy meter TX stopped", self._name)

    async def _run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._schedule)
                await self._bus.send(self._tx_id, self._msg)
                logger.debug(
                    "[%s] TX 0x%03X  %s",
                    self._name, self._tx_id, self._msg.hex(" "),
                )
        except asyncio.CancelledError:
            raise
