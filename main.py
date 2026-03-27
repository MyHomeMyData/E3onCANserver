"""
main.py – Entry point for the Viessmann E3 CAN-bus simulator.

Usage
-----
    python main.py --devices config/devices.json [--interface socketcan] [--channel vcan0] [--log-level DEBUG]

The devices JSON file must follow this schema::

    {
      "vcal": {
        "tx": "0x680",
        "dpList": "data/Open3Edatapoints_680.py",
        "prop": "HPMUMASTER"
      },
      "vx3": {
        "tx": "0x6a1",
        "dpList": "data/Open3Edatapoints_6a1.py",
        "prop": "EMCUMASTER"
      }
    }

Keys
----
tx      : CAN arbitration ID (hex string) on which the client sends requests.
dpList  : Path to the datapoint list file (relative to the devices.json file).
          Implicit rule: For each datapoint list file a file named "virtdata_xxx.txt"
                         is available containing value of the data points.
                         xxx = hex address of device
prop    : Device property string (informational, not used by the simulator yet).

Extension notes
---------------
* Additional protocol support: a future ``protocol`` key in the device entry
  can select a different ProtocolHandler class.
* Dynamic data generation: a future ``dynamic`` key can activate per-DID
  resolvers defined in a separate config section.
* Cyclic TX: a future ``cyclic`` key can enable unsolicited broadcast tasks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
from pathlib import Path

from simulator.bus import CANBus
from simulator.device import SimulatedDevice
from simulator.protocol.uds import UDSHandler

pgm_ver_str = 'V0.1.0 (2026-03-27)'

def parse_args() -> argparse.Namespace:
    help_version_string = pgm_ver_str
    parser = argparse.ArgumentParser(
        description="Simulator of Viessmann E3-series devices on CAN-bus",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=f'E3onCANserver {help_version_string}',
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


def load_devices(devices_file: Path, bus: CANBus) -> list[SimulatedDevice]:
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

    devices: list[SimulatedDevice] = []
    for name, entry in config.items():
        tx_id = int(entry["tx"], 16)
        dp_path = base_dir / entry["dpList"]
        dp_val_path = base_dir / entry["dpList"].replace('Open3Edatapoints','virtdata').replace('.py','.txt')

        # Extension point: read ``entry.get("protocol", "uds")`` here and
        # select the appropriate ProtocolHandler class.
        protocol_class = UDSHandler

        device = SimulatedDevice(
            name=name,
            tx_id=tx_id,
            dp_list_path=dp_path,
            dp_values_path=dp_val_path,
            bus=bus,
            protocol_class=protocol_class,
        )
        device.register()
        devices.append(device)
        logging.info("Loaded device %r (tx=0x%03X, dpList=%s)", name, tx_id, dp_path)

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

    logging.info("Simulator running – %d device(s) active. Press Ctrl-C to stop.", len(devices))

    # Wait until cancelled by SIGINT or SIGTERM.
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
