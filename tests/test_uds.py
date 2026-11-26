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


# ------------------------------------------------------------------
# Data length on WriteDataByIdentifier (fixed vs. variable length)
# ------------------------------------------------------------------

import json
import logging

from main import parse_variable_length_dids
from simulator.device import SimulatedDevice
from simulator.protocol.uds import (
    NRC_CONDITIONS_NOT_CORRECT,
    NRC_INCORRECT_MESSAGE_LENGTH,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
VDENS_CONFIG = ROOT / "config" / "devices_vdens.json"
VDENS_VALUES = DATA_DIR / "virtdata_vdens_680.txt"
VCAL_VALUES = DATA_DIR / "virtdata_680.txt"

HK1 = bytes.fromhex("484b3100")                         # "HK1", 4 bytes
HEIZKREIS2 = bytes.fromhex("4865697a6b72656973203200")  # "Heizkreis 2", 12 bytes

# max_len per DID for the handler tests: 627 → 6, 628 → 40
VAR_LEN = {627: 6, 628: 40}


def _write(did: int, data: bytes) -> bytes:
    return bytes([0x2E, did >> 8, did & 0xFF]) + data


def _read(did: int) -> bytes:
    return bytes([0x22, did >> 8, did & 0xFF])


def _ok(did: int) -> bytes:
    return bytes([0x6E, did >> 8, did & 0xFF])


def _assert_nrc(resp: bytes, nrc: int) -> None:
    assert resp == bytes([SID_NEGATIVE_RESP, SID_WRITE_DATA_BY_ID, nrc])


@pytest.fixture
def vdens_store() -> DatapointStore:
    return DatapointStore.from_file(VDENS_VALUES)


@pytest.fixture
def vdens_handler() -> UDSHandler:
    return UDSHandler(variable_length_dids=VAR_LEN)


def test_variable_read_returns_stored_length(vdens_handler, vdens_store):
    assert vdens_handler.handle(_read(627), vdens_store) == bytes([0x62, 0x02, 0x73]) + HK1
    assert vdens_handler.handle(_read(628), vdens_store) == bytes([0x62, 0x02, 0x74]) + HEIZKREIS2


def test_variable_write_shorter_then_longer(vdens_handler, vdens_store):
    # Shorter: "Heizkreis 2" (12 bytes) → "HK2" (4 bytes)
    hk2 = b"HK2\x00"
    assert vdens_handler.handle(_write(628, hk2), vdens_store) == _ok(628)
    assert vdens_handler.handle(_read(628), vdens_store) == bytes([0x62, 0x02, 0x74]) + hk2

    # Longer than the current value and longer than the value in virtdata
    long_name = b"Heizkreis Obergeschoss\x00"  # 23 bytes
    assert vdens_handler.handle(_write(628, long_name), vdens_store) == _ok(628)
    assert vdens_handler.handle(_read(628), vdens_store) == bytes([0x62, 0x02, 0x74]) + long_name


def test_variable_write_longer_than_virtdata_up_to_max_len(vdens_handler, vdens_store):
    # 627 is stored with 4 bytes, max_len is 6
    assert vdens_handler.handle(_write(627, b"HK11\x00"), vdens_store) == _ok(627)
    assert vdens_store.read(627) == b"HK11\x00"
    assert vdens_handler.handle(_write(627, b"HK111\x00"), vdens_store) == _ok(627)
    assert vdens_store.read(627) == b"HK111\x00"


def test_variable_write_full_max_len(vdens_handler, vdens_store):
    full = b"A" * 39 + b"\x00"
    assert vdens_handler.handle(_write(628, full), vdens_store) == _ok(628)
    assert vdens_store.read(628) == full


def test_variable_write_single_byte_accepted(vdens_handler, vdens_store):
    assert vdens_handler.handle(_write(627, b"\x00"), vdens_store) == _ok(627)
    assert vdens_store.read(627) == b"\x00"


def test_variable_write_too_long_rejected(vdens_handler, vdens_store):
    # 627: max_len 6; 7 bytes → too long
    resp = vdens_handler.handle(_write(627, b"HK1111\x00"), vdens_store)
    _assert_nrc(resp, NRC_INCORRECT_MESSAGE_LENGTH)
    assert vdens_store.read(627) == HK1

    # 628: max_len 40; 41 bytes → too long
    resp = vdens_handler.handle(_write(628, b"A" * 40 + b"\x00"), vdens_store)
    _assert_nrc(resp, NRC_INCORRECT_MESSAGE_LENGTH)
    assert vdens_store.read(628) == HEIZKREIS2


def test_variable_write_without_data_rejected(vdens_handler, vdens_store):
    # Zero data bytes: request is too short (NRC 0x12), value unchanged.
    resp = vdens_handler.handle(_write(627, b""), vdens_store)
    _assert_nrc(resp, NRC_SUBFUNCTION_NOT_SUPPORTED)
    assert vdens_store.read(627) == HK1


def test_fixed_write_other_length_rejected(handler, store):
    # DID 256 is fixed length 2
    for data in (b"\x01", b"\x01\x02\x03"):
        resp = handler.handle(_write(256, data), store)
        _assert_nrc(resp, NRC_INCORRECT_MESSAGE_LENGTH)
        assert store.read(256) == bytes([0x00, 0xD7])


def test_fixed_vcal_627_stays_40_bytes():
    store = DatapointStore.from_file(VCAL_VALUES)
    h = UDSHandler()
    assert len(store.read(627)) == 40
    _assert_nrc(h.handle(_write(627, b"HK11\x00"), store), NRC_INCORRECT_MESSAGE_LENGTH)
    new = b"HK11".ljust(40, b"\x00")
    assert h.handle(_write(627, new), store) == _ok(627)
    assert store.read(627) == new


def test_without_config_variable_dids_are_fixed(vdens_store):
    # Same data, no variableLengthDids → fixed length behaviour.
    h = UDSHandler()
    _assert_nrc(h.handle(_write(628, b"HK2\x00"), vdens_store), NRC_INCORRECT_MESSAGE_LENGTH)
    assert vdens_store.read(628) == HEIZKREIS2
    assert h.handle(_write(627, b"HK9\x00"), vdens_store) == _ok(627)


def test_protection_has_priority_over_length(vdens_store):
    h = UDSHandler(
        service77_dids=frozenset({627}),
        variable_length_dids={627: 6},
    )
    _assert_nrc(h.handle(_write(627, b"HK1111\x00"), vdens_store), NRC_CONDITIONS_NOT_CORRECT)
    _assert_nrc(h.handle(_write(627, b"X\x00"), vdens_store), NRC_CONDITIONS_NOT_CORRECT)
    assert vdens_store.read(627) == HK1


def test_unknown_variable_did_still_out_of_range(vdens_handler, vdens_store):
    resp = UDSHandler(variable_length_dids={0x0999: 10}).handle(
        _write(0x0999, b"\x00"), vdens_store
    )
    _assert_nrc(resp, NRC_REQUEST_OUT_OF_RANGE)


# ------------------------------------------------------------------
# Config parsing and device warnings
# ------------------------------------------------------------------

def test_parse_variable_length_dids():
    assert parse_variable_length_dids("d", []) == {}
    assert parse_variable_length_dids("d", [[627, 40], [631, 12]]) == {627: 40, 631: 12}
    assert parse_variable_length_dids("d", [[627, 1]]) == {627: 1}


@pytest.mark.parametrize("raw", [
    [627],                 # old format: plain DID
    [[627]],               # missing max_len
    [[627, 40, 1]],        # too many elements
    [[627, "40"]],         # not an int
    [[627, 0]],            # max_len < 1
    [[627, -5]],
    [[627, True]],
    {"627": 40},           # not a list
])
def test_parse_variable_length_dids_invalid(raw):
    with pytest.raises(ValueError):
        parse_variable_length_dids("d", raw)


def _make_vdens_device(var_len):
    return SimulatedDevice(
        name="vdens", tx_id=0x680,
        dp_list_path=DATA_DIR / "Open3Edatapoints_vdens_680.py",
        dp_values_path=VDENS_VALUES,
        bus=None,
        variable_length_dids=var_len,
    )


def test_device_warns_on_unknown_variable_did(caplog):
    with caplog.at_level(logging.WARNING, logger="simulator.device"):
        _make_vdens_device({627: 40, 9999: 10})
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].endswith(": [9999]")


def test_device_warns_if_stored_value_exceeds_max_len(caplog):
    # 628 is stored with 12 bytes
    with caplog.at_level(logging.WARNING, logger="simulator.device"):
        _make_vdens_device({628: 10})
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "DID 628" in warnings[0] and "max_len 10" in warnings[0]


def test_vdens_config_loads_without_warning(caplog):
    entry = json.loads(VDENS_CONFIG.read_text())["vdens"]
    var_len = parse_variable_length_dids("vdens", entry["variableLengthDids"])
    assert var_len[627] == 40 and var_len[634] == 12
    with caplog.at_level(logging.WARNING, logger="simulator.device"):
        dev = _make_vdens_device(var_len)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert dev.datastore.read(627) == HK1
