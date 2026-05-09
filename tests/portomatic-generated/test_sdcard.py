"""Tests for `daqifi.sdcard` (port of SD card value types)."""
from __future__ import annotations

import io
import os
import tempfile
from datetime import datetime, timedelta

import pytest

from daqifi.enums import SdCardLogFormat
from daqifi.sdcard import (
    END_OF_FILE_MARKER,
    SdCardDeviceConfiguration,
    SdCardDownloadResult,
    SdCardFileInfo,
    SdCardFileParser,
    SdCardFileParserFactory,
    SdCardLogEntry,
    SdCardLogSession,
    SdCardOperations,
    SdCardParseOptions,
    SdCardParseProgress,
    SdCardTransferProgress,
)


class TestSdCardFileInfo:
    def test_construct_with_name_only(self):
        info = SdCardFileInfo(file_name="20240101.bin")
        assert info.file_name == "20240101.bin"
        assert info.created_date is None

    def test_construct_with_date(self):
        d = datetime(2024, 1, 1, 12, 0, 0)
        info = SdCardFileInfo(file_name="log.bin", created_date=d)
        assert info.created_date == d

    def test_none_filename_raises(self):
        with pytest.raises(TypeError):
            SdCardFileInfo(file_name=None)  # type: ignore[arg-type]

    def test_is_frozen(self):
        info = SdCardFileInfo(file_name="x")
        with pytest.raises(Exception):
            info.file_name = "y"  # type: ignore[misc]


class TestSdCardParseProgress:
    def test_construct_sets_fields(self):
        p = SdCardParseProgress(bytes_read=100, total_bytes=1000, messages_read=5)
        assert p.bytes_read == 100
        assert p.total_bytes == 1000
        assert p.messages_read == 5

    def test_unknown_total_uses_negative_one(self):
        p = SdCardParseProgress(bytes_read=42, total_bytes=-1, messages_read=2)
        assert p.total_bytes == -1

    def test_is_frozen(self):
        p = SdCardParseProgress(bytes_read=0, total_bytes=0, messages_read=0)
        with pytest.raises(Exception):
            p.bytes_read = 1  # type: ignore[misc]


class TestSdCardDeviceConfiguration:
    def test_minimal_required_fields(self):
        cfg = SdCardDeviceConfiguration(
            analog_port_count=4,
            digital_port_count=8,
            timestamp_frequency=50_000_000,
        )
        assert cfg.analog_port_count == 4
        assert cfg.digital_port_count == 8
        assert cfg.timestamp_frequency == 50_000_000
        assert cfg.device_serial_number is None
        assert cfg.calibration_values is None
        assert cfg.resolution == 0

    def test_full_field_set(self):
        cfg = SdCardDeviceConfiguration(
            analog_port_count=2,
            digital_port_count=4,
            timestamp_frequency=50_000_000,
            device_serial_number="SN001",
            device_part_number="PN-X",
            firmware_revision="1.2.3",
            calibration_values=[(1.0, 0.0), (1.5, -0.1)],
            resolution=65535,
            port_range=[10.0, 5.0],
            internal_scale_m=[1.0, 1.0],
        )
        assert cfg.device_serial_number == "SN001"
        # Container fields are coerced to tuples for true frozen
        # immutability (Qodo PR 17 pass 10 #6).
        assert cfg.calibration_values == ((1.0, 0.0), (1.5, -0.1))
        assert cfg.port_range == (10.0, 5.0)
        assert cfg.internal_scale_m == (1.0, 1.0)

    def test_is_frozen(self):
        cfg = SdCardDeviceConfiguration(0, 0, 0)
        with pytest.raises(Exception):
            cfg.resolution = 1  # type: ignore[misc]


