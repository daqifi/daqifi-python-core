"""B7 — ``daqifi.channels`` analog-input slice.

Exercises the ADC channel surface against real hardware:

* ``AnalogInputs`` collection ops — ``__getitem__`` / ``__len__`` /
  ``__iter__`` round-trip the count populated by
  :meth:`NyquistDevice._initialize_channels` for the connected variant.
* ``AnalogInputChannel.enabled`` GET — the per-channel
  ``CONFigure:ADC:CHANnel? <i>`` query agrees with the firmware-side
  bitmask returned by ``ENABle:VOLTage:DC?`` after a bulk-bitmask
  write (the SCPI query the firmware uses as the source of truth for
  streaming-channel selection).
* ``AnalogInputs.enable_all`` / ``disable_all`` — bulk wrappers around
  the per-channel setter.
* ``AnalogInputChannel.range`` GET — variant-aware: NQ1 short-circuits
  to ``"FIXED"``; NQ3 issues the SCPI query.
* ``AnalogInputChannel.range`` SET — NQ1 raises
  ``FeatureNotSupportedError`` before any SCPI write.
* ``AnalogInputs.configure_channels`` — exercises the bulk-config path.

Hardware source-of-truth: the firmware bitmask via the raw SCPI query
``ENABle:VOLTage:DC?`` (memory-confirmed equivalent to per-channel
writes — see daqifi-python-core "bitmask vs per-channel SCPI are
equivalent" finding).

PRODUCTION BUG SURFACED — per-channel SET path is broken on real
hardware:

  ``AnalogInputChannel.enabled`` setter calls
  ``SerialInterface.send_set("CONFigure:ADC:CHANnel <ch>", value)`` →
  the SCPI command sent on the wire is
  ``CONFigure:ADC:CHANnel <ch> <value>`` (space-separated) but the
  firmware expects the comma-separated form
  ``CONFigure:ADC:CHANnel <ch>,<value>``. The malformed command is
  silently dropped by the firmware (no error queued, bitmask
  unchanged), but ``send_set`` ALSO returns False — independently —
  because the follow-up ``SYST:ERR?`` round-trip races with leftover
  bytes from the prior ``send_command`` window. So the setter raises
  ``CommandError`` even when the queue is clean, AND the underlying
  SCPI is malformed even when the setter doesn't raise.

  This breaks ``analog_inputs[i].enabled = True`` /
  ``enable_all()`` / ``disable_all()`` / ``configure_channels(...)``
  end-to-end. None of those paths affect streaming today (callers
  use the typed-bitmask factory ``enable_adc_channels`` instead),
  so the slice's per-channel SET tests are marked ``xfail`` and
  the bug is logged for a follow-up production-code PR (per the
  tests-only-PR scope rule). The per-channel GET tests below
  still validate the production GET surface end-to-end.
"""
from __future__ import annotations

import re
import time
from typing import Iterator

import pytest

from daqifi._internal import scpi
from daqifi.channels import AnalogInputChannel
from daqifi.device import NyquistDevice
from daqifi.discovery import BoardVariant, DeviceInfo
from daqifi.exceptions import FeatureNotSupportedError


# Strict ``SYST:ERR?`` no-error matcher. ``"No error" in err`` is too
# loose — leftover bytes in the read window can include strings like
# ``"No error in stream"`` (matches but isn't the canonical reply).
# Mirrors the bytes-side regex in ``tests/integration/conftest.py``,
# adapted for the str-shaped response that ``send_query`` returns.
_SCPI_NO_ERROR_STR_RE = re.compile(r'\b0\s*,\s*"?No error"?')


# ---- Local fixture ---------------------------------------------------------

# We can't reuse the ``clean_device`` / ``serial_transport`` fixtures here
# because those open a SerialStreamTransport that holds the COM port
# exclusively, and ``NyquistDevice`` opens its own ``SerialInterface``
# against the same port. Sharing would deadlock at port-open time on
# Windows. So this slice manages its own connection.


