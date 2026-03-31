"""
tests/test_doip.py – Tests for the DoIP server.

Strategy
--------
* Frame builder functions: verify exact byte layout for each message type.
* DoIPServer: spin up a real asyncio TCP server on a random port, connect
  with raw asyncio streams (no external library), and verify the full
  Routing Activation + Diagnostic Message exchange.
* handle_uds_payload: verify that device.handle_uds_payload() returns the
  correct UDS response without any ISO-TP or CAN framing.
* _parse_doip_address: verify host:port parsing.
"""

from __future__ import annotations

import asyncio
import struct
import tempfile
import pytest
from pathlib import Path

from simulator.doip import (
    DoIPServer,
    DEFAULT_PORT,
    DEFAULT_HOST,
    DOIP_HEADER_LEN,
    DOIP_VERSION,
    DOIP_VERSION_INV,
    PT_ROUTING_ACTIVATION_REQ,
    PT_ROUTING_ACTIVATION_RESP,
    PT_DIAGNOSTIC_MSG,
    PT_DIAGNOSTIC_ACK,
    RA_ACTIVATED,
    DIAG_ACK,
    DIAG_NACK_UNKNOWN_TARGET,
    SERVER_LOGICAL_ADDR,
    _header,
    _routing_activation_response,
    _diagnostic_ack,
    _diagnostic_message,
)
from simulator.datastore import DatapointStore
from main import _parse_doip_address


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(content: str = "256 00 D7\n700 01\n") -> DatapointStore:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(content)
    return DatapointStore.from_file(f.name)


def parse_header(data: bytes) -> tuple[int, int, int, int]:
    """Unpack an 8-byte DoIP header → (version, version_inv, payload_type, payload_len)."""
    return struct.unpack("!BBHI", data[:DOIP_HEADER_LEN])


def build_routing_activation_request(client_addr: int = 0x0E00) -> bytes:
    """Build a minimal Routing Activation Request as doipclient would send."""
    # Payload: client logical address (2B) + activation type (1B) + reserved (4B)
    payload = struct.pack("!HBI", client_addr, 0x00, 0)
    return _header(PT_ROUTING_ACTIVATION_REQ, len(payload)) + payload


def build_diagnostic_request(
    source_addr: int,
    target_addr: int,
    uds_payload: bytes,
) -> bytes:
    """Build a Diagnostic Message frame."""
    addr_bytes = struct.pack("!HH", source_addr, target_addr)
    payload    = addr_bytes + uds_payload
    return _header(PT_DIAGNOSTIC_MSG, len(payload)) + payload


