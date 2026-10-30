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
from typing import Dict


__all__ = ["TimestampResult", "TimestampProcessor"]


_UINT32_MAX = 0xFFFFFFFF
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
        "first_system_timestamp",
        "elapsed_seconds",
    )

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.previous_system_timestamp: datetime = datetime.min
        self.previous_device_timestamp: int = 0
        self.has_previous_timestamp: bool = False
        # Each timestamp is the first one plus the exact elapsed time, so
        # timedelta's whole-microsecond rounding doesn't accumulate step by
        # step (#163 item 11).
        self.first_system_timestamp: datetime = datetime.min
        self.elapsed_seconds: float = 0.0


class TimestampProcessor:
    """Thread-safe processor that converts device clock cycles to wall time.

    State is keyed by `device_id`, so a single processor instance
    can multiplex any number of devices.
    """

    DEFAULT_TICK_PERIOD: float = _DEFAULT_TICK_PERIOD

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
        return self._tick_period

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
            if not state.has_previous_timestamp:
                now = datetime.now()
                state.previous_system_timestamp = now
                state.previous_device_timestamp = device_timestamp
                state.has_previous_timestamp = True
                state.first_system_timestamp = now
                state.elapsed_seconds = 0.0
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

            seconds_between_messages = clock_cycles_between_messages * self._tick_period

            # Sanity check: a "rollover" that implies > 10 s gap is more
            # likely an out-of-order message — treat it as moving backwards
            # and downgrade the rollover flag so the result reflects what
            # the sanity check decided, not the initial classification.
            if rollover and seconds_between_messages > _MAX_ROLLOVER_TIME_BETWEEN_MESSAGES:
                rollover = False
                clock_cycles_between_messages = previous_device_timestamp - device_timestamp
                seconds_between_messages = clock_cycles_between_messages * self._tick_period * -1

            # From the first timestamp and the exact running total, not the
            # previous (already rounded) timestamp plus this step.
            state.elapsed_seconds += seconds_between_messages
            message_timestamp = state.first_system_timestamp + timedelta(
                seconds=state.elapsed_seconds
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
        """Drop the state for a single device. No-op if unknown."""
        if device_id is None:
            raise TypeError("device_id must not be None")
        with self._states_lock:
            self._device_states.pop(device_id, None)

    def reset_all(self) -> None:
        """Drop state for every tracked device."""
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
