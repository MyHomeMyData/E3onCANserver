"""
tests/test_bus.py – Tests for CANBus TX error handling.

Key guarantees verified
-----------------------
* Transient send errors (ENOBUFS, EAGAIN) are retried until the frame is sent.
* The errno is recognised both via ``CanError.error_code`` and via the
  wrapped ``OSError`` in ``__cause__``.
* Retries are bounded: after TX_RETRY_MAX retries the frame is dropped.
* Non-transient errors are not retried.
* The TX loop preserves frame order across retries.
"""

from __future__ import annotations

import asyncio
import errno
from unittest.mock import AsyncMock, patch

import can
import pytest

from simulator import bus as bus_mod
from simulator.bus import CANBus, TX_RETRY_MAX


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _oserror_wrapped(err: int) -> can.CanOperationError:
    """Build the exception python-can's socketcan backend raises on send()."""
    os_err = OSError(err, "simulated")
    try:
        raise can.CanOperationError(f"Failed to transmit: {os_err.strerror}", err) from os_err
    except can.CanOperationError as exc:
        return exc


def _cause_only(err: int) -> can.CanOperationError:
    """CanError without error_code, errno only available via __cause__."""
    os_err = OSError(err, "simulated")
    try:
        raise can.CanOperationError("Failed to transmit") from os_err
    except can.CanOperationError as exc:
        return exc


class FakeBus:
    """Records sent frames; raises the queued exceptions first."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.sent: list[can.Message] = []
        self.calls = 0

    def send(self, msg: can.Message) -> None:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        self.sent.append(msg)


def _make_bus(fake: FakeBus) -> CANBus:
    b = CANBus(interface="virtual", channel="test")
    b._bus = fake
    return b


def _msg(arb_id: int = 0x693, data: bytes = b"\x01") -> can.Message:
    return can.Message(arbitration_id=arb_id, data=data, is_extended_id=False)


@pytest.fixture
def no_sleep():
    with patch.object(bus_mod.asyncio, "sleep", new=AsyncMock()) as m:
        yield m


# ---------------------------------------------------------------------------
# _send_with_retry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("err", [errno.ENOBUFS, errno.EAGAIN])
async def test_transient_error_is_retried(no_sleep, err):
    fake = FakeBus([_oserror_wrapped(err)] * 2)
    await _make_bus(fake)._send_with_retry(_msg())
    assert fake.calls == 3
    assert len(fake.sent) == 1
    assert no_sleep.await_count == 2


async def test_errno_via_cause(no_sleep):
    fake = FakeBus([_cause_only(errno.ENOBUFS)])
    await _make_bus(fake)._send_with_retry(_msg())
    assert len(fake.sent) == 1


async def test_backoff_doubles(no_sleep):
    fake = FakeBus([_oserror_wrapped(errno.ENOBUFS)] * 3)
    await _make_bus(fake)._send_with_retry(_msg())
    delays = [c.args[0] for c in no_sleep.await_args_list]
    assert delays == [bus_mod.TX_RETRY_BACKOFF_S * f for f in (1, 2, 4)]


async def test_retries_are_bounded(no_sleep, caplog):
    fake = FakeBus([_oserror_wrapped(errno.ENOBUFS)] * (TX_RETRY_MAX + 5))
    await _make_bus(fake)._send_with_retry(_msg())
    assert fake.calls == TX_RETRY_MAX + 1
    assert fake.sent == []
    assert "dropped" in caplog.text


@pytest.mark.parametrize("exc", [
    _oserror_wrapped(errno.ENETDOWN),
    can.CanOperationError("generic failure"),
])
async def test_non_transient_error_not_retried(no_sleep, exc, caplog):
    fake = FakeBus([exc])
    await _make_bus(fake)._send_with_retry(_msg())
    assert fake.calls == 1
    assert fake.sent == []
    no_sleep.assert_not_awaited()
    assert "CAN TX error" in caplog.text


# ---------------------------------------------------------------------------
# _tx_loop
# ---------------------------------------------------------------------------

async def test_tx_loop_keeps_order_across_retries():
    """A burst with transient errors arrives complete and in order."""
    fake = FakeBus([_oserror_wrapped(errno.ENOBUFS)] * 3)
    b = _make_bus(fake)
    for i in range(26):
        await b.send(0x693, bytes([i]))
    task = asyncio.create_task(b._tx_loop())
    try:
        for _ in range(200):
            if len(fake.sent) == 26:
                break
            await asyncio.sleep(0.005)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert [m.data[0] for m in fake.sent] == list(range(26))
