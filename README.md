![Logo](admin/e3oncan_small.png)
# E3onCANserver

A simulator of Viessmann E3-series devices (Vitocal, Vitodens, VX3/Vitocharge) on CAN-bus.

The simulator listens on a virtual CAN interface (`vcan0`) and responds to
**UDSonCAN** requests from client software such as
[open3e](https://github.com/open3e/open3e).  Multiple devices can be simulated
in parallel, each on its own CAN arbitration ID.

## Status

**v0.3 – added robustness testing (delay and fault injection)**

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
| Inter-frame delay | ✅ |
| Fault injection for robustness testing | ✅ |

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
    "delay": 20,
    "errors": 5.0,
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
| `dpList` | Datapoint list file – path relative to the devices JSON file and to the datapoint values file (`virtdata_xxx.txt`) |
| `prop` | Device property string (informational) |
| `delay` | Inter-frame delay in ms for UDS responses (0–200, optional). Overrides `--delay`. |
| `errors` | Fault injection rate in % for UDS responses (0.0–20.0, optional). Overrides `--errors`. |
| `cyclic` | Specification of unsolicited, cyclically sent messages (optional) |

The simulator responds on `tx + 0x10` (e.g. requests on `0x680` → responses on `0x690`).

Optionally, the specified cyclic messages are sent without an external request. This is used to test the "Collect" mode (ioBroker.e3oncan and E3onCANcollect).

### Datapoint list file (Python)

Not implemented yet.

### Datapoint values file

Corresponding to each datapoint list file a values file must exist in the same folder. It contains the byte-coded values for datapoints.

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
--delay     MS      Inter-frame delay in ms, all devices (0–200, default: 0)
--errors    PCT     Fault injection rate in %, all devices (0–20, default: 0)
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

## Robustness testing

### Inter-frame delay

A configurable delay can be inserted between CAN frames of a UDS response to test client timeout and retry behaviour. The delay applies to all frames of multi-frame ISO-TP responses.

```bash
# 50 ms delay between all frames, for all devices
python main.py --devices config/devices.json --delay 50

# Per-device override in devices.json:
"vcal": { "tx": "0x680", "delay": 100, ... }
```

The delay value is an integer in milliseconds (0–200). If both `--delay` and a per-device `"delay"` key are specified, the per-device value wins.

### Fault injection

A configurable percentage of UDS responses can be deliberately corrupted to test client error handling. The fault type is chosen randomly from the following pool:

| Fault type | Description |
|---|---|
| `WRONG_DID` | DID bytes in the response header replaced with a different value |
| `WRONG_SERVICE` | UDS service byte replaced with `0x00` |
| `SHORT_PAYLOAD` | Last 1–3 bytes of the final frame replaced with padding |
| `WRONG_PADDING` | All `0xCC` padding bytes replaced with `0xAA` |
| `DROP_RANDOM_CF` | One random Consecutive Frame silently dropped (MF only) |
| `DROP_LAST_CF` | Last Consecutive Frame silently dropped (MF only) |
| `WRONG_SEQ` | Sequence nibble of one CF corrupted by +1 (MF only) |
| `DUPLICATE_CF` | One Consecutive Frame sent twice in a row (MF only) |
| `TRUNCATED_MF` | Only the First Frame sent, all CFs dropped (MF only) |
| `WRONG_LEN` | Announced total length in the FF header inflated by 1 (MF only) |

Fault injection applies to UDS request/response exchanges only; cyclic collect messages are never faulted. A rate of 0% (the default) guarantees completely fault-free behaviour.

```bash
# 10% of responses deliberately corrupted, for all devices
python main.py --devices config/devices.json --errors 10

# Combine delay and errors for worst-case testing:
python main.py --devices config/devices.json --delay 50 --errors 10 --log-level DEBUG

# Per-device override in devices.json:
"vcal": { "tx": "0x680", "errors": 5.0, ... }
```

The rate is a decimal percentage (0–20). If both `--errors` and a per-device `"errors"` key are specified, the per-device value wins.

With `--log-level DEBUG`, every injected fault is logged with its type and the affected byte positions, for example:

```
simulator.faults – [vcal] injecting fault WRONG_SERVICE into 5-frame response
simulator.faults – [vcal] WRONG_SERVICE: 0x62 → 0x00 at byte 2
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
│   ├── faults.py           # Delay and fault injection for UDS responses
│   └── protocol/
│       ├── base.py         # Abstract ProtocolHandler base class
│       ├── collect.py      # Segmentation for the Viessmann E3 "collect" protocol
│       ├── encoders.py     # Encoder classes for cyclic (unsolicited) CAN messages
│       ├── isotp.py        # ISO 15765-2 segmentation & reassembly
│       └── uds.py          # UDS services 0x22 / 0x2E
├── config/
│   └── devices.json        # Example device configuration
├── data/
│   ├── virtdata_680.txt    # Example datapoint values for vcal
│   └── virtdata_6a1.txt    # Example datapoint values for vx3
├── docs/
│   └── protocol.md         # Description of Viessmann "Collect" protocol
├── tests/
│   ├── test_collect.py
│   ├── test_datastore.py
│   ├── test_faults.py
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

### Adding a new fault type

Edit `simulator/faults.py`:
1. Add a name to the `FaultType` enum.
2. Implement a `_<name>` method on `FaultInjector`.
3. Register it in `_SF_FAULTS` or `_MF_FAULTS` (or both).

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

### 0.3.0 (2026-03-29)
* (MyHomeMyData) Added inter-frame delay and fault injection for robustness testing

### 0.2.0 (2026-03-28)
* (MyHomeMyData) Added cyclic unsolicited messages

### 0.1.0 (2026-03-27)
* (MyHomeMyData) Initial version. Created using Claude code.
