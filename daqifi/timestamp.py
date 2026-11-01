"""Stateful device-timestamp processing with uint32 rollover handling.

Port of `Daqifi.Core.Device.TimestampProcessor` and `TimestampResult`.

Device timestamps are 32-bit unsigned cycle counters. At 50 MHz
(20 ns tick) the counter rolls over every ~85.9 seconds, so the
processor maintains per-device state so that streaming code can
recover monotonic system timestamps across rollover events.

A 10-second sanity check guards against false-positive rollover
detection caused by out-of-order messages: if applying the
rollover formula yields >10 s between messages, the rollover is
treated as a backwards-in-time event instead and reported as
negative `seconds_between_messages`.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Optional


__all__ = ["TimestampResult", "TimestampProcessor"]


_UINT32_MAX = 0xFFFFFFFF
_DEFAULT_TIMESTAMP_FREQUENCY = 50_000_000
_DEFAULT_TICK_PERIOD = 20e-9
_MAX_ROLLOVER_TIME_BETWEEN_MESSAGES = 10.0


@dataclass(frozen=True)
class TimestampResult:
    """Result of processing one device timestamp."""

    timestamp: datetime
    was_rollover: bool
    clock_cycles_between_messages: int
    seconds_between_messages: float
    is_first_message: bool


class _DeviceTimestampState:
    __slots__ = (
        "lock",
        "previous_system_timestamp",
        "previous_device_timestamp",
        "has_previous_timestamp",
        "tick_period",
        "base_system_timestamp",
        "base_tick_period",
        "elapsed_ticks",
    )

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.previous_system_timestamp: datetime = datetime.min
        self.previous_device_timestamp: int = 0
        self.has_previous_timestamp: bool = False
        # The device's own tick period (from its timestamp_freq), or None
        # for the processor's fallback. On the state, under its lock, so it
        # can't fall out of step with the previous timestamp it's applied
        # to, and goes when the state does.
        self.tick_period: Optional[float] = None
        # Each timestamp is a base plus the signed whole ticks since it,
        # converted once: neither timedelta's microsecond rounding nor a
        # float running sum accumulates step by step (#163 item 11). A
        # period change moves the base, so earlier time isn't rescaled.
        self.base_system_timestamp: datetime = datetime.min
        self.base_tick_period: float = 0.0
        self.elapsed_ticks: int = 0

    def clear_timestamps(self) -> None:
        self.previous_system_timestamp = datetime.min
        self.previous_device_timestamp = 0
        self.has_previous_timestamp = False


class TimestampProcessor:
    """Thread-safe processor that converts device clock cycles to wall time.

    State is keyed by `device_id`, so a single processor instance
    can multiplex any number of devices.

    The tick period defaults to 20 ns (50 MHz), but should be set per
    device from its reported ``timestamp_freq`` (the protobuf status
    field, :attr:`DaqifiDevice.timestamp_frequency`) with
    :meth:`set_timestamp_frequency`: boards don't all run at 50 MHz.
    A device without one uses the processor-wide :attr:`tick_period`
    (upstream 6f9677d).
    """

    DEFAULT_TICK_PERIOD: float = _DEFAULT_TICK_PERIOD
    DEFAULT_TIMESTAMP_FREQUENCY: int = _DEFAULT_TIMESTAMP_FREQUENCY

    def __init__(self, tick_period: float = _DEFAULT_TICK_PERIOD) -> None:
        if tick_period <= 0:
            raise ValueError("Tick period must be greater than zero.")
        self._tick_period = tick_period
        self._device_states: Dict[str, _DeviceTimestampState] = {}
        # Guards _device_states dict insertion/deletion. Per-device
        # processing locks live on each state object.
        self._states_lock = threading.Lock()

    @property
    def tick_period(self) -> float:
        """The fallback tick period, for devices with no frequency set."""
        return self._tick_period

    def set_timestamp_frequency(self, device_id: str, frequency_hz: int) -> None:
        """Use ``1 / frequency_hz`` as ``device_id``'s tick period from now
        on (the device-reported ``timestamp_freq``). 0 clears it, back to
        the fallback (an older firmware that doesn't report one)."""
        if device_id is None:
            raise TypeError("device_id must not be None")
        if isinstance(frequency_hz, bool) or not isinstance(frequency_hz, int):
            raise TypeError(f"frequency_hz must be an int (got {frequency_hz!r})")
        if frequency_hz < 0:
            raise ValueError(f"frequency_hz must not be negative (got {frequency_hz})")
        # On the device's state, under its lock: a frequency change can't
        # land between a process_timestamp's reading of the period and its
        # use of the previous timestamp.
        while True:
            state = self._get_or_add_state(device_id)
            with state.lock:
                # reset() may have dropped this state since we got it; the
                # frequency must go on the device's current one.
                if self._device_states.get(device_id) is not state:
                    continue
                state.tick_period = None if frequency_hz == 0 else 1.0 / frequency_hz
                return

    def get_tick_period(self, device_id: str) -> float:
        """The tick period ``device_id``'s timestamps are converted with."""
        if device_id is None:
            raise TypeError("device_id must not be None")
        state = self._device_states.get(device_id)
        return self._tick_period_of(state) if state is not None else self._tick_period

    def _tick_period_of(self, state: _DeviceTimestampState) -> float:
        # One read: get_tick_period calls this without the lock, and a
        # concurrent clear between two reads would return None.
        period = state.tick_period
        return period if period is not None else self._tick_period

    def process_timestamp(self, device_id: str, device_timestamp: int) -> TimestampResult:
        """Process one device timestamp and compute its system-clock equivalent.

        Args:
            device_id: Unique identifier (e.g. serial number) — used to
                key per-device state. Must not be `None`.
            device_timestamp: Raw 32-bit unsigned device clock cycle count.

        Returns:
            A `TimestampResult` describing the message.

        Raises:
            TypeError: if `device_id` is `None`.
        """
        if device_id is None:
            raise TypeError("device_id must not be None")

        state = self._get_or_add_state(device_id)

        with state.lock:
            # From the state this lock guards, with the previous timestamp
            # (set_timestamp_frequency writes it under this lock too).
            tick_period = self._tick_period_of(state)
            if not state.has_previous_timestamp:
                now = datetime.now()
                state.previous_system_timestamp = now
                state.previous_device_timestamp = device_timestamp
                state.has_previous_timestamp = True
                state.base_system_timestamp = now
                state.base_tick_period = tick_period
                state.elapsed_ticks = 0
                return TimestampResult(
                    timestamp=now,
                    was_rollover=False,
                    clock_cycles_between_messages=0,
                    seconds_between_messages=0.0,
                    is_first_message=True,
                )

            previous_device_timestamp = state.previous_device_timestamp
            rollover = previous_device_timestamp > device_timestamp

            if rollover:
                # +1 for the wrap tick from 0xFFFFFFFF → 0 itself, which
                # would otherwise be lost: e.g. previous=MAX, current=0
                # is one cycle, not zero. Without the +1, gaps that
                # straddle the wrap are reported one tick short.
                cycles_to_max = _UINT32_MAX - previous_device_timestamp + 1
                clock_cycles_between_messages = cycles_to_max + device_timestamp
            else:
                clock_cycles_between_messages = device_timestamp - previous_device_timestamp

            seconds_between_messages = clock_cycles_between_messages * tick_period

            # Sanity check: a "rollover" that implies > 10 s gap is more
            # likely an out-of-order message — treat it as moving backwards
            # and downgrade the rollover flag so the result reflects what
            # the sanity check decided, not the initial classification.
            if rollover and seconds_between_messages > _MAX_ROLLOVER_TIME_BETWEEN_MESSAGES:
                rollover = False
                clock_cycles_between_messages = previous_device_timestamp - device_timestamp
                seconds_between_messages = clock_cycles_between_messages * tick_period * -1

            if tick_period != state.base_tick_period:
                state.base_system_timestamp = state.previous_system_timestamp
                state.base_tick_period = tick_period
                state.elapsed_ticks = 0
            # From the base and the exact tick total, not the previous
            # (already rounded) timestamp plus this step.
            state.elapsed_ticks += (
                -clock_cycles_between_messages if seconds_between_messages < 0
                else clock_cycles_between_messages
            )
            message_timestamp = state.base_system_timestamp + timedelta(
                seconds=state.elapsed_ticks * tick_period
            )

            state.previous_system_timestamp = message_timestamp
            state.previous_device_timestamp = device_timestamp

            return TimestampResult(
                timestamp=message_timestamp,
                was_rollover=rollover,
                clock_cycles_between_messages=clock_cycles_between_messages,
                seconds_between_messages=seconds_between_messages,
                is_first_message=False,
            )

    def reset(self, device_id: str) -> None:
        """Forget a device's timestamps, so its next one is a first message.
        No-op if unknown. Its frequency is kept: that's static device
        configuration, not per-session state. A device with no frequency
        set is dropped entirely, so only configured devices are retained."""
        if device_id is None:
            raise TypeError("device_id must not be None")
        state = self._device_states.get(device_id)
        if state is None:
            return
        with state.lock:
            if state.tick_period is not None:
                state.clear_timestamps()
                return
            with self._states_lock:
                if self._device_states.get(device_id) is state:
                    del self._device_states[device_id]

    def reset_all(self) -> None:
        """Drop the state of every tracked device, frequencies included. A
        call already holding a device's lock finishes on its old state, its
        frequency included; the next starts fresh."""
        with self._states_lock:
            self._device_states.clear()

    def _get_or_add_state(self, device_id: str) -> _DeviceTimestampState:
        # Fast path: state already exists. Dict reads are atomic in CPython
        # but the insert path needs the lock to avoid duplicate inserts.
        state = self._device_states.get(device_id)
        if state is not None:
            return state
        with self._states_lock:
            state = self._device_states.get(device_id)
            if state is None:
                state = _DeviceTimestampState()
                self._device_states[device_id] = state
            return state