class TestSdCardParseOptions:
    def test_defaults(self):
        opts = SdCardParseOptions()
        assert opts.session_start_time is None
        assert opts.buffer_size == 64 * 1024
        assert opts.progress is None
        assert opts.fallback_timestamp_frequency == 50_000_000
        assert opts.configuration_override is None

    def test_progress_is_callable_slot(self):
        calls = []
        opts = SdCardParseOptions(progress=lambda p: calls.append(p))
        # Simulate calling it.
        progress = SdCardParseProgress(1, 2, 3)
        opts.progress(progress)  # type: ignore[misc]
        assert calls == [progress]

    def test_mutable(self):
        opts = SdCardParseOptions()
        opts.buffer_size = 1024
        assert opts.buffer_size == 1024

    def test_zero_fallback_disables(self):
        opts = SdCardParseOptions(fallback_timestamp_frequency=0)
        assert opts.fallback_timestamp_frequency == 0

    def test_with_configuration_override(self):
        cfg = SdCardDeviceConfiguration(1, 2, 50_000_000)
        opts = SdCardParseOptions(configuration_override=cfg)
        assert opts.configuration_override is cfg


@pytest.mark.portomatic_generated
class TestSdCardTransferProgress:
    def test_construct(self):
        p = SdCardTransferProgress(bytes_received=1024, file_name="log.bin")
        assert p.bytes_received == 1024
        assert p.file_name == "log.bin"

    def test_is_frozen(self):
        p = SdCardTransferProgress(bytes_received=0, file_name="x")
        with pytest.raises(Exception):
            p.bytes_received = 1  # type: ignore[misc]

    def test_equality(self):
        a = SdCardTransferProgress(100, "log.bin")
        b = SdCardTransferProgress(100, "log.bin")
        assert a == b


@pytest.mark.portomatic_generated
class TestSdCardDownloadResult:
    def test_required_fields(self):
        r = SdCardDownloadResult(
            file_name="log.bin",
            file_size=2048,
            duration=timedelta(seconds=3.5),
        )
        assert r.file_name == "log.bin"
        assert r.file_size == 2048
        assert r.duration == timedelta(seconds=3.5)
        assert r.file_path is None

    def test_with_local_file_path(self):
        r = SdCardDownloadResult(
            file_name="log.bin",
            file_size=2048,
            duration=timedelta(milliseconds=750),
            file_path="/tmp/dl/log.bin",
        )
        assert r.file_path == "/tmp/dl/log.bin"

    def test_is_frozen(self):
        r = SdCardDownloadResult(
            file_name="log.bin",
            file_size=0,
            duration=timedelta(0),
        )
        with pytest.raises(Exception):
            r.file_size = 1  # type: ignore[misc]


@pytest.mark.portomatic_generated
class TestSdCardOperationsProtocol:
    def test_runtime_checkable_complete_stub(self):
        class _Stub:
            @property
            def is_logging_to_sd_card(self) -> bool:
                return False

            @property
            def sd_card_files(self):
                return ()

            async def get_sd_card_files(self):
                return ()

            async def start_sd_card_logging(
                self, file_name=None, channel_mask=None,
                format=SdCardLogFormat.PROTOBUF,
            ):
                return None

            async def stop_sd_card_logging(self):
                return None

            async def delete_sd_card_file(self, file_name):
                return None

            async def format_sd_card(self):
                return None

            async def download_sd_card_file(
                self, file_name, destination_stream, progress=None,
            ):
                return SdCardDownloadResult(file_name, 0, timedelta(0))

            async def download_sd_card_file_to_temp(
                self, file_name, progress=None,
            ):
                return SdCardDownloadResult(file_name, 0, timedelta(0))

        assert isinstance(_Stub(), SdCardOperations)

    def test_incomplete_fails_isinstance(self):
        class _Incomplete:
            @property
            def is_logging_to_sd_card(self) -> bool:
                return False

            # Missing nearly all of the methods.

        assert not isinstance(_Incomplete(), SdCardOperations)


# ---------------------------------------------------------------------------
# Parser tests — synthetic protobuf bytes via the real generated class
# ---------------------------------------------------------------------------


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _encode_message(msg) -> bytes:
    """Length-prefixed wire encoding the parser expects (one frame)."""
    payload = msg.SerializeToString()
    return _varint(len(payload)) + payload