def _wake_and_drain(dev: NyquistDevice) -> None:
    """Bring the device into a state where channel writes will be
    accepted — wake from Standby and drain any stale SCPI errors.

    Mirrors the wake/init sequence that ``clean_device`` does for
    streaming tests, routed through the public ``NyquistDevice``
    surface."""
    dev.power_up()
    # Bounded clear (capped at 10 by the SerialInterface impl — that's
    # plenty for our setup since we haven't issued any error-producing
    # commands yet).
    dev.clear_errors()


def _read_firmware_bitmask(dev: NyquistDevice) -> int:
    """Return the current ADC channel-enable bitmask as the firmware
    reports it. This is the load-bearing assertion target — it's what
    the streaming engine uses to decide which channels to sample, so
    if a per-channel write doesn't show up here, streaming will be
    wrong even if the per-channel getter looks right.

    No public API surfaces the bulk ``ENABle:VOLTage:DC?`` query
    (the per-channel ``enabled`` getter only reads one bit at a
    time), so we use the underlying ``SerialInterface`` directly via
    the SCPI constant — this stays in sync with the production code
    if the constant is ever renamed."""
    response = dev._comm.send_query(scpi.AnalogInput.ENABLE_ALL_QUERY)
    return scpi.parse_int(response)


def _await_firmware_bitmask(
    dev: NyquistDevice,
    expected: int,
    *,
    timeout: float = 1.0,
    poll_interval: float = 0.02,
) -> None:
    """Poll until the firmware bitmask matches ``expected`` or
    ``timeout`` elapses. Replaces fixed-sleep settles so a slow
    firmware commit (USB CDC scheduling jitter, paging on the
    Windows VM, etc.) doesn't false-fail the next read — and so a
    real settle bug surfaces with a diagnostic instead of as a
    race-condition flake later."""
    start = time.monotonic()
    while True:
        actual = _read_firmware_bitmask(dev)
        if actual == expected:
            return
        if (time.monotonic() - start) >= timeout:
            raise AssertionError(
                f"firmware bitmask did not settle to 0x{expected:X} "
                f"within {timeout:g}s; last read was 0x{actual:X}"
            )
        time.sleep(poll_interval)


def _write_firmware_bitmask(dev: NyquistDevice, value: int) -> None:
    """Set the ADC channel-enable bitmask via the proven raw-SCPI
    path. Used for setting up GET-test preconditions independently
    of the production per-channel setter (which is broken — see
    module docstring). Uses the SCPI constant so a rename of the
    underlying command surface stays in lockstep with production
    code. Blocks until the firmware confirms the new bitmask via
    ``ENABle:VOLTage:DC?``."""
    dev._comm.send_command(
        f"{scpi.AnalogInput.ENABLE_ALL_SET} {value}", delay=0.2
    )
    _await_firmware_bitmask(dev, value)