async def read_doip_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one complete DoIP frame; return (payload_type, payload)."""
    header = await reader.readexactly(DOIP_HEADER_LEN)
    _, _, payload_type, payload_len = struct.unpack("!BBHI", header)
    payload = await reader.readexactly(payload_len) if payload_len else b""
    return payload_type, payload


# ---------------------------------------------------------------------------
# Fake SimulatedDevice for DoIP tests (no CAN bus needed)
# ---------------------------------------------------------------------------

class FakeDevice:
    """Minimal device stub with handle_uds_payload and tx_id."""

    def __init__(self, tx_id: int, store: DatapointStore) -> None:
        from simulator.protocol.uds import UDSHandler
        self.tx_id = tx_id
        self._store   = store
        self._handler = UDSHandler()

    async def handle_uds_payload(self, payload: bytes):
        response = self._handler.handle(payload, self._store)
        return response


# ---------------------------------------------------------------------------
# Frame builder tests
# ---------------------------------------------------------------------------

class TestFrameBuilders:

    def test_header_layout(self):
        h = _header(PT_DIAGNOSTIC_MSG, 42)
        assert len(h) == DOIP_HEADER_LEN
        ver, ver_inv, pt, plen = struct.unpack("!BBHI", h)
        assert ver     == DOIP_VERSION
        assert ver_inv == DOIP_VERSION_INV
        assert pt      == PT_DIAGNOSTIC_MSG
        assert plen    == 42

    def test_routing_activation_response_layout(self):
        resp = _routing_activation_response(0x0E00)
        _, payload = resp[:DOIP_HEADER_LEN], resp[DOIP_HEADER_LEN:]
        client_addr, server_addr, code, _ = struct.unpack("!HHBI", payload)
        assert client_addr == 0x0E00
        assert server_addr == SERVER_LOGICAL_ADDR
        assert code        == RA_ACTIVATED

    def test_diagnostic_ack_layout(self):
        frame = _diagnostic_ack(0x0010, 0x0E00)
        payload = frame[DOIP_HEADER_LEN:]
        pt = struct.unpack("!H", frame[2:4])[0]
        assert pt == PT_DIAGNOSTIC_ACK
        src, tgt, code = struct.unpack("!HHB", payload)
        assert src  == 0x0010
        assert tgt  == 0x0E00
        assert code == DIAG_ACK

    def test_diagnostic_message_carries_uds(self):
        uds = bytes([0x62, 0x01, 0x00, 0xAB, 0xCD])
        frame = _diagnostic_message(0x0010, 0x0E00, uds)
        payload = frame[DOIP_HEADER_LEN:]
        src  = struct.unpack("!H", payload[0:2])[0]
        tgt  = struct.unpack("!H", payload[2:4])[0]
        body = payload[4:]
        assert src  == 0x0010
        assert tgt  == 0x0E00
        assert body == uds

    def test_header_payload_type_round_trip(self):
        for pt in (PT_ROUTING_ACTIVATION_REQ, PT_ROUTING_ACTIVATION_RESP,
                   PT_DIAGNOSTIC_MSG, PT_DIAGNOSTIC_ACK):
            h = _header(pt, 0)
            _, _, got_pt, _ = struct.unpack("!BBHI", h)
            assert got_pt == pt


# ---------------------------------------------------------------------------
# _parse_doip_address
# ---------------------------------------------------------------------------

class TestParseDoipAddress:

    def test_port_only(self):
        assert _parse_doip_address("13400") == ("127.0.0.1", 13400)

    def test_host_and_port(self):
        assert _parse_doip_address("0.0.0.0:13400") == ("0.0.0.0", 13400)

    def test_localhost(self):
        host, port = _parse_doip_address("127.0.0.1:9999")
        assert host == "127.0.0.1" and port == 9999

    def test_non_standard_port(self):
        _, port = _parse_doip_address("5000")
        assert port == 5000


# ---------------------------------------------------------------------------
# DoIPServer integration tests (real TCP)
# ---------------------------------------------------------------------------

async def _find_free_port() -> int:
    """Bind to port 0, get the assigned port, then release it."""
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def server_and_device():
    """Spin up a DoIPServer with one FakeDevice, yield (server, device, port)."""
    store  = make_store()
    device = FakeDevice(tx_id=0x0680, store=store)
    port   = await _find_free_port()
    srv    = DoIPServer({0x0680: device}, host="127.0.0.1", port=port)
    await srv.start()
    yield srv, device, port
    await srv.stop()


class TestDoIPServer:

    @pytest.mark.asyncio
    async def test_routing_activation_response(self, server_and_device):
        srv, device, port = server_and_device
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        writer.write(build_routing_activation_request(0x0E00))
        await writer.drain()

        pt, payload = await read_doip_frame(reader)
        assert pt == PT_ROUTING_ACTIVATION_RESP
        client_addr, server_addr, code, _ = struct.unpack("!HHBI", payload)
        assert client_addr == 0x0E00
        assert code        == RA_ACTIVATED

        writer.close()
        await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_diagnostic_read_by_did(self, server_and_device):
        """Full exchange: Routing Activation + ReadDataByIdentifier for DID 256."""
        srv, device, port = server_and_device
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        # Routing Activation
        writer.write(build_routing_activation_request())
        await writer.drain()
        await read_doip_frame(reader)   # consume RA response

        # ReadDataByIdentifier DID 256 (0x0100)
        uds_req = bytes([0x22, 0x01, 0x00])
        writer.write(build_diagnostic_request(0x0E00, 0x0680, uds_req))
        await writer.drain()

        # First response: Diagnostic ACK
        pt_ack, payload_ack = await read_doip_frame(reader)
        assert pt_ack == PT_DIAGNOSTIC_ACK
        ack_code = struct.unpack("!B", payload_ack[4:5])[0]
        assert ack_code == DIAG_ACK

        # Second response: Diagnostic Message with UDS response
        pt_resp, payload_resp = await read_doip_frame(reader)
        assert pt_resp == PT_DIAGNOSTIC_MSG
        uds_resp = payload_resp[4:]   # strip source/target addresses
        assert uds_resp[0] == 0x62   # positive response for 0x22
        assert uds_resp[1] == 0x01   # DID high
        assert uds_resp[2] == 0x00   # DID low
        assert uds_resp[3:] == bytes([0x00, 0xD7])   # value from store

        writer.close()
        await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_diagnostic_unknown_target(self, server_and_device):
        """Request to unknown ECU address returns NACK."""
        srv, device, port = server_and_device
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        writer.write(build_routing_activation_request())
        await writer.drain()
        await read_doip_frame(reader)   # consume RA response

        uds_req = bytes([0x22, 0x01, 0x00])
        writer.write(build_diagnostic_request(0x0E00, 0x9999, uds_req))  # unknown
        await writer.drain()

        pt, payload = await read_doip_frame(reader)
        assert pt == PT_DIAGNOSTIC_ACK
        code = struct.unpack("!B", payload[4:5])[0]
        assert code == DIAG_NACK_UNKNOWN_TARGET

        writer.close()
        await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_diagnostic_before_activation_ignored(self, server_and_device):
        """Diagnostic Message sent before Routing Activation must be ignored."""
        srv, device, port = server_and_device
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        # Send diagnostic WITHOUT routing activation first
        uds_req = bytes([0x22, 0x01, 0x00])
        writer.write(build_diagnostic_request(0x0E00, 0x0680, uds_req))
        await writer.drain()

        # Then do proper routing activation
        writer.write(build_routing_activation_request())
        await writer.drain()

        pt, _ = await read_doip_frame(reader)
        # The first thing we should receive is the Routing Activation Response,
        # not a diagnostic response – the earlier message was silently ignored.
        assert pt == PT_ROUTING_ACTIVATION_RESP

        writer.close()
        await writer.wait_closed()

    @pytest.mark.asyncio
    async def test_multiple_requests_same_connection(self, server_and_device):
        """Two consecutive read requests on the same connection both succeed."""
        srv, device, port = server_and_device
        reader, writer = await asyncio.open_connection("127.0.0.1", port)

        writer.write(build_routing_activation_request())
        await writer.drain()
        await read_doip_frame(reader)

        for _ in range(2):
            writer.write(build_diagnostic_request(0x0E00, 0x0680, bytes([0x22, 0x01, 0x00])))
            await writer.drain()
            pt_ack, _ = await read_doip_frame(reader)
            pt_resp, payload = await read_doip_frame(reader)
            assert pt_ack  == PT_DIAGNOSTIC_ACK
            assert pt_resp == PT_DIAGNOSTIC_MSG
            assert payload[4] == 0x62

        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# handle_uds_payload (unit test, no TCP)
# ---------------------------------------------------------------------------

class TestHandleUdsPayload:

    @pytest.mark.asyncio
    async def test_read_known_did(self):
        device = FakeDevice(0x0680, make_store("256 00 D7\n"))
        resp = await device.handle_uds_payload(bytes([0x22, 0x01, 0x00]))
        assert resp == bytes([0x62, 0x01, 0x00, 0x00, 0xD7])

    @pytest.mark.asyncio
    async def test_read_unknown_did(self):
        device = FakeDevice(0x0680, make_store("256 00 D7\n"))
        resp = await device.handle_uds_payload(bytes([0x22, 0x09, 0x99]))
        assert resp[0] == 0x7F   # negative response

    @pytest.mark.asyncio
    async def test_write_did(self):
        store  = make_store("256 00 D7\n")
        device = FakeDevice(0x0680, store)
        resp = await device.handle_uds_payload(bytes([0x2E, 0x01, 0x00, 0xAB, 0xCD]))
        assert resp == bytes([0x6E, 0x01, 0x00])
        assert store.read(256) == bytes([0xAB, 0xCD])

    @pytest.mark.asyncio
    async def test_empty_payload_returns_none(self):
        device = FakeDevice(0x0680, make_store())
        resp = await device.handle_uds_payload(b"")
        assert resp is None
