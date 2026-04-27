"""B8 — ``daqifi.channels`` digital-I/O slice.

Exercises the DIO surface against real hardware:

* ``DigitalIO`` collection ops — ``__getitem__`` / ``__len__`` /
  ``__iter__`` round-trip the 16 DIO channels populated by
  :meth:`NyquistDevice._initialize_channels`.
* ``DigitalIOChannel.direction`` GET — agrees with the
  firmware-side ``DIO:PORt:DIRection?`` mask after a raw bulk-mask
  write.
* ``DigitalIOChannel.direction`` SET — round-trips through the
  per-channel property.
* ``DigitalIO.configure_outputs`` / ``configure_inputs`` — bulk
  wrappers around the per-channel direction setter.
* ``DigitalIOChannel.state`` GET — agrees with
  ``DIO:PORt:STATe?`` mask after a raw bulk-state write.
* ``DigitalIOChannel.state`` SET on an output channel round-trips;
  on an input channel raises ``ConfigurationError`` BEFORE any
  SCPI write hits the wire.
* ``DigitalIO.set_states`` — bulk state writes settle to the
  expected port mask.
* ``DigitalIO.read_all`` — returns ``{channel_index: state}`` for
  every channel; the dict is a fresh snapshot (caller mutation
  doesn't affect the next read).

Hardware source-of-truth: the firmware bitmask via the raw SCPI
queries ``DIO:PORt:DIRection?`` and ``DIO:PORt:STATe?``. Same
bitmask-vs-per-channel-SCPI equivalence as ADC enable — the
firmware has both surfaces but the bitmask is what the rest of
the system reads from.

Hardware safety: tests configure every DIO line as either input
(high-Z, harmless) or short output pulses on the unloaded headers.
The teardown forces every line back to ``input`` so a later test
session never inherits driven outputs that could fight whatever
the user wires up next.
"""
from __future__ import annotations

import time
from typing import Iterator

import pytest

from daqifi._internal import scpi
from daqifi.channels import DigitalIOChannel
from daqifi.device import NyquistDevice
from daqifi.discovery import BoardVariant, DeviceInfo
from daqifi.exceptions import ConfigurationError


# ---- Local fixture ---------------------------------------------------------

# Mirrors B7's `nyquist_device` fixture — can't reuse `clean_device` /
# `serial_transport` because those open a SerialStreamTransport that
# holds the COM port exclusively, and `NyquistDevice` opens its own
# SerialInterface against the same port.


def _wake_and_drain(dev: NyquistDevice) -> None:
    """Bring the device into a state where DIO writes will be
    accepted — wake from Standby and drain any stale SCPI errors."""
    dev.power_up()
    dev.clear_errors()


def _read_direction_mask(dev: NyquistDevice) -> int:
    """Return the current DIO direction mask as the firmware reports
    it (1=output, 0=input). Used as the load-bearing assertion target
    for direction tests."""
    response = dev._comm.send_query(scpi.DigitalIO.PORT_DIRECTION_QUERY)
    return scpi.parse_int(response)


def _read_state_mask(dev: NyquistDevice) -> int:
    """Return the current DIO state mask (1=high, 0=low)."""
    response = dev._comm.send_query(scpi.DigitalIO.PORT_STATE_QUERY)
    return scpi.parse_int(response)


def _await_direction_mask(
    dev: NyquistDevice,
    expected: int,
    *,
    timeout: float = 1.0,
    poll_interval: float = 0.02,
) -> None:
    """Poll until the direction mask matches ``expected`` or
    ``timeout`` elapses. Same shape as B7's bitmask waiter — handles
    USB CDC scheduling jitter and transient parse hiccups without
    false-failing on the next read."""
    start = time.monotonic()
    last_actual: int | None = None
    last_exc: Exception | None = None
    while True:
        try:
            last_actual = _read_direction_mask(dev)
            last_exc = None
            if last_actual == expected:
                return
        except Exception as e:
            last_exc = e
        if (time.monotonic() - start) >= timeout:
            tail = (
                f"; last exception was {last_exc!r}"
                if last_exc is not None
                else f"; last read was 0x{last_actual:X}"
            )
            raise AssertionError(
                f"direction mask did not settle to 0x{expected:X} "
                f"within {timeout:g}s{tail}"
            )
        time.sleep(poll_interval)