@pytest.fixture
def nyquist_device(device_info: DeviceInfo) -> Iterator[NyquistDevice]:
    """Open a NyquistDevice on the discovered port. Yields a connected
    device with the error queue drained and Standby cleared. Disables
    all channels + disconnects on teardown so the next test starts
    from a known-clean baseline.

    Discovery may return the port name with trailing whitespace
    (Windows registry quirk); normalize before opening (same as the
    B2 / B6 ``_open_factory_device`` helpers)."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = NyquistDevice.connect(port.strip())
    try:
        _wake_and_drain(dev)
        yield dev
    finally:
        # Best-effort teardown: clear all enabled channels so the
        # next test (or a streaming test that runs later in the same
        # session) doesn't inherit a stale bitmask. Uses raw SCPI
        # because the per-channel setter is broken (see module
        # docstring).
        try:
            dev._comm.send_command(
                f"{scpi.AnalogInput.ENABLE_ALL_SET} 0", delay=0.2
            )
        except Exception:
            pass
        try:
            dev.disconnect()
        except Exception:
            pass


# ---- Collection ops --------------------------------------------------------


def test_analog_inputs_collection_size_matches_variant(
    nyquist_device: NyquistDevice,
) -> None:
    """``len(analog_inputs)`` reflects the variant-specific channel
    count populated by ``_initialize_channels`` — 16 on NQ1/NQ2,
    8 on NQ3. Variants we don't have in the test rig get skipped
    rather than guessing."""
    expected = {
        BoardVariant.NQ1: 16,
        BoardVariant.NQ2: 16,
        BoardVariant.NQ3: 8,
    }.get(nyquist_device.variant)
    if expected is None:
        pytest.skip(
            f"unknown variant {nyquist_device.variant!r}; "
            "extend the expected-count map when a new board ships"
        )
    assert len(nyquist_device.analog_inputs) == expected


def test_analog_inputs_iter_yields_indexed_channels(
    nyquist_device: NyquistDevice,
) -> None:
    """Iterating the collection yields ``AnalogInputChannel`` instances
    with strictly ascending ``index`` values starting at 0."""
    seen = list(nyquist_device.analog_inputs)
    assert len(seen) == len(nyquist_device.analog_inputs)
    for i, ch in enumerate(seen):
        assert isinstance(ch, AnalogInputChannel)
        assert ch.index == i


def test_analog_inputs_getitem_returns_channel_by_index(
    nyquist_device: NyquistDevice,
) -> None:
    """``analog_inputs[i]`` returns the same instance for the same
    index (collection holds the channels — no per-call construction)
    and out-of-range indices raise ``IndexError``."""
    inputs = nyquist_device.analog_inputs
    last = len(inputs) - 1
    first = inputs[0]
    same_first = inputs[0]
    assert first is same_first
    assert first.index == 0
    assert inputs[last].index == last
    with pytest.raises(IndexError):
        _ = inputs[len(inputs)]


# ---- Per-channel GET — bitmask round trip via the production getter --------


def test_per_channel_enabled_getter_reads_bitmask_correctly(
    nyquist_device: NyquistDevice,
) -> None:
    """The per-channel ``enabled`` getter
    (``CONFigure:ADC:CHANnel? <i>``) MUST agree with the firmware
    bitmask (``ENABle:VOLTage:DC?``) for every channel. Driven by
    raw-SCPI bitmask writes because the production setter is broken
    (see module docstring); validates the GET path end-to-end.

    Pattern picked to catch off-by-one / endianness drift: enable
    bits 3 + 5 + 7 (non-adjacent, asymmetric), then assert each of
    the 16 channel getters lines up with the corresponding bit."""
    inputs = nyquist_device.analog_inputs
    target = (1 << 3) | (1 << 5) | (1 << 7)
    _write_firmware_bitmask(nyquist_device, target)
    assert _read_firmware_bitmask(nyquist_device) == target

    n = len(inputs)
    target_within_collection = target & ((1 << n) - 1)
    for i in range(n):
        expected = bool(target_within_collection & (1 << i))
        actual = inputs[i].enabled
        assert actual is expected, (
            f"channel {i}: getter returned {actual!r}, "
            f"expected {expected!r} from bitmask 0x{target:X}"
        )


def test_per_channel_enabled_getter_tracks_bitmask_changes(
    nyquist_device: NyquistDevice,
) -> None:
    """Re-reading the per-channel getter after a bitmask change must
    return the new value — no stale caching in the getter."""
    inputs = nyquist_device.analog_inputs
    _write_firmware_bitmask(nyquist_device, 0)
    assert inputs[0].enabled is False
    assert inputs[1].enabled is False

    _write_firmware_bitmask(nyquist_device, 0x3)  # bits 0+1
    assert inputs[0].enabled is True
    assert inputs[1].enabled is True

    _write_firmware_bitmask(nyquist_device, 0x2)  # bit 1 only
    assert inputs[0].enabled is False
    assert inputs[1].enabled is True


def test_disable_all_after_bitmask_write_reflects_in_getter(
    nyquist_device: NyquistDevice,
) -> None:
    """After a bulk raw-SCPI bitmask reset, the per-channel getter
    reports ``False`` for every channel."""
    inputs = nyquist_device.analog_inputs
    full = (1 << len(inputs)) - 1
    _write_firmware_bitmask(nyquist_device, full)
    assert _read_firmware_bitmask(nyquist_device) == full
    assert all(inputs[i].enabled for i in range(len(inputs)))

    _write_firmware_bitmask(nyquist_device, 0)
    assert _read_firmware_bitmask(nyquist_device) == 0
    assert not any(inputs[i].enabled for i in range(len(inputs)))


# ---- Per-channel SET — currently broken upstream (see module docstring) ----


@pytest.mark.xfail(
    reason=(
        "AnalogInputChannel.enabled setter sends malformed SCPI "
        "(space-separated `CONFigure:ADC:CHANnel <ch> <val>` instead "
        "of comma-separated `<ch>,<val>`); firmware silently drops "
        "it AND SerialInterface.send_set's response check races on "
        "the SYST:ERR? round-trip. Production-code fix is out of "
        "scope for this tests-only PR."
    ),
    strict=True,
)
def test_per_channel_enabled_setter_reflects_in_firmware_bitmask(
    nyquist_device: NyquistDevice,
) -> None:
    """Setting ``analog_inputs[i].enabled = True`` SHOULD set bit
    ``i`` in the firmware bitmask. Currently broken — see xfail."""
    inputs = nyquist_device.analog_inputs
    _write_firmware_bitmask(nyquist_device, 0)
    assert _read_firmware_bitmask(nyquist_device) == 0
    inputs[3].enabled = True
    inputs[7].enabled = True
    time.sleep(0.05)
    bitmask = _read_firmware_bitmask(nyquist_device)
    assert bitmask == (1 << 3) | (1 << 7), (
        f"unexpected firmware bitmask after per-channel writes: "
        f"0x{bitmask:X}"
    )


@pytest.mark.xfail(
    reason="depends on the broken per-channel enabled setter (see above)",
    strict=True,
)
def test_enable_all_sets_full_bitmask(nyquist_device: NyquistDevice) -> None:
    """``enable_all()`` SHOULD flip every per-channel bit on. Currently
    broken — bulk wrapper iterates the broken per-channel setter."""
    inputs = nyquist_device.analog_inputs
    _write_firmware_bitmask(nyquist_device, 0)
    inputs.enable_all()
    time.sleep(0.1)
    expected = (1 << len(inputs)) - 1
    assert _read_firmware_bitmask(nyquist_device) == expected


@pytest.mark.xfail(
    reason="depends on the broken per-channel enabled setter (see above)",
    strict=True,
)
def test_disable_all_clears_bitmask(nyquist_device: NyquistDevice) -> None:
    """``disable_all()`` SHOULD drive the firmware bitmask to 0.
    Currently broken — bulk wrapper iterates the broken per-channel
    setter."""
    inputs = nyquist_device.analog_inputs
    full = (1 << len(inputs)) - 1
    _write_firmware_bitmask(nyquist_device, full)
    assert _read_firmware_bitmask(nyquist_device) == full
    inputs.disable_all()
    time.sleep(0.1)
    assert _read_firmware_bitmask(nyquist_device) == 0


# ---- Range — variant-aware -------------------------------------------------


def test_range_getter_returns_FIXED_on_nq1(
    nyquist_device: NyquistDevice,
) -> None:
    """NQ1 has the MC12bADC, which has a single fixed input range.
    The ``range`` getter MUST short-circuit to the literal
    ``"FIXED"`` rather than issuing the unsupported SCPI query."""
    if nyquist_device.variant != BoardVariant.NQ1:
        pytest.skip(
            f"NQ1-only test (variant={nyquist_device.variant.value})"
        )
    assert nyquist_device.analog_inputs[0].range == "FIXED"


def test_range_setter_raises_on_nq1(
    nyquist_device: NyquistDevice,
) -> None:
    """NQ1 must reject any range write with
    ``FeatureNotSupportedError`` — and it must fail before issuing
    the SCPI command, so the firmware error queue stays clean."""
    if nyquist_device.variant != BoardVariant.NQ1:
        pytest.skip(
            f"NQ1-only test (variant={nyquist_device.variant.value})"
        )
    with pytest.raises(FeatureNotSupportedError):
        nyquist_device.analog_inputs[0].range = "10V"
    # No SCPI command was sent → no firmware error generated.
    # Strict structured match against the canonical reply shape
    # ``0,"No error"`` so a stray ``"No error in stream"`` substring
    # in the read window can't false-positive (matches the bytes-side
    # regex pattern in tests/integration/conftest.py).
    err = nyquist_device.get_last_error()
    assert _SCPI_NO_ERROR_STR_RE.search(err), (
        f"expected canonical 0,\"No error\" reply after rejected "
        f"range setter, got {err!r}"
    )


def test_range_setter_round_trip_on_nq3(
    nyquist_device: NyquistDevice,
) -> None:
    """NQ3 supports module-level range configuration (AD7609). The
    setter must round-trip the supported aliases: ``"5V"``/``"10V"``
    (and the explicit ``"0"``/``"1"`` numeric form). Skipped when
    the test rig isn't an NQ3."""
    if nyquist_device.variant != BoardVariant.NQ3:
        pytest.skip(
            f"NQ3-only test (variant={nyquist_device.variant.value})"
        )
    inputs = nyquist_device.analog_inputs
    inputs[0].range = "10V"
    time.sleep(0.05)
    assert inputs[0].range == "1"
    inputs[0].range = "5V"
    time.sleep(0.05)
    assert inputs[0].range == "0"


