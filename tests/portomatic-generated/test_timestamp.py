"""Tests for `daqifi.timestamp` (port of C# TimestampProcessor)."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import pytest

from daqifi.timestamp import TimestampProcessor, TimestampResult


_UINT32_MAX = 0xFFFFFFFF
_TEST_DEVICE_ID = "test-device-001"


# -- Constructor tests ----------------------------------------------------

class TestConstructor:
    def test_default_uses_default_tick_period(self):
        proc = TimestampProcessor()
        assert proc.tick_period == TimestampProcessor.DEFAULT_TICK_PERIOD

    def test_custom_tick_period(self):
        proc = TimestampProcessor(10e-9)
        assert proc.tick_period == 10e-9

    @pytest.mark.parametrize("invalid", [0, -1e-9, -100])
    def test_invalid_tick_period_raises(self, invalid):
        with pytest.raises(ValueError):
            TimestampProcessor(invalid)


# -- First message tests --------------------------------------------------

class TestFirstMessage:
    def test_first_message_flags(self):
        proc = TimestampProcessor()
        result = proc.process_timestamp(_TEST_DEVICE_ID, 1000)
        assert result.is_first_message is True
        assert result.was_rollover is False
        assert result.clock_cycles_between_messages == 0
        assert result.seconds_between_messages == 0.0

    def test_first_message_timestamp_is_now(self):
        proc = TimestampProcessor()
        before = datetime.now()
        result = proc.process_timestamp(_TEST_DEVICE_ID, 1000)
        after = datetime.now()
        assert before <= result.timestamp <= after


# -- Normal processing ----------------------------------------------------

class TestNormalProcessing:
    def test_second_message_calculates_elapsed_time(self):
        proc = TimestampProcessor()
        first_ts = 1000
        second_ts = 50_000_000  # ~1 second at 20ns

        proc.process_timestamp(_TEST_DEVICE_ID, first_ts)
        result = proc.process_timestamp(_TEST_DEVICE_ID, second_ts)

        assert result.is_first_message is False
        assert result.was_rollover is False
        assert result.clock_cycles_between_messages == second_ts - first_ts
        expected = (second_ts - first_ts) * TimestampProcessor.DEFAULT_TICK_PERIOD
        assert result.seconds_between_messages == pytest.approx(expected, abs=1e-6)

    def test_multiple_messages_accumulate_time(self):
        proc = TimestampProcessor()
        ticks_per_second = 50_000_000

        first = proc.process_timestamp(_TEST_DEVICE_ID, 0)
        second = proc.process_timestamp(_TEST_DEVICE_ID, ticks_per_second)
        third = proc.process_timestamp(_TEST_DEVICE_ID, ticks_per_second * 2)

        elapsed_1to2 = (second.timestamp - first.timestamp).total_seconds()
        elapsed_2to3 = (third.timestamp - second.timestamp).total_seconds()
        assert elapsed_1to2 == pytest.approx(1.0, abs=0.01)
        assert elapsed_2to3 == pytest.approx(1.0, abs=0.01)


# -- Rollover detection ---------------------------------------------------

class TestRollover:
    def test_detects_rollover_when_timestamp_wraps(self):
        proc = TimestampProcessor()
        near_max = _UINT32_MAX - 1_000_000
        after_rollover = 1_000_000

        proc.process_timestamp(_TEST_DEVICE_ID, near_max)
        result = proc.process_timestamp(_TEST_DEVICE_ID, after_rollover)
        assert result.was_rollover is True

    def test_rollover_calculates_correct_cycles(self):
        proc = TimestampProcessor()
        near_max = _UINT32_MAX - 1000
        after_rollover = 1000
        # (MaxValue - near_max) + after_rollover = 1000 + 1000 = 2000
        proc.process_timestamp(_TEST_DEVICE_ID, near_max)
        result = proc.process_timestamp(_TEST_DEVICE_ID, after_rollover)
        assert result.clock_cycles_between_messages == 2000

    def test_rollover_calculates_correct_time(self):
        proc = TimestampProcessor()
        near_max = _UINT32_MAX - 25_000_000
        after_rollover = 25_000_000

        proc.process_timestamp(_TEST_DEVICE_ID, near_max)
        result = proc.process_timestamp(_TEST_DEVICE_ID, after_rollover)
        assert result.was_rollover is True
        assert 0.9 <= result.seconds_between_messages <= 1.1


# -- False positive sanity check -----------------------------------------

class TestFalsePositiveRollover:
    def test_negative_time_when_exceeds_10_seconds(self):
        proc = TimestampProcessor()
        first_ts = 1_000_000_000
        second_ts = 100_000_000  # appears as rollover, but >10s implied

        proc.process_timestamp(_TEST_DEVICE_ID, first_ts)
        result = proc.process_timestamp(_TEST_DEVICE_ID, second_ts)
        assert result.was_rollover is True
        assert result.seconds_between_messages < 0

    def test_valid_rollover_under_10_seconds_positive_time(self):
        proc = TimestampProcessor()
        near_max = _UINT32_MAX - 50_000_000  # ~1s before max
        after_rollover = 50_000_000          # ~1s after rollover

        proc.process_timestamp(_TEST_DEVICE_ID, near_max)
        result = proc.process_timestamp(_TEST_DEVICE_ID, after_rollover)
        assert result.was_rollover is True
        assert result.seconds_between_messages > 0
        assert 1.5 <= result.seconds_between_messages <= 2.5


# -- Multi-device ---------------------------------------------------------

class TestMultiDevice:
    def test_devices_tracked_independently(self):
        proc = TimestampProcessor()
        d1, d2 = "device-001", "device-002"

        d1_first = proc.process_timestamp(d1, 1000)
        d2_first = proc.process_timestamp(d2, 2000)
        d1_second = proc.process_timestamp(d1, 50_000_000)
        d2_second = proc.process_timestamp(d2, 100_000_000)

        assert d1_first.is_first_message
        assert d2_first.is_first_message
        assert not d1_second.is_first_message
        assert not d2_second.is_first_message
        assert d1_second.clock_cycles_between_messages == 50_000_000 - 1000
        assert d2_second.clock_cycles_between_messages == 100_000_000 - 2000

    def test_reset_one_does_not_affect_other(self):
        proc = TimestampProcessor()
        d1, d2 = "device-001", "device-002"
        proc.process_timestamp(d1, 1000)
        proc.process_timestamp(d2, 2000)

        proc.reset(d1)
        d1_after = proc.process_timestamp(d1, 5000)
        d2_after = proc.process_timestamp(d2, 50_000_000)

        assert d1_after.is_first_message is True
        assert d2_after.is_first_message is False


# -- Reset ----------------------------------------------------------------

class TestReset:
    def test_reset_clears_device_state(self):
        proc = TimestampProcessor()
        proc.process_timestamp(_TEST_DEVICE_ID, 1000)
        proc.process_timestamp(_TEST_DEVICE_ID, 2000)

        proc.reset(_TEST_DEVICE_ID)
        result = proc.process_timestamp(_TEST_DEVICE_ID, 3000)
        assert result.is_first_message is True

    def test_reset_nonexistent_device_does_not_raise(self):
        proc = TimestampProcessor()
        proc.reset("never-seen-device")  # should not raise

    def test_reset_all_clears_all(self):
        proc = TimestampProcessor()
        proc.process_timestamp("device-001", 1000)
        proc.process_timestamp("device-002", 2000)
        proc.process_timestamp("device-003", 3000)

        proc.reset_all()
        assert proc.process_timestamp("device-001", 4000).is_first_message
        assert proc.process_timestamp("device-002", 5000).is_first_message
        assert proc.process_timestamp("device-003", 6000).is_first_message


# -- None / empty parameters ---------------------------------------------

class TestNullParams:
    def test_process_none_device_id_raises(self):
        proc = TimestampProcessor()
        with pytest.raises(TypeError):
            proc.process_timestamp(None, 1000)  # type: ignore[arg-type]

    def test_reset_none_device_id_raises(self):
        proc = TimestampProcessor()
        with pytest.raises(TypeError):
            proc.reset(None)  # type: ignore[arg-type]

    def test_empty_device_id_handled(self):
        proc = TimestampProcessor()
        result = proc.process_timestamp("", 1000)
        assert result.is_first_message is True


# -- TimestampResult shape -----------------------------------------------

class TestTimestampResultShape:
    def test_properties_set_correctly(self):
        ts = datetime.now()
        result = TimestampResult(
            timestamp=ts,
            was_rollover=True,
            clock_cycles_between_messages=12345,
            seconds_between_messages=0.00024690,
            is_first_message=False,
        )
        assert result.timestamp == ts
        assert result.was_rollover is True
        assert result.clock_cycles_between_messages == 12345
        assert result.seconds_between_messages == 0.00024690
        assert result.is_first_message is False

    def test_is_frozen(self):
        result = TimestampResult(datetime.now(), False, 0, 0.0, True)
        with pytest.raises(Exception):
            result.was_rollover = True  # type: ignore[misc]


# -- Edge cases -----------------------------------------------------------

class TestEdgeCases:
    def test_zero_timestamp_handled(self):
        proc = TimestampProcessor()
        result = proc.process_timestamp(_TEST_DEVICE_ID, 0)
        assert result.is_first_message is True

    def test_max_timestamp_handled(self):
        proc = TimestampProcessor()
        result = proc.process_timestamp(_TEST_DEVICE_ID, _UINT32_MAX)
        assert result.is_first_message is True

    def test_same_timestamp_twice_zero_elapsed(self):
        proc = TimestampProcessor()
        ts = 50_000_000
        proc.process_timestamp(_TEST_DEVICE_ID, ts)
        result = proc.process_timestamp(_TEST_DEVICE_ID, ts)
        assert result.clock_cycles_between_messages == 0
        assert result.seconds_between_messages == 0.0


# -- Thread safety --------------------------------------------------------

class TestThreadSafety:
    def test_process_timestamp_is_thread_safe(self):
        proc = TimestampProcessor()
        results = []
        results_lock = threading.Lock()

        def call(i: int):
            r = proc.process_timestamp(_TEST_DEVICE_ID, i * 1000)
            with results_lock:
                results.append(r)

        with ThreadPoolExecutor(max_workers=16) as pool:
            futures = [pool.submit(call, i) for i in range(100)]
            for f in as_completed(futures):
                f.result()

        assert len(results) == 100
        first_count = sum(1 for r in results if r.is_first_message)
        assert first_count == 1

    def test_multiple_devices_in_parallel_thread_safe(self):
        proc = TimestampProcessor()
        per_device_results: dict[str, list[TimestampResult]] = {}
        per_device_locks: dict[str, threading.Lock] = {}

        for n in range(10):
            d = f"device-{n:03d}"
            per_device_results[d] = []
            per_device_locks[d] = threading.Lock()

        def call(device_id: str, msg_num: int):
            r = proc.process_timestamp(device_id, msg_num * 1_000_000)
            with per_device_locks[device_id]:
                per_device_results[device_id].append(r)

        with ThreadPoolExecutor(max_workers=32) as pool:
            futures = []
            for device_num in range(10):
                d = f"device-{device_num:03d}"
                for msg_num in range(10):
                    futures.append(pool.submit(call, d, msg_num))
            for f in as_completed(futures):
                f.result()

        assert len(per_device_results) == 10
        for results in per_device_results.values():
            assert len(results) == 10
