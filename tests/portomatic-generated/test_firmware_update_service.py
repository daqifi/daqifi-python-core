"""Tests for the FirmwareUpdateService Protocol port (closes #7)."""
from __future__ import annotations

from typing import Callable, Optional

import pytest

from daqifi.enums import FirmwareUpdateState
from daqifi.firmware import FirmwareUpdateService
from daqifi.firmware.update_events import (
    FirmwareUpdateProgress,
    FirmwareUpdateStateChangedEventArgs,
)


pytestmark = pytest.mark.portomatic_generated


class _CompleteStub:
    """Minimal complete impl exercising every Protocol member."""

    def __init__(self) -> None:
        self._state = FirmwareUpdateState.IDLE
        self.state_changed: Optional[
            Callable[[FirmwareUpdateStateChangedEventArgs], None]
        ] = None
        self.firmware_calls: list[tuple] = []
        self.wifi_calls: list[tuple] = []

    @property
    def current_state(self) -> FirmwareUpdateState:
        return self._state

    def update_firmware(
        self,
        device,
        hex_file_path: str,
        progress: Optional[Callable[[FirmwareUpdateProgress], None]] = None,
    ) -> None:
        self.firmware_calls.append((device, hex_file_path, progress))

    def update_wifi_module(
        self,
        device,
        firmware_path: str,
        progress: Optional[Callable[[FirmwareUpdateProgress], None]] = None,
    ) -> None:
        self.wifi_calls.append((device, firmware_path, progress))


class TestFirmwareUpdateServiceProtocol:
    def test_runtime_checkable_complete_stub_passes(self):
        assert isinstance(_CompleteStub(), FirmwareUpdateService)

    def test_incomplete_stub_fails_isinstance(self):
        class _Incomplete:
            @property
            def current_state(self) -> FirmwareUpdateState:
                return FirmwareUpdateState.IDLE

            # Missing state_changed, update_firmware, update_wifi_module.

        assert not isinstance(_Incomplete(), FirmwareUpdateService)

    def test_state_changed_attribute_can_be_set_and_cleared(self):
        # The Protocol declares state_changed as a settable attribute.
        # Implementations should let callers attach/detach observers.
        stub = _CompleteStub()
        captured: list[FirmwareUpdateStateChangedEventArgs] = []

        stub.state_changed = captured.append
        # Simulate the impl firing the event
        if stub.state_changed is not None:
            stub.state_changed(
                FirmwareUpdateStateChangedEventArgs(
                    previous_state=FirmwareUpdateState.IDLE,
                    current_state=FirmwareUpdateState.PREPARING_DEVICE,
                )
            )
        assert len(captured) == 1
        assert captured[0].current_state is FirmwareUpdateState.PREPARING_DEVICE

        stub.state_changed = None
        # Detached observer no longer fires.
        assert stub.state_changed is None

    def test_update_firmware_signature(self):
        stub = _CompleteStub()
        progress_calls: list[FirmwareUpdateProgress] = []

        def _progress(p: FirmwareUpdateProgress) -> None:
            progress_calls.append(p)

        stub.update_firmware(
            device=object(),
            hex_file_path="/tmp/firmware.hex",
            progress=_progress,
        )
        assert len(stub.firmware_calls) == 1
        assert stub.firmware_calls[0][1] == "/tmp/firmware.hex"
        # Identity comparison works on regular functions (unlike
        # bound-methods, which return a fresh wrapper each access).
        assert stub.firmware_calls[0][2] is _progress

    def test_update_wifi_module_signature(self):
        stub = _CompleteStub()
        stub.update_wifi_module(
            device=object(),
            firmware_path="/tmp/wifi-tool",
        )
        assert len(stub.wifi_calls) == 1
        assert stub.wifi_calls[0][1] == "/tmp/wifi-tool"
        assert stub.wifi_calls[0][2] is None

    def test_progress_callback_is_optional(self):
        # Both update_firmware and update_wifi_module accept None for
        # progress. Implementations must handle the no-callback case.
        stub = _CompleteStub()
        stub.update_firmware(device=object(), hex_file_path="x.hex")
        stub.update_wifi_module(device=object(), firmware_path="tool")
        assert stub.firmware_calls[0][2] is None
        assert stub.wifi_calls[0][2] is None

    def test_module_exports(self):
        from daqifi import firmware as fw_module

        # Re-export check — keeps the public surface honest.
        assert hasattr(fw_module, "FirmwareUpdateService")
        assert fw_module.FirmwareUpdateService is FirmwareUpdateService
