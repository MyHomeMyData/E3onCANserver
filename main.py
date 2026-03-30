"""
main.py – Entry point for the Viessmann E3 CAN-bus simulator.

Usage
-----
    python main.py --devices config/devices.json \\
                   [--interface socketcan] [--channel vcan0] \\
                   [--delay 50] [--errors 5.0] \\
                   [--log-level DEBUG]

The devices JSON file must follow this schema::

    {
      "vcal": {
        "tx": "0x680",
        "dpList": "../data/Open3Edatapoints_680.py",
        "prop": "HPMUMASTER",
        "service77": [ .. ],  // optional, list of dids rejected by standard writeDataByIdentifier protocol
        "delay": 20,          // optional, ms, overrides --delay
        "errors": 10.0,       // optional, %, overrides --errors
        "cyclic": { ... }     // optional
      }
    }

Keys (UDS/request side)
------------------------
tx        : CAN arbitration ID (hex string) on which the client sends requests.
dpList    : Path to the datapoint list file (relative to the devices.json file).
prop      : Device property string (informational).
service77 : A write request targeting any of these DIDs via service 2E returns NRC 0x22 (conditionsNotCorrect)
delay     : Inter-frame delay in ms for this device (0–200). Overrides --delay.
errors    : Error injection rate in % for this device (0–20). Overrides --errors.

Keys (cyclic/broadcast side, optional)
----------------------------------------
cyclic.tx       : CAN-ID on which unsolicited collect messages are broadcast.
cyclic.messages : List of message descriptors (did, schedule, encoder).

Extension notes
---------------
* Additional protocol support: a future ``protocol`` key in the device entry
  can select a different ProtocolHandler class.
* Dynamic data generation: register resolvers on device.datastore after
  load_devices() returns.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from pathlib import Path
from typing import List, Optional

from simulator.bus import CANBus
from simulator.cyclic import CyclicMessage, CyclicTask
from simulator.device import SimulatedDevice
from simulator.faults import DELAY_MAX_MS, ERROR_PCT_MAX, FaultConfig
from simulator.protocol.encoders import Encoder
from simulator.protocol.uds import UDSHandler

pgm_ver_str = 'V0.4.0 (2026-03-30)'


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Simulator of Viessmann E3-series devices on CAN-bus",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=f'E3onCANserver {pgm_ver_str}',
    )
    parser.add_argument(
        "--devices", "-d",
        required=True,
        metavar="FILE",
        help="Path to the devices JSON configuration file",
    )
    parser.add_argument(
        "--interface", "-i",
        default="socketcan",
        metavar="IFACE",
        help="python-can interface type (e.g. socketcan, virtual)",
    )
    parser.add_argument(
        "--channel", "-c",
        default="vcan0",
        metavar="CHAN",
        help="CAN channel / device name",
    )
    parser.add_argument(
        "--delay",
        type=int,
        default=0,
        metavar="MS",
        help=f"Inter-frame delay in ms for all devices (0–{DELAY_MAX_MS}). "
             f"Overridden per device by 'delay' in devices.json.",
    )
    parser.add_argument(
        "--errors",
        type=float,
        default=0.0,
        metavar="PCT",
        help=f"Error injection rate in %% for all devices (0–{ERROR_PCT_MAX}). "
             f"Overridden per device by 'errors' in devices.json.",
    )
    parser.add_argument(
        "--log-level", "-l",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity",
    )
    return parser.parse_args()


def _build_cyclic_task(
    device_name: str,
    cyclic_cfg: dict,
    bus: CANBus,
    store,
) -> Optional[CyclicTask]:
    """Parse the 'cyclic' block and return a CyclicTask, or None."""
    tx_id = int(cyclic_cfg["tx"], 16)
    raw_messages: list = cyclic_cfg.get("messages", [])
    if not raw_messages:
        logging.warning("[%s] cyclic block has no messages, skipping", device_name)
        return None

    messages: List[CyclicMessage] = []
    for entry in raw_messages:
        did: int = int(entry["did"])
        schedule: float = float(entry["schedule"])
        enc_cfg: dict = entry["encoder"]
        fct: str = enc_cfg["fct"]
        args: dict = enc_cfg.get("_args", {})
        encoder: Encoder = Encoder.from_config(fct, args)
        messages.append(CyclicMessage(did=did, schedule=schedule, encoder=encoder))
        logging.debug(
            "[%s] cyclic DID %d every %.1fs via encoder '%s'",
            device_name, did, schedule, fct,
        )

    return CyclicTask(
        device_name=device_name,
        tx_id=tx_id,
        messages=messages,
        bus=bus,
        store=store,
    )


def load_devices(
    devices_file: Path,
    bus: CANBus,
    cli_delay_ms: int,
    cli_error_pct: float,
) -> List[SimulatedDevice]:
    """
    Parse the devices JSON file and instantiate SimulatedDevice objects.

    Parameters
    ----------
    devices_file :
        Path to the JSON file.
    bus :
        Shared CANBus instance.
    cli_delay_ms :
        Global inter-frame delay from --delay (overridden per device).
    cli_error_pct :
        Global error rate from --errors (overridden per device).
    """
    base_dir = devices_file.parent

    with devices_file.open("r", encoding="utf-8") as fh:
        config = json.load(fh)

    devices: List[SimulatedDevice] = []
    for name, entry in config.items():
        tx_id = int(entry["tx"], 16)
        dp_path = base_dir / entry["dpList"]
        dp_val_path = (
            base_dir
            / entry["dpList"]
            .replace("Open3Edatapoints", "virtdata")
            .replace(".py", ".txt")
        )

        fault_config = FaultConfig.from_config(
            entry,
            cli_delay_ms=cli_delay_ms,
            cli_error_pct=cli_error_pct,
        )

        # Service 77 protection list: DIDs that normal WriteDataByIdentifier
        # must reject with NRC 0x22.  Service 77 accepts them regardless.
        s77_raw = entry.get("service77", [])
        service77_dids = frozenset(int(d) for d in s77_raw)
        if service77_dids:
            logging.debug(
                "[%s] Service-77-protected DIDs: %s",
                name, sorted(service77_dids),
            )

        protocol_class = UDSHandler  # extension point

        device = SimulatedDevice(
            name=name,
            tx_id=tx_id,
            dp_list_path=dp_path,
            dp_values_path=dp_val_path,
            bus=bus,
            protocol_class=protocol_class,
            cyclic_task=None,
            fault_config=fault_config,
            service77_dids=service77_dids,
        )

        if "cyclic" in entry:
            try:
                cyclic = _build_cyclic_task(
                    name, entry["cyclic"], bus, device.datastore
                )
                device._cyclic_task = cyclic
            except (KeyError, ValueError) as exc:
                logging.error(
                    "[%s] failed to build cyclic task: %s – skipping cyclic TX",
                    name, exc,
                )

        device.register()
        devices.append(device)
        logging.info(
            "Loaded device %r (tx=0x%03X, delay=%dms, errors=%.1f%%,"
            " cyclic=%s, s77_protected=%d DID(s))",
            name, tx_id,
            fault_config.delay_ms, fault_config.error_pct,
            "yes" if device._cyclic_task else "no",
            len(service77_dids),
        )

    return devices


async def run(args: argparse.Namespace) -> None:
    """Main coroutine: start bus + devices and wait for SIGINT/SIGTERM."""
    devices_file = Path(args.devices).resolve()
    if not devices_file.is_file():
        raise FileNotFoundError(f"Devices file not found: {devices_file}")

    bus = CANBus(interface=args.interface, channel=args.channel)
    devices = load_devices(
        devices_file, bus,
        cli_delay_ms=args.delay,
        cli_error_pct=args.errors,
    )

    await bus.start()
    for device in devices:
        await device.start()

    logging.info(
        "Simulator running – %d device(s) active. Press Ctrl-C to stop.",
        len(devices),
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        logging.info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await stop_event.wait()

    logging.info("Shutting down…")
    for device in devices:
        await device.stop()
    await bus.stop()
    logging.info("Bye.")


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-8s %(name)s – %(message)s",
    )
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
