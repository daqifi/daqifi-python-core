"""B-fillers — ``NyquistDevice`` collection-property accessors.

Smaller follow-up to B7/B8/B9: covers the high-level collection-
accessor properties that the per-collection slices implicitly relied
on but never asserted in their own right:

* ``NyquistDevice.analog_inputs`` — returns a populated
  ``AnalogInputs`` after connect (FeatureNotSupportedError on a
  device with no ADC, but every Nq variant ships ADC so that
  branch is dead in practice).
* ``NyquistDevice.digital_io`` — returns a populated ``DigitalIO``
  after connect.
* ``NyquistDevice.analog_outputs`` — raises
  ``FeatureNotSupportedError`` on Nq1/Nq2 (DAC is Nq3-only); we
  test the raise path here since we don't have an Nq3 in the
  test rig.
* ``NyquistDevice.create_stream`` — sync mode (default) returns
  ``StreamManager``; async mode returns ``AsyncStreamManager``
  (latter exercised in B14).
* ``StreamManager.buffer`` — exposes the buffer that ``read_all``
  populates; mutation via ``add_sample`` reflects in subsequent
  reads.

Hardware safety: pure-property reads + one no-side-effects
StreamManager construction; no streaming engaged.
"""
from __future__ import annotations

from typing import Iterator

import pytest

from daqifi.channels import AnalogInputs, DigitalIO
from daqifi.device import NyquistDevice
from daqifi.discovery import BoardVariant, DeviceInfo
from daqifi.exceptions import FeatureNotSupportedError
from daqifi.streaming import (
    AsyncStreamManager,
    Sample,
    SampleBuffer,
    StreamManager,
)


@pytest.fixture
def device(device_info: DeviceInfo) -> Iterator[NyquistDevice]:
    """Open NyquistDevice; teardown disconnects."""
    port = device_info.port
    if not port:
        pytest.skip(f"device_info has no serial port: {device_info!r}")
    dev = NyquistDevice.connect(port.strip())
    try:
        yield dev
    finally:
        try:
            dev.disconnect()
        except Exception:
            pass


# ---- Collection accessors -------------------------------------------------


def test_analog_inputs_returns_populated_collection(
    device: NyquistDevice,
) -> None:
    """``device.analog_inputs`` returns an ``AnalogInputs`` instance
    populated with channels (16 on Nq1/Nq2, 8 on Nq3)."""
    inputs = device.analog_inputs
    assert isinstance(inputs, AnalogInputs)
    assert len(inputs) > 0


def test_digital_io_returns_populated_collection(
    device: NyquistDevice,
) -> None:
    """``device.digital_io`` returns a ``DigitalIO`` instance
    populated with 16 channels (every Nyquist variant)."""
    dio = device.digital_io
    assert isinstance(dio, DigitalIO)
    assert len(dio) == 16


def test_analog_outputs_raises_on_non_nq3(
    device: NyquistDevice,
) -> None:
    """On Nq1 / Nq2 (no DAC), ``device.analog_outputs`` raises
    ``FeatureNotSupportedError`` with a diagnostic message naming
    the variant. Skipped on Nq3 (positive path)."""
    if device.variant == BoardVariant.NQ3:
        pytest.skip("Nq3 has DAC; this test covers the raise path on Nq1/Nq2")
    with pytest.raises(FeatureNotSupportedError, match="NQ3"):
        _ = device.analog_outputs


# ---- create_stream factory ------------------------------------------------


def test_create_stream_default_returns_sync_manager(
    device: NyquistDevice,
) -> None:
    """``create_stream(...)`` (default async_mode=False) returns a
    ``StreamManager``. No SCPI traffic — just constructor."""
    stream = device.create_stream(channels=[0], sample_rate=100)
    assert isinstance(stream, StreamManager)


def test_create_stream_async_mode_returns_async_manager(
    device: NyquistDevice,
) -> None:
    """``create_stream(async_mode=True)`` returns an
    ``AsyncStreamManager`` (the async branch of the factory)."""
    stream = device.create_stream(
        channels=[0], sample_rate=100, async_mode=True
    )
    assert isinstance(stream, AsyncStreamManager)


# ---- StreamManager.buffer property ---------------------------------------


def test_stream_manager_buffer_property_exposes_internal_buffer(
    device: NyquistDevice,
) -> None:
    """``stream.buffer`` returns the StreamManager's internal
    ``SampleBuffer``. Adding a sample directly to the returned
    buffer is reflected on the next access — confirming the
    property exposes the SAME instance, not a defensive copy.
    (StreamManager.read_all relies on this — it returns
    ``self._buffer`` directly.)"""
    stream = device.create_stream(channels=[0], sample_rate=100)
    buf1 = stream.buffer
    assert isinstance(buf1, SampleBuffer)

    sentinel = Sample(timestamp=42.0, channels={0: 0.123})
    buf1.add_sample(sentinel)

    buf2 = stream.buffer
    assert buf2 is buf1
    assert sentinel in buf2.samples
