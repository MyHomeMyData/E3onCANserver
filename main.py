"""
main.py – Entry point for the Viessmann E3 CAN-bus simulator.

Usage
-----
    python main.py --devices config/devices.json [--interface socketcan] [--channel vcan0] [--log-level DEBUG]

The devices JSON file must follow this schema::

    {
      "vcal": {
        "tx": "0x680",
        "dpList": "../data/Open3Edatapoints_680.py",
        "prop": "HPMUMASTER",
        "cyclic": {
          "tx": "0x693",
          "messages": [
            { "did": 256,
              "schedule": 15,
              "encoder": { "fct": "raw", "_args": { "val": "" } }
            },
            { "did": 506,
              "schedule": 1,
              "encoder": { "fct": "localtime", "_args": { "format": "hhmmss" } }
            }
          ]
        }
      }
    }

Keys (UDS/request side)
------------------------
tx      : CAN arbitration ID (hex string) on which the client sends requests.
dpList  : Path to the datapoint list file (relative to the devices.json file).
          Implicit rule: the values file is named virtdata_<hex_addr>.txt in
          the same directory.
prop    : Device property string (informational).

Keys (cyclic/broadcast side, optional)
----------------------------------------
cyclic.tx       : CAN-ID on which unsolicited collect messages are broadcast.
cyclic.messages : List of message descriptors:
    did      : Data identifier (decimal integer).
    schedule : Broadcast interval in seconds.
    encoder  : { "fct": "<name>", "_args": { ... } }
               Supported fct values: "raw", "localtime".

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
from simulator.protocol.encoders import Encoder
from simulator.protocol.uds import UDSHandler

pgm_ver_str = 'V0.2.0 (2026-03-28)'


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
    store,               # DatapointStore – imported lazily to avoid circular
) -> Optional[CyclicTask]:
    """
    Parse the ``"cyclic"`` block of a device config entry and return a
    CyclicTask, or None if the block is absent or has no messages.

    Parameters
    ----------
    device_name :
        Used in log messages.
    cyclic_cfg :
        The dict from ``entry["cyclic"]``.
    bus :
        Shared CANBus.
    store :
        DatapointStore of the device (passed to encoders at runtime).
    """
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


def load_devices(devices_file: Path, bus: CANBus) -> List[SimulatedDevice]:
    """
    Parse the devices JSON file and instantiate SimulatedDevice objects.

    Parameters
    ----------
    devices_file :
        Path to the JSON file.
    bus :
        Shared CANBus instance (passed to each device).

    Returns
    -------
    list[SimulatedDevice]
        One device per entry in the JSON file.
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

        # Extension point: read entry.get("protocol", "uds") here and
        # select the appropriate ProtocolHandler class.
        protocol_class = UDSHandler

        # Build device first (creates the DatapointStore).
        device = SimulatedDevice(
            name=name,
            tx_id=tx_id,
            dp_list_path=dp_path,
            dp_values_path=dp_val_path,
            bus=bus,
            protocol_class=protocol_class,
            cyclic_task=None,  # attached below after store is ready
        )

        # Attach cyclic task if configured.
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
            "Loaded device %r (tx=0x%03X, dpList=%s, cyclic=%s)",
            name, tx_id, dp_path,
            "yes" if device._cyclic_task else "no",
        )

    return devices


async def run(args: argparse.Namespace) -> None:
    """Main coroutine: start bus + devices and wait for SIGINT/SIGTERM."""
    devices_file = Path(args.devices).resolve()
    if not devices_file.is_file():
        raise FileNotFoundError(f"Devices file not found: {devices_file}")

    bus = CANBus(interface=args.interface, channel=args.channel)
    devices = load_devices(devices_file, bus)

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
