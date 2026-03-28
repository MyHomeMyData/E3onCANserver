![Logo](admin/e3oncan_small.png)
# E3onCANserver

A simulator of Viessmann E3-series devices (Vitocal, Vitodens, VX3/Vitocharge) on CAN-bus.

The simulator listens on a virtual CAN interface (`vcan0`) and responds to
**UDSonCAN** requests from client software such as
[open3e](https://github.com/open3e/open3e).  Multiple devices can be simulated
in parallel, each on its own CAN arbitration ID.

## Status

**v0.2 – added cyclic unsolicited messages**

| Feature | Status |
|---|---|
| UDS ReadDataByIdentifier (0x22) | ✅ |
| UDS WriteDataByIdentifier (0x2E) | ✅ |
| ISO-TP Single Frame | ✅ |
| ISO-TP Multi-Frame (FF/CF/FC) | ✅ |
| Multiple parallel devices | ✅ |
| Dynamic value generation | 🔜 planned |
| Cyclic unsolicited TX | ✅ |
| Additional protocols | ✅ |

## Requirements

- Python ≥ 3.10
- Linux with `vcan` kernel module (for real operation)
- [python-can](https://python-can.readthedocs.io/) ≥ 4.3

```bash
pip install -r requirements.txt
```

## Setting up vcan0

```bash
sudo modprobe vcan
sudo ip link add dev vcan0 type vcan
sudo ip link set up vcan0
```

## Configuration

### Devices file (JSON)

Pass the path via `--devices`.  Example (`config/devices.json`):

```json
{
  "vcal": {
    "tx": "0x680",
    "dpList": "../data/Open3Edatapoints_680.py",
    "prop": "HPMUMASTER",
    "cyclic": {
      "tx": "0x693",
      "messages": [
        { "did": 256,
          "schedule": 17,
          "encoder": { "fct": "raw", "_args": { "val": "" } }
        },
        { "did": 506,
          "schedule": 1,
          "encoder": { "fct": "localtime", "_args": { "format": "hhmmss" } }
        },
        { "did": 954,
          "schedule": 13,
          "encoder": { "fct": "raw", "_args": { "val": "" } }
        }
      ]
    }
  },
  "vx3": {
    "tx": "0x6a1",
    "dpList": "../data/Open3Edatapoints_6a1.py",
    "prop": "EMCUMASTER",
    "cyclic": {
      "tx": "0x451",
      "messages": [
        { "did": 506,
          "schedule": 1,
          "encoder": { "fct": "localtime", "_args": { "format": "hhmmss" } }
        }
      ]
    }
  }
}
```

| Key | Description |
|---|---|
| `tx` | CAN ID on which the **client** sends requests (hex string) |
| `dpList` | Datapoint list file – path relative to the devices py file and to datapoint values file (virtdata_xxx.txt) |
| `prop` | Device property string (informational) |
| `cyclic` | Specification of unsolicited, cyclically sent messages (optional) |

The simulator responds on `tx + 0x10` (e.g. requests on `0x680` → responses on `0x690`).

Optionally, the specified cyclic messages are sent without an external request. This is used to test the "Collect" mode (ioBroker.e3oncan and E3onCANcollect).

### Datapoint list file (Python)

Not implemented yet.

### Datapoint values file

Correscponding to each datapoint list file a values file must exist in same folder. It contains the byte coded values for datapoints.

One datapoint per line: decimal DID, space, hex bytes.
Lines starting with `#` are comments.

```
# DID  value (hex bytes)
256 01021f091400fd010109c000020064026500040037343730363238323033333037313238
268 8c01c1007a027e0100
269 80 01 d1 00 58 02 71 01 00

```
Use of delimiter between bytes is optional.

## Usage

```bash
python main.py --devices config/devices.json
```

Options:

```
--devices FILE      Path to the devices JSON file (required)
--interface IFACE   python-can interface (default: socketcan)
--channel   CHAN    CAN channel (default: vcan0)
--log-level LEVEL   DEBUG | INFO | WARNING | ERROR (default: INFO)
```

### Testing with open3e

With `vcan0` up and the simulator running:

```bash
# Read datapoint 256 from device vcal
open3e --can vcan0 -v -r 256
```

### Monitoring the bus

```bash
candump vcan0
```

## Running the tests

```bash
pytest tests/ -v
```

## Project structure

```
E3onCANserver/
├── simulator/
│   ├── bus.py              # Async python-can wrapper (shared bus, RX dispatch)
│   ├── cyclic.py           # Unsolicited broadcast scheduler for one device
│   ├── datastore.py        # Per-device datapoint storage (dict + resolver API)
│   ├── device.py           # SimulatedDevice: asyncio task, ISO-TP ↔ UDS glue
│   └── protocol/
│       ├── base.py         # Abstract ProtocolHandler base class
│       ├── collect.py      # Segmentation for the Viessmann E3 "collect" protocol
│       ├── encoders.py     # Encoder classes for cyclic (unsolicited) CAN messages
│       ├── isotp.py        # ISO 15765-2 segmentation & reassembly
│       └── uds.py          # UDS services 0x22 / 0x2E
├── config/
│   └── devices.json        # Example device configuration
├── data/
│   └── Open3Edatapoints_680.txt   # Example datapoint values
├── docs/
│   └── protocol.md         # Description of Viessmann specific protocol used as "Collect" by clients
├── tests/
│   ├── test_collect.py
│   ├── test_datastore.py
│   ├── test_isotp.py
│   └── test_uds.py
├── main.py
├── requirements.txt
└── pyproject.toml
```

## CAN-ID address space

The simulator uses the address range `0x680`–`0x6EF` for client requests.
Responses are sent on `request_id + 0x10`:

| Device | Request ID | Response ID | Unsolicited ID |
|---|---|---|---|
| vcal (HPMUMASTER) | 0x680 | 0x690 | 0x693 |
| vx3  (EMCUMASTER) | 0x6A1 | 0x6B1 | 0x451 |

## Extending the simulator

### Adding a new UDS service

Edit `simulator/protocol/uds.py`:
1. Add a `_handle_<service>` method.
2. Register it in the `_HANDLERS` dict at the bottom of the class.

### Adding a new protocol

1. Create `simulator/protocol/myproto.py`, sub-class `ProtocolHandler`.
2. In `main.py`, read a `"protocol"` key from the device config entry and
   pass the corresponding class to `SimulatedDevice`.

### Dynamic datapoint values

```python
from simulator.datastore import DatapointStore
import struct, time

store: DatapointStore  # obtained from device.datastore

# Register a resolver that returns the current Unix time as uint32
store.register_resolver(0x0200, lambda: struct.pack(">I", int(time.time())))
```

## Changelog
<!--
    Placeholder for the next version (at the beginning of the line):
    ### **WORK IN PROGRESS**
-->

### 0.2.0 (2026-03-28)
* (MyHomeMyData) Added cyclic unsolicited messages

### 0.1.0 (2026-03-27)
* (MyHomeMyData) Initial version. Created using Claude code.