# ---- configure_channels ----------------------------------------------------


def test_configure_channels_raises_on_nq1_due_to_range_setter(
    nyquist_device: NyquistDevice,
) -> None:
    """``configure_channels`` writes ``range`` first — so on NQ1 it
    propagates the same ``FeatureNotSupportedError`` the standalone
    range setter raises, and the targeted channels stay un-enabled.

    NOTE: This documents a real production limitation —
    ``configure_channels`` is unusable on NQ1 even when the caller
    only cares about the ``enabled`` arg. Combined with the broken
    per-channel ``enabled`` setter (see module docstring), the bulk
    bulk-config path is fully unusable on NQ1 today. Logged as a
    follow-up production fix; tests-only PR scope keeps it out of
    this slice."""
    if nyquist_device.variant != BoardVariant.NQ1:
        pytest.skip(
            f"NQ1-only test (variant={nyquist_device.variant.value})"
        )
    inputs = nyquist_device.analog_inputs
    _write_firmware_bitmask(nyquist_device, 0)
    with pytest.raises(FeatureNotSupportedError):
        inputs.configure_channels([0, 1, 2], range="10V", enabled=True)
    # Channels stayed un-enabled — the failed range write short-
    # circuited before reaching the (broken) enabled write.
    bitmask = _read_firmware_bitmask(nyquist_device)
    assert bitmask & 0x7 == 0, (
        f"configure_channels left bits 0..2 set on NQ1 despite "
        f"raising; bitmask=0x{bitmask:X}"
    )


