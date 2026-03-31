# Description of used protocols

## Unsolicited sequences (mode "collect" of clients)

### Description of protocol

Protocol is similar to a servers answer on a ReadDataByIdentifier request, but is sent unsolicited in a fixed schedule, e.g. every 10 seconds.

Handling of length of payload is different from ReadDataByIdentifier protocol (all data in hex):
* Payload here means the value of the datapoint, e.g. "cf 01" for a 2-byte sensor value
* Each frame has 8 bytes: v0 v1 v2 v3 v4 v5 v6 v7
* v0 of first frame is 21
* v0 of following frames wraps in range 20 .. 2F
* first frame contains DID, length and payload:
    + v1 v2 is DID low- and high-byte
    + v3 is lenght code:
        * if v3 is in range B1 .. B4: Single frame, length of payload is v3-B0, payload starts @v4
        * if v3 is in range B5 .. BF: Multi frame, length of payload is v3-B0, payload starts @v4
        * if v3 equals B0:
            + if v4 equals C1: Multi frame, length of payload is v5, payload starts @v6; this behaviour is observed for lenght of B5 only, yet
            + if v4 not equals C1: Multi frame, length of payload is v4, payload starts @v5
* last frame is padded to a length of 8 bytes

From servers point of view:
* Create frames similar to protocol ReadDataByIdentifier
* length of payload is stored to v3 if it is in range 1 .. 15 with an adder of B0. Payload starts at v4. Empty payload is not supported.
* length of payload is stored to v3, v4 and possibly v5 if it is 16 or greater, v3 is set to B0
    * if length of payload equals C1 or B5: v4 ist set to c1, v5 is set to length of payload, payload starts at v6
    * if length of payload not equals C1: v4 ist set to length of payload, payload starts at v5

Typical sequences are:

Single Frame, DID 0x09BE, lenght 4:
```
can0  693   [8]  21 BE 09 B4 95 0E 00 00
```

Multi Frame, DID 0x011A, length 9:
```
can0  693   [8]  21 1A 01 B9 90 01 D4 00
can0  693   [8]  22 E5 01 82 01 00 55 55
```

Multi Frame, DID 0x0224, length 24 (0x18):
```
can0  693   [8]  21 24 02 B0 18 55 00 00
can0  693   [8]  22 00 1A 03 00 00 5F 0A
can0  693   [8]  23 00 00 38 0F 00 00 9B
can0  693   [8]  24 32 00 00 57 5E 00 00
```

Multi Frame, DID 0x0509, length 181 (0xB5):
```
can0  693   [8]  21 09 05 B0 C1 B5 00 00
can0  693   [8]  22 00 00 00 00 00 00 00
can0  693   [8]  23 00 00 00 00 00 00 00
can0  693   [8]  24 00 00 00 00 00 00 00
can0  693   [8]  25 00 00 00 00 00 00 00
can0  693   [8]  26 00 00 00 00 00 00 00
can0  693   [8]  27 00 00 00 00 00 00 00
can0  693   [8]  28 00 00 00 00 00 00 00
can0  693   [8]  29 00 00 00 00 00 00 00
can0  693   [8]  2A 00 00 00 00 00 00 00
can0  693   [8]  2B 00 00 00 00 00 00 00
can0  693   [8]  2C 00 00 00 00 00 00 00
can0  693   [8]  2D 00 00 00 00 00 00 00
can0  693   [8]  2E 00 00 00 00 00 00 00
can0  693   [8]  2F 00 00 00 00 00 00 00
can0  693   [8]  20 00 00 00 00 00 00 00
can0  693   [8]  21 00 00 00 00 00 00 00
can0  693   [8]  22 00 00 00 00 00 00 00
can0  693   [8]  23 00 00 00 00 00 00 00
can0  693   [8]  24 00 00 00 00 00 00 00
can0  693   [8]  25 00 00 00 00 00 00 00
can0  693   [8]  26 00 00 00 00 00 00 00
can0  693   [8]  27 00 00 00 00 00 00 00
can0  693   [8]  28 00 00 00 00 00 00 00
can0  693   [8]  29 00 00 00 00 00 00 00
can0  693   [8]  2A 00 00 00 00 00 00 00
can0  693   [8]  2B 00 00 00 00 55 55 55
```

---

## Service 77 (proprietary write protocol)

### Background

Service 77 is a Viessmann-proprietary write protocol discovered via reverse engineering. It operates in parallel with UDS on a dedicated CAN-ID pair and allows writing of data points that are protected against normal `WriteDataByIdentifier` (UDS service 0x2E).

Viessmann uses this mechanism to protect certain data points from accidental or unauthorised modification. When a client receives NRC `0x22` (conditionsNotCorrect) in response to a normal UDS write, it can retry the same write using Service 77 on the dedicated CAN-ID.

Both protocols share the same data store: a value written via Service 77 is immediately readable via UDS `ReadDataByIdentifier`.

### CAN-ID mapping

The Service 77 CAN-IDs are derived from the device's UDS address:

| | CAN-ID |
|---|---|
| Service 77 request  | `device_tx + 0x02`  (e.g. `0x682` for main device at `0x680`) |
| Service 77 response | `device_tx + 0x12`  (= request + `0x10`) |

### Transport layer

Service 77 uses the same ISO 15765-2 (ISO-TP) framing as UDS. The reassembled payload is described below.

### Request frame format

```
[0x77] [DID_HIGH] [DID_LOW] [PREFIX_0] [PREFIX_1] [PREFIX_2] [PREFIX_3] [PREFIX_4] [PREFIX_5] [DATA ...]
```

| Field | Bytes | Description |
|---|---|---|
| Service ID | 1 | Always `0x77` |
| DID | 2 | Data identifier, big-endian (high byte first) |
| Prefix | 6 | Client-specific prefix bytes, content not known; ignored by the server |
| Data | n | New value for the data point |

The 6-byte prefix is present in every request. Its content is not known in detail and is not stored.

### Response frame format

Positive response:

```
[0x77] [DID_HIGH] [DID_LOW] [0x44]
```

| Field | Bytes | Description |
|---|---|---|
| Service ID | 1 | Always `0x77` |
| DID | 2 | Data identifier echoed from request (high byte first) |
| Confirmation byte | 1 | Always `0x44` (Viessmann-specific, no UDS equivalent) |

Negative response (reuses UDS encoding):

```
[0x7F] [0x77] [NRC]
```

| NRC | Meaning |
|---|---|
| `0x12` | Payload too short (subFunctionNotSupported) |
| `0x31` | DID not present in data store (requestOutOfRange) |

### Interaction with UDS WriteDataByIdentifier

The `service77` key in `devices.json` specifies a list of DIDs that are protected against normal UDS writes:

* A `WriteDataByIdentifier` (0x2E) request targeting a protected DID returns NRC `0x22` (conditionsNotCorrect) without modifying the data store.
* A Service 77 request targeting the same DID is accepted and the value is written normally.
* Service 77 accepts writes to **all** known DIDs, including unprotected ones.

### Example exchange

Write DID `0x044C` (decimal 1100) on the main device (`tx = 0x680`):

```
# Client request on 0x682 (= 0x680 + 0x02):
682   [8]  07 77 04 4C AA BB CC DD EE FF  ← SID=77, DID=044C, 6-byte prefix, data=AA BB CC

# (ISO-TP framing applies for payloads > 7 bytes; shown here as single frame for brevity)

# Server response on 0x692 (= 0x682 + 0x10):
692   [8]  04 77 04 4C 44 55 55 55       ← SID=77, DID=044C, confirm=0x44
```