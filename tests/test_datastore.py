"""Tests for simulator.datastore."""

import pytest
from pathlib import Path
from simulator.datastore import DatapointStore


@pytest.fixture
def sample_file(tmp_path: Path) -> Path:
    content = (
        "# comment line\n"
        "256 00 D7\n"
        "700 01\n"
        "1234 FF 00 AB\n"
    )
    f = tmp_path / "dp.txt"
    f.write_text(content)
    return f


def test_load_from_file(sample_file):
    store = DatapointStore.from_file(sample_file)
    assert len(store) == 3


def test_read_known_did(sample_file):
    store = DatapointStore.from_file(sample_file)
    assert store.read(256) == bytes([0x00, 0xD7])
    assert store.read(700) == bytes([0x01])
    assert store.read(1234) == bytes([0xFF, 0x00, 0xAB])


def test_read_unknown_did(sample_file):
    store = DatapointStore.from_file(sample_file)
    assert store.read(9999) is None


def test_write_known_did(sample_file):
    store = DatapointStore.from_file(sample_file)
    assert store.write(256, bytes([0x01, 0x00])) is True
    assert store.read(256) == bytes([0x01, 0x00])


def test_write_unknown_did(sample_file):
    store = DatapointStore.from_file(sample_file)
    assert store.write(9999, bytes([0x42])) is False


def test_dynamic_resolver(sample_file):
    store = DatapointStore.from_file(sample_file)
    store.register_resolver(256, lambda: bytes([0xAA, 0xBB]))
    assert store.read(256) == bytes([0xAA, 0xBB])


def test_known_dids_sorted(sample_file):
    store = DatapointStore.from_file(sample_file)
    assert store.known_dids() == [256, 700, 1234]