@pytest.mark.xfail(
    reason=(
        "configure_channels on NQ3 depends on the broken per-channel "
        "enabled setter (see module docstring); range write succeeds "
        "but the enabled write silently fails and the bitmask stays 0."
    ),
    strict=True,
)
def test_configure_channels_enables_listed_channels_on_nq3(
    nyquist_device: NyquistDevice,
) -> None:
    """On NQ3 the bulk path SHOULD write both range AND enabled for
    each listed channel. Currently broken on the enabled half (see
    xfail). Skipped when the test rig isn't an NQ3."""
    if nyquist_device.variant != BoardVariant.NQ3:
        pytest.skip(
            f"NQ3-only test (variant={nyquist_device.variant.value})"
        )
    inputs = nyquist_device.analog_inputs
    _write_firmware_bitmask(nyquist_device, 0)
    targets = [1, 3, 5]
    inputs.configure_channels(targets, range="10V", enabled=True)
    time.sleep(0.1)
    bitmask = _read_firmware_bitmask(nyquist_device)
    expected = sum(1 << i for i in targets)
    assert bitmask == expected, (
        f"configure_channels({targets}) left firmware bitmask at "
        f"0x{bitmask:X} (expected 0x{expected:X})"
    )


# ---- Channel.index — round out the public surface --------------------------


def test_analog_input_channel_index_matches_position(
    nyquist_device: NyquistDevice,
) -> None:
    """``AnalogInputChannel.index`` is the constructor arg and stays
    constant — useful as a positional identity check when iterating
    the collection."""
    for i, ch in enumerate(nyquist_device.analog_inputs):
        assert ch.index == i, (
            f"channel at position {i} reports index={ch.index!r}"
        )
