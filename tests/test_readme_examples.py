"""The README's Python examples must run as written (#197).

Several README examples failed as soon as a user ran them: the streaming
loop read ``sample.values`` (a ``Sample`` has ``channels``) and called
``time.sleep`` without importing ``time``; the SD example called a method
that returns nothing and read attributes ``SdCardFileInfo`` doesn't have;
the CSV export's ``SampleSource`` didn't accept the exporter's
``cancel_event`` and used an undefined ``values``; the readiness probe
called a ``send()`` that doesn't exist, so it never reported ready; the
Quick Start set the DAC and the input range unconditionally, which raises
on NQ1/NQ2.

``examples/basic_adc.py`` set the range unconditionally too, and
``examples/streaming_callback.py`` measured its duration from the first
sample's device-relative timestamp to ``time.time()``, about 56 years.

Each example here is executed against a fake device, so these tests need
no hardware. The blocks are found by a phrase they contain, not by their
position, so reordering the README doesn't break them.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import re
import runpy
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from daqifi import BoardVariant, DeviceFinder, NyquistDevice
from daqifi.devices import DaqifiDeviceFactory
from daqifi.exceptions import FeatureNotSupportedError
from daqifi.sdcard import SdCardFileInfo
from daqifi.streaming import Sample

REPO_ROOT = Path(__file__).resolve().parent.parent
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
PYTHON_BLOCKS = re.findall(r"```python\n(.*?)```", README, re.S)


def _block(phrase: str) -> str:
    matches = [b for b in PYTHON_BLOCKS if phrase in b]
    assert len(matches) == 1, f"expected one README python block containing {phrase!r}"
    return matches[0]


def _run(source: str, **names) -> dict:
    namespace = dict(names)
    exec(compile(source, "README.md", "exec"), namespace)
    return namespace


_real_sleep = time.sleep


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)


def test_every_python_block_compiles():
    for source in PYTHON_BLOCKS:
        compile(source, "README.md", "exec")


def test_every_daqifi_import_resolves():
    for source in PYTHON_BLOCKS:
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("daqifi"):
                module = importlib.import_module(node.module)
                for alias in node.names:
                    assert hasattr(module, alias.name), f"{node.module}.{alias.name}"


# ---- Quick Start ------------------------------------------------------------


class _NotOnThisBoard:
    """A setting that raises on assignment, as DAC voltage and input range
    do on NQ1/NQ2."""

    def __setattr__(self, name, value):
        if name in ("voltage", "range"):
            raise FeatureNotSupportedError(f"{name} not available on this board")
        object.__setattr__(self, name, value)


def _fake_nyquist(variant):
    no_dac = variant != BoardVariant.NQ3
    channel = _NotOnThisBoard if no_dac else SimpleNamespace
    return SimpleNamespace(
        variant=variant,
        power_up=lambda: None,
        disconnect=lambda: None,
        analog_outputs=[channel()],
        analog_inputs=[channel()],
        digital_io=[SimpleNamespace()],
    )


@pytest.mark.parametrize("variant", [BoardVariant.NQ1, BoardVariant.NQ2, BoardVariant.NQ3])
def test_quick_start_runs_on_every_variant(monkeypatch, variant):
    device = _fake_nyquist(variant)
    monkeypatch.setattr(DeviceFinder, "discover", staticmethod(lambda *a, **k: ["info"]))
    monkeypatch.setattr(NyquistDevice, "connect", classmethod(lambda cls, *a, **k: device))

    _run(_block("# Discover devices"))

    assert device.analog_inputs[0].enabled is True
    if variant == BoardVariant.NQ3:
        assert device.analog_outputs[0].voltage == 5.0


# ---- Streaming --------------------------------------------------------------


class _FakeStream:
    def __init__(self, channels):
        self._channels = channels
        self.callback = None

    def start(self, callback=None):
        self.callback = callback
        if callback is not None:
            callback(self._sample())

    def read(self, num_samples, timeout=None):
        return [self._sample() for _ in range(num_samples)]

    def stop(self):
        pass

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.stop()

    def _sample(self):
        return Sample(timestamp=0.001, channels={ch: 1.25 for ch in self._channels})


def test_streaming_example_runs():
    streams = []

    def create_stream(channels, sample_rate, format):
        streams.append(_FakeStream(channels))
        return streams[-1]

    device = SimpleNamespace(create_stream=create_stream)
    _run(_block("# Basic streaming (JSON format)"), device=device)

    assert streams[-1].callback is not None


# ---- CSV export -------------------------------------------------------------


def test_csv_export_example_writes_a_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    _run(_block("class InMemorySource"))

    lines = (tmp_path / "export.csv").read_text().splitlines()
    assert len(lines) >= 2  # a header and at least one row


# ---- SD card ----------------------------------------------------------------


class _FakeSdDevice:
    def __init__(self, error=None):
        self._error = error
        self.disconnected = False

    def get_sd_card_files(self):
        if self._error is not None:
            raise self._error
        return [SdCardFileInfo("log_20260101.bin", size=1024)]

    def disconnect(self):
        self.disconnected = True


@pytest.mark.parametrize(
    "error",
    [None, RuntimeError("Cannot list SD card files while logging to the SD card")],
)
def test_sd_example_runs(monkeypatch, capsys, error):
    device = _FakeSdDevice(error)
    monkeypatch.setattr(
        DaqifiDeviceFactory, "connect_serial", staticmethod(lambda *a, **k: device)
    )

    _run(_block("SdCardNotPresentException"))

    out = capsys.readouterr().out
    assert ("log_20260101.bin 1024" in out) if error is None else ("logging" in out)
    assert device.disconnected


# ---- Firmware update readiness probe ----------------------------------------


class _FakeProbeDevice:
    def __init__(self, popped):
        self._popped = popped

    def drain_errors(self, max_iterations=256, empty_budget=3):
        return list(self._popped)


def test_readiness_probe_tells_ready_from_silent():
    app_ready = _run(_block("def app_ready"))["app_ready"]

    assert app_ready(_FakeProbeDevice([]), None) is True
    assert app_ready(_FakeProbeDevice(['-200,"Execution error"']), None) is True
    silent = '99999,"DRAIN_INCONCLUSIVE: queue not confirmed clean"'
    assert app_ready(_FakeProbeDevice([silent]), None) is False


# ---- Prose ------------------------------------------------------------------


def test_architecture_tree_names_real_classes():
    tree = re.search(r"\*\*Lower-level building blocks\*\*.*?```\n(.*?)```", README, re.S)
    assert tree is not None
    checked = 0
    for line in tree.group(1).splitlines():
        entry = re.match(r"(\s*)[├└]── (\w+)\.py\s+- (.*)", line)
        if not entry:
            continue
        # The deeper-indented entries are the ones under _internal/.
        package = "daqifi._internal" if len(entry.group(1)) > 2 else "daqifi"
        module = importlib.import_module(f"{package}.{entry.group(2)}")
        described = re.sub(r"\(.*?\)", "", entry.group(3))  # drop C# asides
        for name in re.findall(r"\b[A-Z][A-Za-z]+[a-z][A-Za-z]*\b", described):
            if name in ("WiFi", "LAN", "SD"):
                continue
            assert hasattr(module, name), f"{module.__name__}.{name}"
            checked += 1
    assert checked > 0


def test_no_references_to_missing_files_or_the_wrong_repo():
    assert "testing/suites" not in README
    support = README[README.index("## Support"):]
    assert "github.com/daqifi/daqifi-python-core/issues" in support


# ---- examples/ --------------------------------------------------------------


class _FakeAnalogInputs(list):
    def configure_channels(self, channels, range="10V", enabled=True):
        for ch in channels:
            self[ch].enabled = enabled

    def disable_all(self):
        for ch in self:
            ch.enabled = False


def _example_device(variant, stream=None):
    inputs = _FakeAnalogInputs()
    for index in range(8):
        channel = _NotOnThisBoard() if variant != BoardVariant.NQ3 else SimpleNamespace()
        channel.index, channel.enabled = index, False
        # Set past the NQ1/NQ2 fake's refusal: the board reports "FIXED".
        object.__setattr__(channel, "range", "FIXED" if variant != BoardVariant.NQ3 else "1")
        inputs.append(channel)
    return SimpleNamespace(
        variant=variant,
        firmware_version="3.8.0",
        analog_inputs=inputs,
        create_stream=lambda **kwargs: stream,
        disconnect=lambda: None,
    )


@pytest.mark.parametrize("variant", [BoardVariant.NQ1, BoardVariant.NQ3])
def test_basic_adc_example_runs_on_nq1_and_nq3(monkeypatch, variant):
    device = _example_device(variant)
    monkeypatch.setattr(NyquistDevice, "connect", classmethod(lambda cls, *a, **k: device))

    runpy.run_path(str(REPO_ROOT / "examples" / "basic_adc.py"), run_name="__main__")

    assert not any(ch.enabled for ch in device.analog_inputs)


class _TimedStream:
    """Delivers 1000 samples one millisecond apart, device-relative."""

    def __init__(self):
        self.buffer = []

    def start(self, callback=None):
        for i in range(1000):
            callback(Sample(timestamp=5.0 + i / 1000.0, channels={0: 0.0, 1: 0.0}))

    def stop(self):
        pass


def test_streaming_callback_example_reports_device_time_duration(monkeypatch, capsys):
    device = _example_device(BoardVariant.NQ3, stream=_TimedStream())
    monkeypatch.setattr(NyquistDevice, "connect", classmethod(lambda cls, *a, **k: device))

    runpy.run_path(str(REPO_ROOT / "examples" / "streaming_callback.py"), run_name="__main__")

    duration = re.search(r"Duration: ([0-9.]+) seconds", capsys.readouterr().out)
    assert duration is not None
    assert float(duration.group(1)) == pytest.approx(0.999, abs=0.01)


def _stream_fakes():
    spec = importlib.util.spec_from_file_location(
        "stream_fakes", REPO_ROOT / "tests" / "test_stream_manager_startup_data.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_streaming_callback_example_duration_spans_a_tick_rollover(monkeypatch, capsys, tmp_path):
    # A real StreamManager reads four JSON records one second apart that
    # straddle the device's 32-bit tick rollover; the unwrap (#193) keeps
    # the timestamps rising, so the example reports 3 s, not a negative time.
    fakes = _stream_fakes()
    tick_hz, wrap = 50_000_000, 2 ** 32
    ticks = [wrap - 75_000_000, wrap - 25_000_000, 25_000_000, 75_000_000]
    data = b'{"meta":{"dev":"Nyquist 3","sn":"0","tick_hz":%d}}\n' % tick_hz + b"".join(
        b'{"ts":%d,"ai":[{"ch":0,"val":1000},{"ch":1,"val":1000}]}\n' % t for t in ticks
    )

    def create_stream(channels, sample_rate=1000, format=1):
        fake = fakes._FakeSerial(enabled_mask=0b11, on_start=data)
        return fakes._manager(fake, channels=channels, sample_rate=sample_rate, format=format)

    device = _example_device(BoardVariant.NQ3)
    device.create_stream = create_stream
    monkeypatch.setattr(NyquistDevice, "connect", classmethod(lambda cls, *a, **k: device))
    monkeypatch.chdir(tmp_path)
    # The example's 10 s wait: long enough here for the reader to deliver all four.
    real_sleep = _real_sleep
    monkeypatch.setattr(time, "sleep", lambda seconds: real_sleep(min(seconds, 0.5)))

    runpy.run_path(str(REPO_ROOT / "examples" / "streaming_callback.py"), run_name="__main__")

    out = capsys.readouterr().out
    assert "Total samples: 4" in out
    duration = re.search(r"Duration: (-?[0-9.]+) seconds", out)
    assert duration is not None
    assert float(duration.group(1)) == pytest.approx(3.0, abs=0.01)
