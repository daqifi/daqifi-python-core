"""Integration-test fixtures.

These tests connect to a real DAQiFi device. They auto-skip unless the
device is discoverable. Set `DAQIFI_DEVICE_PORT=/dev/ttyUSB0` (or
similar) to force a specific port; otherwise auto-discovery runs.

Set `DAQIFI_DEVICE_REQUIRED=1` to FAIL (rather than skip) when no
device is found — useful in CI runs that have a device attached.

The lower-level ``serial_transport``, ``clean_device``, ``error_drainer``,
``firmware_stats_diff`` and ``wire_capture`` fixtures live alongside the
high-level ``device`` fixture for use by tests under
``tests/integration/hardware/``.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List

import pytest

from daqifi.device import NyquistDevice
from daqifi.discovery import DeviceFinder, DeviceInfo
from daqifi.transport import SerialStreamTransport


# Match the canonical 'no-error' reply against the raw bytes — robust
# against leftover binary stream data (protobuf frames, partial UTF-8)
# that may share the read window with a SCPI reply just after a stream
# stops. ``\b`` boundaries keep us from matching e.g. '120,"No error in
# stream"' if firmware ever emits something looser.
_SCPI_NO_ERROR_RE = re.compile(rb'\b0\s*,\s*"No error"')

# A SCPI error payload has the shape `<int>,"<message>"`. Anything
# else in the response window (echo, prompt, stray binary) is not a
# valid error to record.
_SCPI_ERROR_LINE_RE = re.compile(r'^-?\d+\s*,\s*".*"$')


def _required() -> bool:
    return os.environ.get("DAQIFI_DEVICE_REQUIRED", "").lower() in ("1", "true", "yes")


def _explicit_port() -> str | None:
    return os.environ.get("DAQIFI_DEVICE_PORT") or None


def _destructive_ok() -> bool:
    return os.environ.get("DAQIFI_DESTRUCTIVE_OK", "").lower() in ("1", "true", "yes")


@pytest.fixture(scope="session")
def device_info() -> DeviceInfo:
    """Discover a connected device, or skip if none present."""
    explicit = _explicit_port()
    if explicit:
        info = DeviceFinder._identify_device(explicit)  # noqa: SLF001 — internal but works
        if info is None:
            msg = f"DAQIFI_DEVICE_PORT={explicit!r} could not be identified as a DAQiFi device"
            if _required():
                pytest.fail(msg)
            pytest.skip(msg)
        return info

    info = DeviceFinder.discover_first()
    if info is None:
        msg = "no DAQiFi device discovered (set DAQIFI_DEVICE_PORT to force, or DAQIFI_DEVICE_REQUIRED=1 to fail)"
        if _required():
            pytest.fail(msg)
        pytest.skip(msg)
    return info


@pytest.fixture
def device(device_info: DeviceInfo):
    """Connect to the discovered device, yield it, then disconnect cleanly."""
    dev = NyquistDevice.connect(device_info)
    try:
        yield dev
    finally:
        try:
            dev.disconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Hardware-test shared fixtures (used by tests/integration/hardware/)
# ---------------------------------------------------------------------------


@pytest.fixture
def serial_transport(device_info: DeviceInfo) -> Iterator[SerialStreamTransport]:
    """Open a raw SerialStreamTransport against the discovered device.

    Returned with NO consumer attached — tests own the read end of the
    stream and can drive raw SCPI directly without competing with a
    background protobuf consumer. Tests that want the high-level
    facade (NyquistDevice / DaqifiStreamingDevice) construct it
    themselves on top of this transport, OR use the ``device`` fixture.
    """
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    transport = SerialStreamTransport(
        port_name=port,
        baud_rate=115200,
        os_rx_buffer_bytes=65536,
        os_tx_buffer_bytes=65536,
    )
    transport.connect()
    if not transport.is_connected or transport.stream is None:
        # connect() raises on failure on most paths, but guard the
        # belt-and-suspenders case so a downstream test gets a clear
        # skip message instead of an AttributeError trying to read
        # bytes off a None stream.
        try:
            transport.disconnect()
        except Exception:
            pass
        pytest.skip(f"failed to connect to device on port {port!r}")
    try:
        yield transport
    finally:
        try:
            transport.disconnect()
        except Exception:
            pass


def _send_scpi(serial_obj, line: str, delay: float = 0.2) -> None:
    serial_obj.write((line + "\r").encode("ascii"))
    try:
        serial_obj.flush()
    except Exception:
        pass
    if delay > 0:
        time.sleep(delay)


def _drain(serial_obj, timeout: float) -> bytes:
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        n = serial_obj.in_waiting
        if n > 0:
            buf.extend(serial_obj.read(n))
        else:
            time.sleep(0.005)
    return bytes(buf)


_DRAIN_EMPTY_BUDGET = 5


def _drain_scpi_errors(serial_obj, max_iterations: int = 256) -> List[str]:
    """Pop SYST:ERR? until the queue reports 'No error' or cap is reached.

    Returns the popped error strings (empty if the queue was clean).
    Mirrors the design from the new daqifi-python-core#22 ticket: the
    caller owns the popped errors and decides whether to log/raise.

    Robust against binary noise in the read window — checks the raw
    bytes for the canonical 'No error' marker before falling back to
    text decoding, so leftover protobuf stream bytes (or any other
    non-ASCII junk in the buffer) can't mask a clean queue.

    Anti-hang escape hatch: if the device returns ``_DRAIN_EMPTY_BUDGET``
    consecutive *unproductive* reads (truly empty OR full of bytes that
    don't contain a SCPI error payload — e.g. a stream that's still
    flushing protobuf frames into the buffer), treat the queue as
    drained rather than spinning ``max_iterations × _drain.timeout``
    seconds (~60s) waiting for a response that's never coming.
    """
    popped: List[str] = []
    unproductive_reads = 0
    for _ in range(max_iterations):
        _send_scpi(serial_obj, "SYST:ERR?", delay=0.05)
        raw = _drain(serial_obj, timeout=0.2)
        if not raw:
            unproductive_reads += 1
            if unproductive_reads >= _DRAIN_EMPTY_BUDGET:
                return popped
            continue
        if _SCPI_NO_ERROR_RE.search(raw):
            return popped
        text = raw.decode("ascii", errors="replace")
        found_error_line = False
        for line in text.split("\r\n"):
            line = line.strip()
            if not line or line.startswith("SYST:ERR?") or line == "DAQIFI>":
                continue
            if _SCPI_ERROR_LINE_RE.match(line):
                popped.append(line)
                found_error_line = True
                break
        if found_error_line:
            unproductive_reads = 0
        else:
            unproductive_reads += 1
            if unproductive_reads >= _DRAIN_EMPTY_BUDGET:
                return popped
    return popped


@pytest.fixture
def error_drainer(serial_transport: SerialStreamTransport):
    """Function-scoped fixture: drain SYST:ERR? before AND after a test.

    Yields a callable so a test can also drain mid-run and inspect the
    popped errors. After the test, fails if errors accumulated *during*
    the test (caller-introduced) — so a clean-bill-of-health gate.
    """
    serial_obj = serial_transport.stream  # type: ignore[union-attr]

    pre = _drain_scpi_errors(serial_obj)
    if pre:
        # Pre-existing errors aren't the test's fault — log via print
        # so they show up under `pytest -s` but don't fail.
        print(f"\n[error_drainer] pre-test drained {len(pre)} stale error(s):")
        for e in pre:
            print(f"  {e}")

    def _drain_now() -> List[str]:
        return _drain_scpi_errors(serial_obj)

    yield _drain_now

    post = _drain_scpi_errors(serial_obj)
    if post:
        formatted = "\n  ".join(post)
        pytest.fail(
            f"test left {len(post)} SCPI error(s) in the device queue:\n  {formatted}"
        )


@pytest.fixture
def clean_device(serial_transport: SerialStreamTransport):
    """Bring the device to a known-clean state before yielding it.

    Steps (mirrors the wake/init sequence proven on COM7 fw 3.4.6b1):
      1. Drain any stale SCPI errors
      2. Stop any in-flight streaming
      3. Disable all ADC channels
      4. Wake from Standby (`SYSTem:POWer:STATe 1`)
      5. Set USB streaming interface (`SYSTem:STReam:INTerface 0`)
      6. Re-drain errors so the test starts with an empty queue

    On teardown: stop streaming again (idempotent), disable channels.
    """
    serial_obj = serial_transport.stream  # type: ignore[union-attr]

    # Brief CDC settle window — the OS / pyserial dance to open the
    # port can leave a few stray bytes in the receive buffer (boot
    # banner, stale streaming output from a prior session). Drain
    # them before issuing the first SCPI command so they don't get
    # mistaken for a SCPI reply.
    time.sleep(0.2)
    _drain(serial_obj, timeout=0.2)

    _drain_scpi_errors(serial_obj)
    _send_scpi(serial_obj, "SYSTem:StopStreamData", delay=0.5)
    _drain(serial_obj, timeout=0.3)
    _send_scpi(serial_obj, "ENAble:VOLTage:DC 0", delay=0.2)
    _drain(serial_obj, timeout=0.2)
    _send_scpi(serial_obj, "SYSTem:POWer:STATe 1", delay=0.5)
    _drain(serial_obj, timeout=0.3)
    # STR:INT is unrecognised on older firmwares — ignore the error
    # rather than letting it taint the queue.
    _send_scpi(serial_obj, "SYSTem:STReam:INTerface 0", delay=0.2)
    _drain(serial_obj, timeout=0.2)
    _drain_scpi_errors(serial_obj)

    yield serial_transport

    # Best-effort teardown: stop streaming, disable channels.
    try:
        _send_scpi(serial_obj, "SYSTem:StopStreamData", delay=0.3)
        _drain(serial_obj, timeout=0.3)
        _send_scpi(serial_obj, "ENAble:VOLTage:DC 0", delay=0.1)
        _drain(serial_obj, timeout=0.2)
    except Exception:
        pass


def _parse_kv_block(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in text.split("\r\n"):
        line = line.strip()
        if "=" not in line or "DAQIFI" in line or line.startswith("SYST"):
            continue
        k, _, v = line.partition("=")
        try:
            out[k.strip()] = int(v.strip())
        except ValueError:
            pass
    return out


class _StatsDelta:
    """Captures pre/post SYST:STR:STATS? snapshots and computes deltas."""

    def __init__(self, serial_obj):
        self._serial = serial_obj
        self.pre: dict[str, int] = {}
        self.post: dict[str, int] = {}

    def snapshot(self) -> dict[str, int]:
        _drain(self._serial, timeout=0.2)
        _send_scpi(self._serial, "SYST:STR:STATS?", delay=0.5)
        raw = _drain(self._serial, timeout=1.0)
        return _parse_kv_block(raw.decode("ascii", errors="replace"))

    def open(self) -> None:
        self.pre = self.snapshot()

    def close(self) -> None:
        self.post = self.snapshot()

    def delta(self, key: str) -> int:
        return self.post.get(key, 0) - self.pre.get(key, 0)

    def assert_no_drops(self) -> None:
        """Caller-friendly: assert all firmware drop counters didn't move."""
        for key in ("UsbDroppedBytes", "QueueDroppedSamples",
                    "WifiDroppedBytes", "SdDroppedBytes",
                    "EncoderDroppedSamples", "DioDroppedSamples"):
            d = self.delta(key)
            assert d == 0, f"firmware reported {d} {key} during the test"


