![Logo](admin/e3oncan_small.png)
# E3onCANserver

A simulator of Viessmann E3-series devices (Vitocal, Vitodens, VX3/Vitocharge) on CAN-bus and via DoIP.

The simulator listens on a virtual CAN interface (`vcan0`) and responds to
**UDSonCAN** requests from client software such as
[open3e](https://github.com/open3e/open3e) or [ioBroker.e3oncan](https://github.com/MyHomeMyData/ioBroker.e3oncan).  Multiple devices can be simulated
in parallel, each on its own CAN arbitration ID.

Alternatively, the DoIP (Diagnostics over IP, ISO 13400) protocol can be used over TCP/IP. Communication to only one device per connection. The CAN bus is then deactivated.

## Status

**v0.5 – added DoIP (Diagnostics over IP) support**

| Feature | Status |
|---|---|
| UDS ReadDataByIdentifier (0x22) | ✅ |
| UDS WriteDataByIdentifier (0x2E) | ✅ |
| ISO-TP Single Frame | ✅ |
| ISO-TP Multi-Frame (FF/CF/FC) | ✅ |
| Multiple parallel devices | ✅ |
| Dynamic value generation | 🔜 planned |
| Cyclic unsolicited TX (Collect protocol) | ✅ |
| Inter-frame delay | ✅ |
| Fault injection for robustness testing | ✅ |
| Viessmann Service 77 write protocol | ✅ |
| DoIP (ISO 13400) server | ✅ |

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

Pass the path via `--devices`.  Full example (`config/devices.json`):

```json
{
  "vcal": {
    "tx": "0x680",
    "dpList": "../data/Open3Edatapoints_680.py",
    "prop": "HPMUMASTER",
    "delay": 20,
    "errors": 5.0,
    "service77": [1100, 1101],
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
| `service77` | List of DID integers protected against normal WriteDataByIdentifier (optional). These DIDs can only be written via Service 77. |
| `cyclic` | Specification of unsolicited, cyclically sent messages (optional) |

The simulator responds on `tx + 0x10` (e.g. requests on `0x680` → responses on `0x690`).

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
--doip      [HOST:]PORT  Run in DoIP mode (e.g. 13400 or 0.0.0.0:13400)
```

### Testing with open3e

With `vcan0` up and the simulator running:

```bash
# Read datapoint 256 from device 0x680
open3e --can vcan0 -v -r 256
```

Simulator running with `--doip`:

```bash
# Read datapoint 256 from device 0x680
open3e --doip 127.0.0.1 -v -r 256
```

### Monitoring the CAN bus

```bash
candump vcan0
```

## Protocols

### UDS (ISO 14229) – ReadDataByIdentifier and WriteDataByIdentifier

The main protocol for client/server communication. The simulator responds on `tx + 0x10`:

| Device | Request ID | Response ID |
|---|---|---|
| vcal (HPMUMASTER) | 0x680 | 0x690 |
| vx3 (EMCUMASTER) | 0x6A1 | 0x6B1 |

Supported services: `0x22` ReadDataByIdentifier, `0x2E` WriteDataByIdentifier.

### Viessmann Service 77

A proprietary Viessmann write protocol, discovered via reverse engineering. It operates in parallel with UDS on a dedicated CAN-ID pair and allows writing of data points that are protected against normal WriteDataByIdentifier.

**Background:** Viessmann protects certain data points from accidental writes. A UDS write to a protected DID returns NRC `0x22` (conditionsNotCorrect). Service 77 bypasses this protection and is accepted by real devices for the same DIDs.

**CAN-ID mapping** (derived automatically from the device `tx` address):

| Device | Service 77 Request ID | Service 77 Response ID |
|---|---|---|
| vcal (tx=0x680) | 0x682 | 0x692 |
| vx3 (tx=0x6A1) | 0x6A3 | 0x6B3 |

The offset is always `+0x02` for requests and `+0x12` for responses.

**Frame format:**

```
Request:           [0x77] [DID_HIGH] [DID_LOW] [DATA ...]
Positive response: [0x77] [0x04]     [DID_HIGH] [DID_LOW]
Negative response: [0x7F] [0x77]     [NRC]
```

**Protection list (`service77` key in devices.json):**

```json
"vcal": {
  "tx": "0x680",
  "service77": [256, 1100, 1101],
  ...
}
```

DIDs listed under `service77` are protected: a normal WriteDataByIdentifier (0x2E) on any of these DIDs returns NRC `0x22`. Service 77 accepts writes to all known DIDs, including protected ones.

Service 77 is always active for every device – no additional configuration is needed beyond the optional protection list. Both UDS and Service 77 share the same datapoint store, so a value written via Service 77 is immediately visible in UDS reads.

### Collect (cyclic unsolicited TX)

The Viessmann-proprietary broadcast protocol used by E3 devices to push datapoint values to listening clients at fixed intervals. Operates on a separate CAN-ID independent of UDS and Service 77. See `docs/protocol.md` for the full frame format specification.

Configuration is via the `cyclic` block in devices.json. Two encoder types are available:

| `fct` | Description |
|---|---|
| `raw` | Sends the value stored for the DID, or an optional fixed hex string |
| `localtime` | Sends the current local time as 3 bytes `[HH, MM, SS]` |

## DoIP mode

DoIP (Diagnostics over IP, ISO 13400) allows UDS clients to communicate with
the simulator over TCP instead of CAN.  This is useful for testing
[open3e](https://github.com/open3e/open3e) in DoIP mode without physical
Viessmann hardware.

### Starting in DoIP mode

```bash
# Listen on localhost, standard DoIP port
python main.py --devices config/devices.json --doip

# Listen on all interfaces
python main.py --devices config/devices.json --doip 0.0.0.0
```

When `--doip` is set, `--interface` and `--channel` are ignored – no CAN bus
is opened.  Cyclic (Collect) messages are not sent in DoIP mode.

### Connecting with open3e

```bash
# Read datapoint 256 from the main device (ECU address 0x680)
open3e --doip 127.0.0.1 -tx 0x680 -v -r 256
```

The ECU address (`-tx`) must match the `tx` value in `devices.json`.

### What is supported in DoIP mode

| Feature | DoIP mode |
|---|---|
| ReadDataByIdentifier (0x22) | ✅ |
| WriteDataByIdentifier (0x2E) | ✅ |
| Service 77 protection list (NRC 0x22) | ✅ |
| Fault injection (`--errors`) | ✅ |
| Inter-frame delay (`--delay`) | ✅ |
| Service 77 write protocol | ❌ (CAN-only) |
| Cyclic unsolicited TX (Collect) | ❌ (CAN-only) |

### DoIP protocol details

The simulator implements the minimal DoIP subset required by `doipclient`
(the library used by open3e):

| Payload type | Description |
|---|---|
| `0x0005` | Routing Activation Request (client → server) |
| `0x0006` | Routing Activation Response (server → client) |
| `0x8001` | Diagnostic Message (UDS payload, both directions) |
| `0x8002` | Diagnostic Message Positive ACK (server → client) |

The standard DoIP port is 13400.  The target address in the client's request
is the device's `tx` value from `devices.json` (e.g. `0x0680`).


## Robustness testing

### Inter-frame delay

A configurable delay inserted between CAN frames of a UDS or Service 77 response. Tests client timeout and retry behaviour.

```bash
# 50 ms delay between all frames, for all devices
python main.py --devices config/devices.json --delay 50
```

Per-device override: `"delay": 100` in the device entry.

### Fault injection

A configurable percentage of UDS responses can be deliberately corrupted to test client error handling. Fault type is chosen randomly.

```bash
# 10% of responses corrupted, combined with delay
python main.py --devices config/devices.json --delay 50 --errors 10 --log-level DEBUG
```

Per-device override: `"errors": 5.0` in the device entry. A value of 0% (the default) guarantees completely fault-free behaviour.

Available fault types:

| Fault type | Applies to | Description |
|---|---|---|
| `WRONG_DID` | SF + MF | DID bytes in response header replaced with a different value |
| `WRONG_SERVICE` | SF + MF | UDS service byte replaced with `0x00` |
| `SHORT_PAYLOAD` | SF + MF | Last 1–3 bytes of the final frame replaced with padding |
| `WRONG_PADDING` | SF + MF | All `0xCC` padding bytes replaced with `0xAA` |
| `DROP_RANDOM_CF` | MF only | One random Consecutive Frame silently dropped |
| `DROP_LAST_CF` | MF only | Last Consecutive Frame silently dropped |
| `WRONG_SEQ` | MF only | Sequence nibble of one CF corrupted by +1 |
| `DUPLICATE_CF` | MF only | One Consecutive Frame sent twice in a row |
| `TRUNCATED_MF` | MF only | Only the First Frame sent, all CFs dropped |
| `WRONG_LEN` | MF only | Announced total length in the FF header inflated by 1 |

Fault injection applies to UDS and Service 77 responses only. Cyclic collect messages are never faulted.

With `--log-level DEBUG`, every injected fault is logged with its type and the affected bytes.

## CAN-ID address space

| Device | UDS/DoIP Addr | UDS Response | S77 Request | S77 Response | Collect (unsolicited) |
|---|---|---|---|---|---|
| vcal (HPMUMASTER) | 0x680 | 0x690 | 0x682 | 0x692 | 0x693 |
| vx3 (EMCUMASTER) | 0x6A1 | 0x6B1 | 0x6A3 | 0x6B3 | 0x451 |

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
│   ├── device.py           # SimulatedDevice: UDS + Service 77 + cyclic workers
│   ├── faults.py           # Delay and fault injection for UDS / Service 77 responses
│   └── protocol/
│       ├── base.py         # Abstract ProtocolHandler base class
│       ├── collect.py      # Segmentation for the Viessmann E3 "collect" protocol
│       ├── encoders.py     # Encoder classes for cyclic (unsolicited) CAN messages
│       ├── isotp.py        # ISO 15765-2 segmentation & reassembly
│       ├── service77.py    # Viessmann proprietary Service 77 write protocol
│       └── uds.py          # UDS services 0x22 / 0x2E (with Service 77 protection)
├── config/
│   └── devices.json        # Example device configuration
├── data/
│   ├── virtdata_680.txt    # Example datapoint values for vcal
│   └── virtdata_6a1.txt    # Example datapoint values for vx3
├── docs/
│   └── protocol.md         # Viessmann "Collect" protocol frame format
├── tests/
│   ├── test_collect.py
│   ├── test_datastore.py
│   ├── test_faults.py
│   ├── test_isotp.py
│   ├── test_service77.py
│   └── test_uds.py
├── main.py
├── requirements.txt
└── pyproject.toml
```

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

### 0.5.0 (2026-03-31)
* (MyHomeMyData) Added DoIP (ISO 13400) server for testing open3e in DoIP mode

### 0.4.0 (2026-03-30)
* (MyHomeMyData) Added Viessmann Service 77 proprietary write protocol
* (MyHomeMyData) Added Service 77 protection list for WriteDataByIdentifier (NRC 0x22)

### 0.3.0 (2026-03-29)
* (MyHomeMyData) Added inter-frame delay and fault injection for robustness testing

### 0.2.0 (2026-03-28)
* (MyHomeMyData) Added cyclic unsolicited messages

### 0.1.0 (2026-03-27)
* (MyHomeMyData) Initial version. Created using Claude code.
