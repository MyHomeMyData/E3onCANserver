"""
tests/test_service77.py – Tests for Service 77 and the UDS protection list.

Covers:
  - Service77Handler: positive response, NRC on unknown DID, short payload
  - UDSHandler: NRC 0x22 on protected DIDs, normal writes still work
  - Shared DatapointStore: Service 77 write is visible via UDS read
  - CAN-ID offset constants
"""

import pytest
from pathlib import Path
import tempfile

from simulator.datastore import DatapointStore
from simulator.protocol.service77 import (
    Service77Handler,
    SID_SERVICE77,
    S77_CONFIRM_BYTE,
    S77_REQUEST_ID_OFFSET,
    S77_RESPONSE_ID_OFFSET,
)
from simulator.protocol.uds import (
    UDSHandler,
    SID_READ_DATA_BY_ID,
    SID_WRITE_DATA_BY_ID,
    SID_NEGATIVE_RESP,
    NRC_CONDITIONS_NOT_CORRECT,
    NRC_REQUEST_OUT_OF_RANGE,
    NRC_SUBFUNCTION_NOT_SUPPORTED,
    NRC_SERVICE_NOT_SUPPORTED,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(content: str = "256 00 D7\n700 01\n1234 FF 00 AB\n") -> DatapointStore:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(content)
        fname = f.name
    return DatapointStore.from_file(fname)


# ---------------------------------------------------------------------------
# CAN-ID offset constants
# ---------------------------------------------------------------------------

class TestCanIdOffsets:

    def test_s77_request_offset_is_2(self):
        assert S77_REQUEST_ID_OFFSET == 0x02

    def test_s77_response_offset_is_0x12(self):
        assert S77_RESPONSE_ID_OFFSET == 0x12

    def test_s77_response_equals_request_plus_uds_offset(self):
        # response = request + 0x10 (standard UDS offset)
        assert S77_RESPONSE_ID_OFFSET == S77_REQUEST_ID_OFFSET + 0x10

    def test_example_main_device(self):
        tx_id = 0x680
        assert tx_id + S77_REQUEST_ID_OFFSET  == 0x682
        assert tx_id + S77_RESPONSE_ID_OFFSET == 0x692


# ---------------------------------------------------------------------------
# Service77Handler
# ---------------------------------------------------------------------------

class TestService77Handler:

    def test_positive_response_format(self):
        """[0x77][0x04][DID_HI][DID_LO]"""
        h = Service77Handler()
        s = make_store()
        req = bytes([0x77, 0x01, 0x00, 0xAB, 0xCD])  # DID=0x0100, not in store
        # DID 0x0100 = 256, which IS in our store
        req = bytes([0x77, 0x01, 0x00, 0xAB, 0xCD])
        resp = h.handle(req, s)
        assert resp[0] == SID_SERVICE77
        assert resp[1] == S77_CONFIRM_BYTE
        assert resp[2] == 0x01   # DID_HI
        assert resp[3] == 0x00   # DID_LO

    def test_positive_response_length(self):
        h = Service77Handler()
        s = make_store()
        req = bytes([0x77, 0x01, 0x00, 0xFF])   # DID 256
        resp = h.handle(req, s)
        assert len(resp) == 4

    def test_write_stored_in_datastore(self):
        h = Service77Handler()
        s = make_store()
        req = bytes([0x77, 0x01, 0x00, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0x12, 0x34])   # DID 256 ← [0x12, 0x34], 6 bytes padding
        h.handle(req, s)
        assert s.read(256) == bytes([0x12, 0x34])

    def test_unknown_did_returns_nrc_request_out_of_range(self):
        h = Service77Handler()
        s = make_store()
        req = bytes([0x77, 0x09, 0x99, 0x42])   # DID 0x0999 not in store
        resp = h.handle(req, s)
        assert resp[0] == SID_NEGATIVE_RESP
        assert resp[1] == SID_SERVICE77
        assert resp[2] == NRC_REQUEST_OUT_OF_RANGE

    def test_too_short_returns_nrc_subfunction(self):
        h = Service77Handler()
        s = make_store()
        req = bytes([0x77, 0x01, 0x00])   # missing data byte
        resp = h.handle(req, s)
        assert resp[0] == SID_NEGATIVE_RESP
        assert resp[2] == NRC_SUBFUNCTION_NOT_SUPPORTED

    def test_empty_payload_returns_none(self):
        h = Service77Handler()
        s = make_store()
        assert h.handle(b"", s) is None

    def test_wrong_service_id_returns_nrc(self):
        h = Service77Handler()
        s = make_store()
        req = bytes([0x2E, 0x01, 0x00, 0x42])   # UDS write – wrong service
        resp = h.handle(req, s)
        assert resp[0] == SID_NEGATIVE_RESP

    def test_writes_protected_did_without_restriction(self):
        """Service 77 ignores the protection list – it has no concept of it."""
        h = Service77Handler()
        s = make_store()
        req = bytes([0x77, 0x01, 0x00, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0xBE, 0xEF])   # DID 256 (protected in UDS), 6 bytes padding
        resp = h.handle(req, s)
        # Service77Handler doesn't know about the protection list at all
        assert resp[0] == SID_SERVICE77
        assert resp[1] == S77_CONFIRM_BYTE
        assert s.read(256) == bytes([0xBE, 0xEF])


# ---------------------------------------------------------------------------
# UDSHandler – protection list (NRC 0x22)
# ---------------------------------------------------------------------------

class TestUDSProtectionList:

    def test_unprotected_write_succeeds(self):
        h = UDSHandler(service77_dids=frozenset([1234]))   # 256 not protected
        s = make_store()
        req = bytes([0x2E, 0x01, 0x00, 0xAA, 0xBB])
        resp = h.handle(req, s)
        assert resp == bytes([0x6E, 0x01, 0x00])
        assert s.read(256) == bytes([0xAA, 0xBB])

    def test_protected_did_returns_nrc_0x22(self):
        h = UDSHandler(service77_dids=frozenset([256]))
        s = make_store()
        req = bytes([0x2E, 0x01, 0x00, 0xAA, 0xBB])   # DID 256 is protected
        resp = h.handle(req, s)
        assert resp[0] == SID_NEGATIVE_RESP
        assert resp[1] == SID_WRITE_DATA_BY_ID
        assert resp[2] == NRC_CONDITIONS_NOT_CORRECT

    def test_protected_did_write_does_not_modify_store(self):
        h = UDSHandler(service77_dids=frozenset([256]))
        s = make_store()
        original = s.read(256)
        req = bytes([0x2E, 0x01, 0x00, 0xAA, 0xBB])
        h.handle(req, s)
        assert s.read(256) == original   # store must be unchanged

    def test_multiple_protected_dids(self):
        h = UDSHandler(service77_dids=frozenset([256, 700, 1234]))
        s = make_store()
        for did, hi, lo in [(256, 0x01, 0x00), (700, 0x02, 0xBC), (1234, 0x04, 0xD2)]:
            req = bytes([0x2E, hi, lo, 0xFF])
            resp = h.handle(req, s)
            assert resp[2] == NRC_CONDITIONS_NOT_CORRECT, \
                f"DID {did} should be protected"

    def test_read_is_never_affected_by_protection_list(self):
        h = UDSHandler(service77_dids=frozenset([256]))
        s = make_store()
        req = bytes([0x22, 0x01, 0x00])   # ReadDataByIdentifier DID 256
        resp = h.handle(req, s)
        assert resp[0] == 0x62   # positive response

    def test_empty_protection_list_is_default_behaviour(self):
        h = UDSHandler()   # no service77_dids
        s = make_store()
        req = bytes([0x2E, 0x01, 0x00, 0xAA, 0xBB])
        resp = h.handle(req, s)
        assert resp == bytes([0x6E, 0x01, 0x00])

    def test_unknown_service_still_returns_nrc_0x11(self):
        h = UDSHandler(service77_dids=frozenset([256]))
        s = make_store()
        req = bytes([0x10, 0x01])   # DiagnosticSessionControl
        resp = h.handle(req, s)
        assert resp[2] == NRC_SERVICE_NOT_SUPPORTED


# ---------------------------------------------------------------------------
# Shared DatapointStore: Service 77 write visible via UDS read
# ---------------------------------------------------------------------------

class TestSharedStore:

    def test_s77_write_reflected_in_uds_read(self):
        """Writing via Service 77 must immediately be visible to UDS reads."""
        store = make_store()
        s77 = Service77Handler()
        uds = UDSHandler(service77_dids=frozenset([256]))

        # Write DID 256 via Service 77
        s77.handle(bytes([0x77, 0x01, 0x00, 0x55, 0x55, 0x55, 0x55, 0x55, 0x55, 0xDE, 0xAD]), store)   # DID 256 (protected in UDS), 6 bytes padding

        # Read DID 256 via UDS – must return the new value
        resp = uds.handle(bytes([0x22, 0x01, 0x00]), store)
        assert resp == bytes([0x62, 0x01, 0x00, 0xDE, 0xAD])

    def test_uds_write_on_unprotected_did_reflected_in_s77_read(self):
        """A UDS write on an unprotected DID updates the store for both."""
        store = make_store()
        uds = UDSHandler(service77_dids=frozenset([1234]))   # 700 not protected
        s77 = Service77Handler()

        # Write DID 700 via UDS (not protected)
        uds.handle(bytes([0x2E, 0x02, 0xBC, 0x42]), store)

        # Service 77 read on DID 700 – both use the same store
        # (Service 77 only writes, but we can verify via store directly)
        assert store.read(700) == bytes([0x42])

    def test_protected_did_unchanged_after_failed_uds_write(self):
        """Failed UDS write on a protected DID must leave the store untouched."""
        store = make_store()
        uds = UDSHandler(service77_dids=frozenset([256]))
        original = store.read(256)

        uds.handle(bytes([0x2E, 0x01, 0x00, 0xFF, 0xFF]), store)
        assert store.read(256) == original