@pytest.fixture
def firmware_stats_diff(serial_transport: SerialStreamTransport):
    """Snapshot SYST:STR:STATS? before + after; expose deltas.

    Use::

        def test_thing(clean_device, firmware_stats_diff):
            firmware_stats_diff.open()
            # ... run the streaming test ...
            firmware_stats_diff.close()
            firmware_stats_diff.assert_no_drops()
    """
    serial_obj = serial_transport.stream  # type: ignore[union-attr]
    return _StatsDelta(serial_obj)


@pytest.fixture
def wire_capture(serial_transport: SerialStreamTransport, request):
    """Opt-in: write the raw serial byte stream to tests/integration/captures/.

    Returns a callable used as a `StreamMessageConsumer`-style chunk
    handler (or just `bytes -> None`). Bytes are appended to a single
    file per test; the path is reported via `print` so it's visible in
    `pytest -s` output and can be promoted to a fixture in
    `tests/test_streaming_wire_fixtures.py`.
    """
    captures_dir = Path(__file__).resolve().parent / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    test_id = request.node.name.replace("[", "_").replace("]", "").replace("/", "_")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = captures_dir / f"{test_id}-{stamp}.bin"

    fh = out_path.open("wb")

    def _record(chunk: bytes) -> None:
        fh.write(chunk)
        # Flush per chunk so a mid-test crash still leaves the captured
        # bytes on disk for post-mortem promotion to a wire fixture.
        # Cost is negligible: FastReader hands off whole-buffer chunks
        # (typically <=64 KiB) at ~10 Hz max during streaming, not
        # per-byte.
        fh.flush()

    yield _record

    fh.close()
    size = out_path.stat().st_size
    if size == 0:
        # Don't litter the captures dir with empty files.
        out_path.unlink()
    else:
        print(f"\n[wire_capture] {size} bytes -> {out_path.relative_to(captures_dir.parent.parent.parent)}")


@pytest.fixture
def destructive_ok():
    """Skip the test unless DAQIFI_DESTRUCTIVE_OK=1.

    Tests that perform destructive ops (SD format, firmware flash,
    factory reset) should depend on this fixture *and* carry the
    ``@pytest.mark.hardware_destructive`` marker.
    """
    if not _destructive_ok():
        pytest.skip(
            "destructive hardware op suppressed (set DAQIFI_DESTRUCTIVE_OK=1 to enable)"
        )
    return True