def _build_status_frame(
    *,
    timestamp_freq: int = 1000,
    analog_in_port_num: int = 4,
    digital_port_num: int = 16,
    device_sn: int = 1234567890,
    device_pn: str = "Nq1",
    device_fw_rev: str = "3.4.6b1",
    analog_in_res: int = 16384,
    cal_m=(1.0, 1.0, 1.0, 1.0),
    cal_b=(0.0, 0.0, 0.0, 0.0),
    port_range=(10.0, 10.0, 10.0, 10.0),
    int_scale_m=(1.0, 1.0, 1.0, 1.0),
):
    from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage

    msg = DaqifiOutMessage()
    msg.timestamp_freq = timestamp_freq
    msg.analog_in_port_num = analog_in_port_num
    msg.digital_port_num = digital_port_num
    msg.device_sn = device_sn
    msg.device_pn = device_pn
    msg.device_fw_rev = device_fw_rev
    msg.analog_in_res = analog_in_res
    msg.analog_in_cal_m.extend(cal_m)
    msg.analog_in_cal_b.extend(cal_b)
    msg.analog_in_port_range.extend(port_range)
    msg.analog_in_int_scale_m.extend(int_scale_m)
    return msg


def _build_stream_frame(
    *,
    msg_time_stamp: int,
    analog_float=(),
    analog_raw=(),
    digital_data: bytes = b"",
    analog_in_data_ts=(),
):
    from daqifi._internal.proto.DaqifiOutMessage_pb2 import DaqifiOutMessage

    msg = DaqifiOutMessage()
    msg.msg_time_stamp = msg_time_stamp
    if analog_float:
        msg.analog_in_data_float.extend(analog_float)
    if analog_raw:
        msg.analog_in_data.extend(analog_raw)
    if digital_data:
        msg.digital_data = digital_data
    if analog_in_data_ts:
        msg.analog_in_data_ts.extend(analog_in_data_ts)
    return msg


class TestSdCardLogEntry:
    def test_construct_minimal(self):
        ts = datetime(2026, 5, 9, 12, 0, 0)
        entry = SdCardLogEntry(timestamp=ts, analog_values=(1.0, 2.0), digital_data=0xFF)
        assert entry.timestamp == ts
        assert entry.analog_values == (1.0, 2.0)
        assert entry.digital_data == 0xFF
        assert entry.analog_timestamps is None

    def test_iterable_inputs_coerce_to_tuple(self):
        ts = datetime(2026, 1, 1)
        analog_list = [1.0, 2.0, 3.0]
        ts_list = [10, 20, 30]
        entry = SdCardLogEntry(
            timestamp=ts,
            analog_values=analog_list,
            digital_data=0,
            analog_timestamps=ts_list,
        )
        # Mutating the original lists must not alter the entry.
        analog_list.append(99.0)
        ts_list.append(99)
        assert entry.analog_values == (1.0, 2.0, 3.0)
        assert entry.analog_timestamps == (10, 20, 30)

    def test_is_frozen(self):
        entry = SdCardLogEntry(
            timestamp=datetime(2026, 1, 1), analog_values=(), digital_data=0
        )
        with pytest.raises(Exception):
            entry.digital_data = 1  # type: ignore[misc]


class TestSdCardLogSession:
    def test_construct_with_required_fields(self):
        samples = iter(())
        session = SdCardLogSession(
            file_name="log.bin",
            file_created_date=None,
            device_config=None,
            samples=samples,
        )
        assert session.file_name == "log.bin"
        assert session.file_created_date is None
        assert session.device_config is None
        assert list(session.samples) == []

    def test_none_filename_raises(self):
        with pytest.raises(TypeError):
            SdCardLogSession(
                file_name=None,  # type: ignore[arg-type]
                file_created_date=None,
                device_config=None,
                samples=iter(()),
            )

    def test_none_samples_raises(self):
        with pytest.raises(TypeError):
            SdCardLogSession(
                file_name="log.bin",
                file_created_date=None,
                device_config=None,
                samples=None,  # type: ignore[arg-type]
            )


