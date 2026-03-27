"""Tests for simulator.protocol.uds (UDS handler)."""

import pytest
from pathlib import Path
from simulator.datastore import DatapointStore
from simulator.protocol.uds import (
    UDSHandler,
    SID_READ_DATA_BY_ID,
    SID_WRITE_DATA_BY_ID,
    SID_NEGATIVE_RESP,
    NRC_SERVICE_NOT_SUPPORTED,
    NRC_SUBFUNCTION_NOT_SUPPORTED,
    NRC_REQUEST_OUT_OF_RANGE,
)


@pytest.fixture
def store(tmp_path: Path) -> DatapointStore:
    f = tmp_path / "dp.txt"
    f.write_text("256 00 D7\n700 01\n")
    return DatapointStore.from_file(f)


@pytest.fixture
def handler() -> UDSHandler:
    return UDSHandler()


# ------------------------------------------------------------------
# ReadDataByIdentifier (0x22)
# ------------------------------------------------------------------

def test_read_positive(handler, store):
    # DID 256 = 0x0100
    req = bytes([0x22, 0x01, 0x00])
    resp = handler.handle(req, store)
    assert resp == bytes([0x62, 0x01, 0x00, 0x00, 0xD7])


def test_read_unknown_did(handler, store):
    req = bytes([0x22, 0x09, 0x99])  # DID 0x0999 = 2457, not in store
    resp = handler.handle(req, store)
    assert resp[0] == SID_NEGATIVE_RESP
    assert resp[1] == SID_READ_DATA_BY_ID
    assert resp[2] == NRC_REQUEST_OUT_OF_RANGE


def test_read_too_short(handler, store):
    req = bytes([0x22, 0x01])  # missing low byte
    resp = handler.handle(req, store)
    assert resp[0] == SID_NEGATIVE_RESP
    assert resp[2] == NRC_SUBFUNCTION_NOT_SUPPORTED


# ------------------------------------------------------------------
# WriteDataByIdentifier (0x2E)
# ------------------------------------------------------------------

def test_write_positive(handler, store):
    # DID 256 = 0x0100, new value = 0x01 0x2C
    req = bytes([0x2E, 0x01, 0x00, 0x01, 0x2C])
    resp = handler.handle(req, store)
    assert resp == bytes([0x6E, 0x01, 0x00])
    # Value must be updated in the store
    assert store.read(256) == bytes([0x01, 0x2C])


def test_write_unknown_did(handler, store):
    req = bytes([0x2E, 0x09, 0x99, 0x42])
    resp = handler.handle(req, store)
    assert resp[0] == SID_NEGATIVE_RESP
    assert resp[1] == SID_WRITE_DATA_BY_ID
    assert resp[2] == NRC_REQUEST_OUT_OF_RANGE


def test_write_too_short(handler, store):
    req = bytes([0x2E, 0x01])
    resp = handler.handle(req, store)
    assert resp[0] == SID_NEGATIVE_RESP
    assert resp[2] == NRC_SUBFUNCTION_NOT_SUPPORTED


# ------------------------------------------------------------------
# Unknown service
# ------------------------------------------------------------------

def test_unknown_service(handler, store):
    req = bytes([0x10, 0x01])  # DiagnosticSessionControl – not implemented
    resp = handler.handle(req, store)
    assert resp[0] == SID_NEGATIVE_RESP
    assert resp[1] == 0x10
    assert resp[2] == NRC_SERVICE_NOT_SUPPORTED


def test_empty_payload(handler, store):
    assert handler.handle(b"", store) is None