def _await_state_mask(
    dev: NyquistDevice,
    expected: int,
    *,
    timeout: float = 1.0,
    poll_interval: float = 0.02,
) -> None:
    """Same as :func:`_await_direction_mask` but for the state mask."""
    start = time.monotonic()
    last_actual: int | None = None
    last_exc: Exception | None = None
    while True:
        try:
            last_actual = _read_state_mask(dev)
            last_exc = None
            if last_actual == expected:
                return
        except Exception as e:
            last_exc = e
        if (time.monotonic() - start) >= timeout:
            tail = (
                f"; last exception was {last_exc!r}"
                if last_exc is not None
                else f"; last read was 0x{last_actual:X}"
            )
            raise AssertionError(
                f"state mask did not settle to 0x{expected:X} "
                f"within {timeout:g}s{tail}"
            )
        time.sleep(poll_interval)


def _write_direction_mask(dev: NyquistDevice, value: int) -> None:
    """Set the DIO direction mask via raw SCPI command, blocking until
    the firmware confirms the new value. Used to set up GET-test
    preconditions independently of the production per-channel setter."""
    dev._comm.send_command(
        f"{scpi.DigitalIO.PORT_DIRECTION_SET} {value}", delay=0.2
    )
    _await_direction_mask(dev, value)


def _write_state_mask(dev: NyquistDevice, value: int) -> None:
    """Set the DIO state mask via raw SCPI command, blocking until the
    firmware confirms the new value."""
    dev._comm.send_command(
        f"{scpi.DigitalIO.PORT_STATE_SET} {value}", delay=0.2
    )
    _await_state_mask(dev, value)