class TestSdCardFileParser:
    def test_empty_stream_returns_session_with_no_samples(self):
        session = SdCardFileParser().parse_stream(io.BytesIO(b""), "log.bin")
        assert session.file_name == "log.bin"
        assert session.device_config is None
        assert list(session.samples) == []

    def test_parses_status_frame_then_stream_frames(self):
        # Use powers of two for the float values — they round-trip
        # cleanly through float32 wire encoding without drift.
        wire = (
            _encode_message(_build_status_frame(timestamp_freq=1000))
            + _encode_message(
                _build_stream_frame(msg_time_stamp=100, analog_float=(0.5, 0.25))
            )
            + _encode_message(
                _build_stream_frame(msg_time_stamp=200, analog_float=(0.75, 0.125))
            )
        )
        session = SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin")
        assert session.device_config is not None
        assert session.device_config.timestamp_frequency == 1000
        assert session.device_config.device_serial_number == "1234567890"
        samples = list(session.samples)
        assert len(samples) == 2
        assert samples[0].analog_values == (0.5, 0.25)
        # Second sample is 100 ticks (= 0.1 s at 1 kHz) after the first.
        assert samples[1].timestamp - samples[0].timestamp == timedelta(seconds=0.1)

    def test_strips_eof_marker(self):
        wire = (
            _encode_message(_build_status_frame())
            + _encode_message(
                _build_stream_frame(msg_time_stamp=10, analog_float=(1.0,))
            )
            + END_OF_FILE_MARKER
        )
        session = SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin")
        samples = list(session.samples)
        assert len(samples) == 1
        assert samples[0].analog_values == (1.0,)

    def test_timestamp_rollover(self):
        # Place the first stream tick near uint32 max so the second
        # rolls over. Use a 1 Hz timestamp_freq so 1 tick == 1 second.
        wire = (
            _encode_message(_build_status_frame(timestamp_freq=1))
            + _encode_message(
                _build_stream_frame(
                    msg_time_stamp=0xFFFFFFFE, analog_float=(1.0,)
                )
            )
            + _encode_message(
                _build_stream_frame(msg_time_stamp=2, analog_float=(2.0,))
            )
        )
        samples = list(SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin").samples)
        assert len(samples) == 2
        # 0xFFFFFFFE → 2 spans 4 ticks (0xFFFFFFFE → 0xFFFFFFFF → 0 → 1 → 2).
        assert samples[1].timestamp - samples[0].timestamp == timedelta(seconds=4)

    def test_merges_consecutive_same_timestamp_messages(self):
        # Firmware sometimes splits analog and digital into two frames
        # at the same MsgTimeStamp. They must collapse into one entry.
        wire = (
            _encode_message(_build_status_frame())
            + _encode_message(
                _build_stream_frame(msg_time_stamp=100, analog_float=(0.5, 0.25))
            )
            + _encode_message(
                _build_stream_frame(
                    msg_time_stamp=100, digital_data=bytes([0xAA, 0x00])
                )
            )
        )
        samples = list(SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin").samples)
        assert len(samples) == 1
        assert samples[0].analog_values == (0.5, 0.25)
        assert samples[0].digital_data == 0xAA

    def test_raw_analog_scaling_uses_config(self):
        # 1.0 * (16383 / 16384) * 10 * 1.0 + 0.0 = 9.999...
        wire = (
            _encode_message(_build_status_frame())
            + _encode_message(
                _build_stream_frame(msg_time_stamp=100, analog_raw=(16383,))
            )
        )
        samples = list(SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin").samples)
        assert len(samples) == 1
        assert samples[0].analog_values[0] == pytest.approx(
            10.0 * 16383 / 16384, rel=1e-9
        )

    def test_raw_analog_no_config_returns_raw_floats(self):
        # Stream-only file (no status frame) and no embedded config in
        # stream messages — _scale_raw_analog_values should return raws.
        wire = _encode_message(
            _build_stream_frame(msg_time_stamp=100, analog_raw=(123, -456))
        )
        # Override to disable the fallback frequency; we only care
        # about analog_values here.
        opts = SdCardParseOptions(fallback_timestamp_frequency=0)
        samples = list(
            SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin", opts).samples
        )
        assert len(samples) == 1
        assert samples[0].analog_values == (123.0, -456.0)

    def test_digital_data_packs_low_byte(self):
        wire = (
            _encode_message(_build_status_frame())
            + _encode_message(
                _build_stream_frame(
                    msg_time_stamp=10,
                    analog_float=(0.0,),
                    digital_data=bytes([0x12, 0x34, 0x56, 0x78]),
                )
            )
        )
        samples = list(SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin").samples)
        # Bytes packed little-endian into a single uint.
        assert samples[0].digital_data == 0x78563412

    def test_first_message_with_stream_payload_is_not_treated_as_status(self):
        # Embedded-config style: timestamp_freq lives in a stream frame.
        wire = _encode_message(
            _build_stream_frame(msg_time_stamp=10, analog_float=(0.5,))
        )
        session = SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin")
        # Frame had no config fields → no device config produced.
        assert session.device_config is None
        samples = list(session.samples)
        assert len(samples) == 1
        assert samples[0].analog_values == (0.5,)

    def test_configuration_override_supplies_resolution_when_file_lacks_it(self):
        # Stream-only file, no embedded config; override supplies it.
        wire = _encode_message(
            _build_stream_frame(msg_time_stamp=100, analog_raw=(8192,))
        )
        override = SdCardDeviceConfiguration(
            analog_port_count=1,
            digital_port_count=0,
            timestamp_frequency=1000,
            resolution=16384,
            port_range=(10.0,),
            internal_scale_m=(1.0,),
            calibration_values=((1.0, 0.0),),
        )
        opts = SdCardParseOptions(configuration_override=override)
        session = SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin", opts)
        samples = list(session.samples)
        assert len(samples) == 1
        assert samples[0].analog_values[0] == pytest.approx(5.0, rel=1e-9)
        assert session.device_config.resolution == 16384

    def test_configuration_override_does_not_clobber_file_values(self):
        # Locks in the documented override-as-fallback semantics
        # (mirrors upstream SdCardFileParser.cs:82-89). When the file
        # supplies a resolution, the override's resolution must NOT
        # win — analog scaling should use the file's value. This is
        # intentional: the override exists to fill gaps when the file
        # lacks a status header, not to second-guess what the file
        # said.
        wire = (
            _encode_message(_build_status_frame(analog_in_res=16384))
            + _encode_message(
                _build_stream_frame(msg_time_stamp=100, analog_raw=(8192,))
            )
        )
        override = SdCardDeviceConfiguration(
            analog_port_count=1,
            digital_port_count=0,
            timestamp_frequency=1000,
            resolution=99999,  # bogus — must be ignored
            port_range=(20.0,),  # bogus — must be ignored
            internal_scale_m=(2.0,),  # bogus — must be ignored
            calibration_values=((5.0, 1.0),),  # bogus — must be ignored
        )
        opts = SdCardParseOptions(configuration_override=override)
        session = SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin", opts)
        # File's (10.0 range, 16384 res, 1.0 scale, 1.0/0.0 cal) wins:
        # 8192/16384 * 10 * 1 + 0 = 5.0
        samples = list(session.samples)
        assert samples[0].analog_values[0] == pytest.approx(5.0, rel=1e-9)
        assert session.device_config.resolution == 16384
        assert session.device_config.port_range == (10.0, 10.0, 10.0, 10.0)

    def test_progress_callback_invoked(self):
        wire = _encode_message(_build_status_frame()) * 1
        seen = []
        opts = SdCardParseOptions(progress=seen.append, buffer_size=64)
        SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin", opts)
        assert seen, "progress callback should be invoked at least once"
        assert all(isinstance(p, SdCardParseProgress) for p in seen)

    def test_filename_date_extraction(self):
        wire = _encode_message(_build_status_frame())
        session = SdCardFileParser().parse_stream(
            io.BytesIO(wire), "log_20260509_120000.bin"
        )
        assert session.file_created_date == datetime(2026, 5, 9, 12, 0, 0)

    def test_path_like_filename_still_extracts_date(self):
        # Regression: callers may pass a full path as file_name. The
        # parser must basename it before the date regex runs, otherwise
        # ``log_YYYYMMDD_HHMMSS.bin`` wrapped in any directory loses
        # its embedded creation date.
        wire = _encode_message(_build_status_frame())
        session = SdCardFileParser().parse_stream(
            io.BytesIO(wire), "/tmp/sdcard/log_20260509_120000.bin"
        )
        assert session.file_created_date == datetime(2026, 5, 9, 12, 0, 0)
        assert session.file_name == "log_20260509_120000.bin"

    def test_session_start_time_overrides_filename_date(self):
        wire = _encode_message(_build_status_frame())
        opts = SdCardParseOptions(session_start_time=datetime(2030, 1, 1))
        session = SdCardFileParser().parse_stream(
            io.BytesIO(wire), "log_20260509_120000.bin", opts
        )
        assert session.file_created_date == datetime(2030, 1, 1)

    def test_parse_file_round_trip(self):
        wire = (
            _encode_message(_build_status_frame())
            + _encode_message(
                _build_stream_frame(msg_time_stamp=100, analog_float=(7.0,))
            )
        )
        with tempfile.NamedTemporaryFile(
            "wb", suffix=".bin", delete=False
        ) as fh:
            fh.write(wire)
            path = fh.name
        try:
            session = SdCardFileParser().parse_file(path)
            samples = list(session.samples)
            assert samples[0].analog_values == (7.0,)
        finally:
            os.unlink(path)

    def test_invalid_buffer_size_raises(self):
        opts = SdCardParseOptions(buffer_size=0)
        with pytest.raises(ValueError):
            SdCardFileParser().parse_stream(io.BytesIO(b""), "log.bin", opts)

    def test_none_stream_raises(self):
        with pytest.raises(TypeError):
            SdCardFileParser().parse_stream(None, "log.bin")  # type: ignore[arg-type]

    def test_none_filename_raises(self):
        with pytest.raises(TypeError):
            SdCardFileParser().parse_stream(io.BytesIO(b""), None)  # type: ignore[arg-type]

    def test_chunked_buffer_handles_frame_straddle(self):
        # Buffer size of 8 forces frames to straddle reads — verifies
        # the carry-over logic preserves split frames.
        wire = (
            _encode_message(_build_status_frame())
            + _encode_message(
                _build_stream_frame(msg_time_stamp=100, analog_float=(1.0, 2.0, 3.0))
            )
        )
        opts = SdCardParseOptions(buffer_size=8)
        session = SdCardFileParser().parse_stream(io.BytesIO(wire), "log.bin", opts)
        samples = list(session.samples)
        assert len(samples) == 1
        assert samples[0].analog_values == (1.0, 2.0, 3.0)


class TestSdCardFileParserFactory:
    @pytest.mark.parametrize(
        "name,expected",
        [
            ("log.bin", SdCardLogFormat.PROTOBUF),
            ("LOG.BIN", SdCardLogFormat.PROTOBUF),
            ("log.json", SdCardLogFormat.JSON),
            ("log.csv", SdCardLogFormat.CSV),
            ("/tmp/some/path/log_20260509_120000.bin", SdCardLogFormat.PROTOBUF),
        ],
    )
    def test_detect_format_known_extensions(self, name, expected):
        assert SdCardFileParserFactory.detect_format(name) is expected

    def test_detect_format_unknown_raises_value_error(self):
        with pytest.raises(ValueError):
            SdCardFileParserFactory.detect_format("log.txt")

    def test_detect_format_none_raises(self):
        with pytest.raises(TypeError):
            SdCardFileParserFactory.detect_format(None)  # type: ignore[arg-type]

    def test_parse_with_protobuf_format_dispatches_to_parser(self):
        wire = _encode_message(_build_status_frame())
        session = SdCardFileParserFactory.parse_with_format(
            io.BytesIO(wire), "log.bin", SdCardLogFormat.PROTOBUF
        )
        assert session.device_config is not None

    @pytest.mark.parametrize(
        "fmt", [SdCardLogFormat.JSON, SdCardLogFormat.CSV]
    )
    def test_unported_formats_raise_not_implemented(self, fmt):
        with pytest.raises(NotImplementedError):
            SdCardFileParserFactory.parse_with_format(
                io.BytesIO(b""), "x", fmt
            )

    def test_parse_stream_uses_extension_detection(self):
        wire = _encode_message(_build_status_frame())
        session = SdCardFileParserFactory.parse_stream(io.BytesIO(wire), "log.bin")
        assert session.device_config is not None

    def test_parse_stream_unknown_extension_raises(self):
        with pytest.raises(ValueError):
            SdCardFileParserFactory.parse_stream(io.BytesIO(b""), "log.txt")

    def test_parse_file_round_trip(self):
        wire = _encode_message(_build_status_frame())
        with tempfile.NamedTemporaryFile(
            "wb", suffix=".bin", delete=False
        ) as fh:
            fh.write(wire)
            path = fh.name
        try:
            session = SdCardFileParserFactory.parse_file(path)
            assert session.device_config is not None
        finally:
            os.unlink(path)

    def test_parse_file_none_raises(self):
        with pytest.raises(TypeError):
            SdCardFileParserFactory.parse_file(None)  # type: ignore[arg-type]