@pytest.fixture
def nyquist_device(device_info: DeviceInfo) -> Iterator[NyquistDevice]:
    """Open a NyquistDevice on the discovered port. Yields a connected
    device with errors drained and the DIO port reset to all-inputs
    so each test starts from a known-clean baseline. Teardown
    restores the same all-inputs baseline so a later session doesn't
    inherit driven outputs.

    Setup baseline asserts on success — if we can't reliably reset
    the direction mask, the module's safety guarantee (no driven
    outputs survive teardown) is unverifiable and tests must not run.
    Teardown re-raises any reset failure AFTER disconnect so the
    operator sees the safety-reset failure even when a test passed."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = NyquistDevice.connect(port.strip())
    setup_failed = False
    try:
        _wake_and_drain(dev)
        # Setup baseline: all inputs (high-Z, harmless). State mask
        # is meaningless for inputs (their state read reflects live
        # pin values, not the output register) so we don't normalize
        # it — leaving direction=0 alone is enough to make every pin
        # safe.
        _write_direction_mask(dev, 0)
        yield dev
    except Exception:
        setup_failed = True
        raise
    finally:
        teardown_exc: Exception | None = None
        # Restore safe baseline. We surface the failure rather than
        # swallow it — driven outputs on a "clean" device are a
        # safety hazard, not a flake.
        if not setup_failed:
            try:
                _write_direction_mask(dev, 0)
            except Exception as e:
                teardown_exc = e
        try:
            dev.disconnect()
        except Exception:
            pass
        if teardown_exc is not None:
            pytest.fail(
                f"DIO safety-reset FAILED in teardown: {teardown_exc!r}. "
                f"Device may have driven outputs — physically unplug "
                f"before re-running."
            )


# ---- Collection ops --------------------------------------------------------


def test_digital_io_collection_size_is_sixteen(
    nyquist_device: NyquistDevice,
) -> None:
    """All Nyquist variants have 16 DIO channels (per
    ``_initialize_channels`` and the protobuf
    ``DeviceCapabilities.digital_channels`` value)."""
    assert len(nyquist_device.digital_io) == 16


def test_digital_io_iter_yields_indexed_channels(
    nyquist_device: NyquistDevice,
) -> None:
    """Iterating yields ``DigitalIOChannel`` instances with strictly
    ascending ``index`` values starting at 0."""
    seen = list(nyquist_device.digital_io)
    assert len(seen) == 16
    for i, ch in enumerate(seen):
        assert isinstance(ch, DigitalIOChannel)
        assert ch.index == i


def test_digital_io_getitem_returns_channel_by_index(
    nyquist_device: NyquistDevice,
) -> None:
    """``digital_io[i]`` returns the same instance for the same index
    (collection holds the channels — no per-call construction); out-of-
    range indices raise ``IndexError``."""
    dio = nyquist_device.digital_io
    first = dio[0]
    same_first = dio[0]
    assert first is same_first
    assert first.index == 0
    assert dio[15].index == 15
    with pytest.raises(IndexError):
        _ = dio[16]


# ---- Direction GET — bitmask round trip via the production getter ----------


def test_per_channel_direction_getter_reads_mask_correctly(
    nyquist_device: NyquistDevice,
) -> None:
    """The per-channel ``direction`` getter
    (``DIO:PORt:DIRection?`` + bit-test) MUST agree with the
    firmware mask for every channel. Driven by raw-SCPI mask writes
    so a future regression in the per-channel setter doesn't mask
    a getter regression too."""
    dev = nyquist_device
    # Pattern picked to exercise both polarities on every byte: 0x5AA5
    # = 0101 1010 1010 0101 — channels 0,2,5,7,10,12,13,14 = output.
    target_mask = 0x5AA5
    _write_direction_mask(dev, target_mask)

    for ch in dev.digital_io:
        expected = "output" if (target_mask & (1 << ch.index)) else "input"
        assert ch.direction == expected, (
            f"channel {ch.index}: expected {expected!r}, got {ch.direction!r}"
        )


# ---- Direction SET — per-channel round trip --------------------------------


def test_per_channel_direction_setter_round_trips_via_mask(
    nyquist_device: NyquistDevice,
) -> None:
    """Setting ``direction = "output"`` on each channel in turn flips
    exactly one bit in the firmware mask. Validates the bit-modify
    path in :meth:`DigitalIOChannel.direction.setter`."""
    dev = nyquist_device
    _write_direction_mask(dev, 0)

    expected_mask = 0
    for ch in dev.digital_io:
        ch.direction = "output"
        expected_mask |= (1 << ch.index)
        _await_direction_mask(dev, expected_mask)

    # Now flip them all back to input and verify the inverse path.
    for ch in dev.digital_io:
        ch.direction = "input"
        expected_mask &= ~(1 << ch.index)
        _await_direction_mask(dev, expected_mask)


def test_direction_setter_rejects_invalid_value(
    nyquist_device: NyquistDevice,
) -> None:
    """``direction`` must be ``"input"`` or ``"output"`` (case-
    insensitive). Anything else raises ``ConfigurationError``
    BEFORE any SCPI write, so the firmware mask is unchanged."""
    dev = nyquist_device
    _write_direction_mask(dev, 0)
    ch = dev.digital_io[0]

    # Tight exception type — the production setter promises
    # ConfigurationError specifically. A broader Exception would
    # silently accept a future regression that swaps it for, say,
    # CommandError.
    with pytest.raises(ConfigurationError):
        ch.direction = "bidirectional"

    # Mask should still be 0 — invalid value rejected before write.
    assert _read_direction_mask(dev) == 0


# ---- Bulk configure_outputs / configure_inputs -----------------------------


def test_configure_outputs_bulk_writes_match_mask(
    nyquist_device: NyquistDevice,
) -> None:
    """``configure_outputs([...])`` flips exactly the supplied
    channels to output; everything else stays input."""
    dev = nyquist_device
    _write_direction_mask(dev, 0)

    target_channels = [0, 3, 7, 15]
    dev.digital_io.configure_outputs(target_channels)

    expected_mask = 0
    for ch in target_channels:
        expected_mask |= (1 << ch)
    _await_direction_mask(dev, expected_mask)


def test_configure_inputs_bulk_clears_mask_bits(
    nyquist_device: NyquistDevice,
) -> None:
    """``configure_inputs([...])`` clears exactly the supplied
    channels back to input; channels not in the list keep their
    prior state."""
    dev = nyquist_device
    # Start with all outputs.
    _write_direction_mask(dev, 0xFFFF)

    dev.digital_io.configure_inputs([0, 3, 7, 15])
    expected_mask = 0xFFFF & ~((1 << 0) | (1 << 3) | (1 << 7) | (1 << 15))
    _await_direction_mask(dev, expected_mask)


# ---- State GET — bitmask round trip ---------------------------------------


def test_per_channel_state_getter_reads_mask_correctly(
    nyquist_device: NyquistDevice,
) -> None:
    """The per-channel ``state`` getter agrees with the firmware
    state mask for every channel. Drives via a raw-SCPI mask write
    on outputs so the GET reads the asserted state, not whatever the
    floating input lines happen to read as."""
    dev = nyquist_device
    # All channels as outputs so state reads are deterministic
    # (input state depends on what's wired up, which the test rig
    # makes no claim about).
    _write_direction_mask(dev, 0xFFFF)

    target_state = 0xA5A5  # alternating high/low pattern
    _write_state_mask(dev, target_state)

    for ch in dev.digital_io:
        expected = bool(target_state & (1 << ch.index))
        assert ch.state == expected, (
            f"channel {ch.index}: expected state={expected}, got {ch.state}"
        )


# ---- State SET on output ---------------------------------------------------


def test_per_channel_state_setter_on_output_round_trips(
    nyquist_device: NyquistDevice,
) -> None:
    """``state = True`` on an output channel raises that pin high in
    the firmware state mask; ``state = False`` lowers it."""
    dev = nyquist_device
    _write_direction_mask(dev, 0xFFFF)
    _write_state_mask(dev, 0)

    ch = dev.digital_io[5]
    ch.state = True
    _await_state_mask(dev, 1 << 5)
    ch.state = False
    _await_state_mask(dev, 0)


# ---- State SET on input — must raise ConfigurationError --------------------


def test_per_channel_state_setter_on_input_raises_configuration_error(
    nyquist_device: NyquistDevice,
) -> None:
    """Trying to set state on a channel that's configured as input
    raises ``ConfigurationError`` BEFORE any state-write SCPI hits
    the wire.

    Setup: every channel as OUTPUT (deterministic state register),
    target channel switched to INPUT just before the attempted
    write. We compare the state mask masking out the target bit so
    the floating input pin's live reading doesn't poison the
    deterministic comparison (input bits in ``DIO:PORt:STATe?``
    reflect the live pin, not the output register, so direct
    equality on a port-wide mask while any bit is an input is
    inherently flaky)."""
    dev = nyquist_device
    # All outputs with a known non-trivial state pattern, so the bits
    # we DON'T touch stay deterministic across the read window.
    _write_direction_mask(dev, 0xFFFF)
    _write_state_mask(dev, 0xA5A5)

    target_idx = 2
    target_bit = 1 << target_idx
    other_bits_mask = 0xFFFF & ~target_bit

    # Switch only the target channel to input; the rest stay outputs
    # so their state-register bits remain deterministic.
    dev.digital_io[target_idx].direction = "input"
    assert dev.digital_io[target_idx].direction == "input"

    before_other = _read_state_mask(dev) & other_bits_mask
    with pytest.raises(ConfigurationError):
        dev.digital_io[target_idx].state = True
    after_other = _read_state_mask(dev) & other_bits_mask
    assert after_other == before_other, (
        "state mask changed on the non-target output channels despite "
        f"ConfigurationError: before=0x{before_other:X} "
        f"after=0x{after_other:X}"
    )


# ---- Bulk set_states / read_all -------------------------------------------


def test_set_states_bulk_writes_match_mask(
    nyquist_device: NyquistDevice,
) -> None:
    """``set_states({i: bool, ...})`` settles to the expected port
    mask. Channels not in the dict keep their prior state."""
    dev = nyquist_device
    _write_direction_mask(dev, 0xFFFF)
    _write_state_mask(dev, 0)

    dev.digital_io.set_states({0: True, 3: True, 7: False, 15: True})
    expected_mask = (1 << 0) | (1 << 3) | (1 << 15)
    _await_state_mask(dev, expected_mask)


def test_read_all_returns_per_channel_dict(
    nyquist_device: NyquistDevice,
) -> None:
    """``read_all()`` returns ``{channel_index: state}`` for every
    one of the 16 channels, agreeing with the firmware state mask."""
    dev = nyquist_device
    _write_direction_mask(dev, 0xFFFF)
    target_state = 0x1234
    _write_state_mask(dev, target_state)

    states = dev.digital_io.read_all()
    assert isinstance(states, dict)
    assert sorted(states.keys()) == list(range(16))
    for i in range(16):
        expected = bool(target_state & (1 << i))
        assert states[i] is expected, (
            f"channel {i}: expected {expected}, got {states[i]}"
        )


def test_read_all_returns_fresh_snapshot_per_call(
    nyquist_device: NyquistDevice,
) -> None:
    """Two consecutive ``read_all()`` calls return independent dicts
    — caller mutation of the first must not poison the second."""
    dev = nyquist_device
    _write_direction_mask(dev, 0xFFFF)
    _write_state_mask(dev, 0)

    snap1 = dev.digital_io.read_all()
    snap1[0] = True  # mutate caller's copy

    snap2 = dev.digital_io.read_all()
    assert snap2[0] is False, (
        "second read_all() inherited caller mutation from first — "
        "the dict is not a defensive snapshot"
    )
