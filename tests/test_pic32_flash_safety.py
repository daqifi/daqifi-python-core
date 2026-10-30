"""#198: PIC32 flash safety.

1. HEX validation accepted data the bootloader won't erase. Since
   daqifi-nyquist-firmware #909 ERASE_FLASH clears only the lower program-flash
   panel (nvm.c APP_FlashErase, LOWER_FLASH_REGION_ERASE_OPERATION), and
   APP_ProgramHexRecord skips bytes outside APP_FLASH_BASE_ADDRESS..
   APP_FLASH_END_ADDRESS (physical 0x1D000000-0x1D0FFFFF). An image with data
   in the upper panel or boot flash is now refused before any device I/O, as
   the firmware's release script (cut_release.sh) refuses it.
2. VERIFYING only asked the bootloader for its version again. It now reads
   each programmed region's CRC back (READ_CRC, datastream.c) and compares.
3. A failure while erasing, programming or verifying left a half-written
   application. While the bootloader is still connected the flash is now
   re-erased (CLEANING_UP -> RECOVERED), so the board stays in its bootloader
   instead of starting a partial application.

The service is driven with the real Pic32BootloaderProtocol and fake HID
transport, enumerator and device (shared with the generated service tests).
"""

import importlib.util
import os
import tempfile
import threading
from pathlib import Path

import pytest

import daqifi.firmware.bootloader as bootloader
from daqifi.enums import FirmwareUpdateState
from daqifi.firmware.bootloader import Pic32BootloaderProtocol, _construct_data_packet, _decode_frame
from daqifi.firmware.crc16 import Crc16
from daqifi.firmware.update_events import FirmwareUpdateException
from daqifi.firmware.update_service import DefaultFirmwareUpdateService
from daqifi._internal.comm import HidDeviceInfo

_spec = importlib.util.spec_from_file_location(
    "service_fakes",
    Path(__file__).parent / "portomatic-generated" / "test_default_firmware_update_service.py",
)
_f = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_f)

# Looked up when used, so on code without them these tests fail as tests.
_CLEANING_UP = getattr(FirmwareUpdateState, "CLEANING_UP", "CLEANING_UP")
_RECOVERED = getattr(FirmwareUpdateState, "RECOVERED", "RECOVERED")


@pytest.fixture(autouse=True)
def _no_application_ports_on_the_host(monkeypatch):
    import daqifi.firmware.update_service as update_service

    monkeypatch.setattr(update_service, "_list_usb_serial_ports", lambda vid, pid: [])


# ---- HEX helpers --------------------------------------------------------


def _line(record: bytes) -> str:
    return ":" + (record + bytes([(-sum(record)) & 0xFF])).hex().upper()


def _base(high: int) -> str:
    return _line(bytes([2, 0, 0, 4, high >> 8, high & 0xFF]))


def _data(offset: int, data: bytes) -> str:
    return _line(bytes([len(data), offset >> 8, offset & 0xFF, 0]) + data)


_EOF = ":00000001FF"
_RESET = bytes(range(8))
_BULK = bytes(range(0x40, 0x60))  # 32 bytes, two records


def _image(*body: str):
    return [*body, _EOF]


# Two regions: 8 bytes at 0x1D000000, 32 bytes at 0x1D000480 (two records).
_GOOD = _image(
    _base(0x1D00), _data(0x0000, _RESET), _data(0x0480, _BULK[:16]), _data(0x0490, _BULK[16:]),
)


# ---- 1: the erasable range ----------------------------------------------


def test_an_image_in_the_lower_panel_is_accepted() -> None:
    assert Pic32BootloaderProtocol().parse_hex_file(_GOOD)


@pytest.mark.parametrize(
    "high, offset",
    [(0x1D15, 0x0000), (0x1D10, 0x0000), (0x1FC0, 0x0000), (0x0000, 0x0000), (0x1CFF, 0xFFF0)],
    ids=["upper-panel", "upper-panel-start", "boot-flash", "address-zero", "below-flash"],
)
def test_data_outside_the_lower_panel_is_refused(high, offset) -> None:
    image = _image(_base(0x1D00), _data(0x0480, b"\x01" * 4), _base(high), _data(offset, b"\x02" * 4))
    with pytest.raises(ValueError, match="outside the flash the bootloader erases"):
        Pic32BootloaderProtocol().parse_hex_file(image)


def test_a_record_running_past_the_lower_panel_is_refused() -> None:
    image = _image(_base(0x1D0F), _data(0xFFF8, b"\x03" * 16))
    with pytest.raises(ValueError, match="0x1D0FFFF8-0x1D100007"):
        Pic32BootloaderProtocol().parse_hex_file(image)


def test_protected_range_records_are_still_dropped_not_refused() -> None:
    image = _image(_base(0x1D00), _data(0x0480, b"\x01" * 4), _base(0x1D1E), _data(0, b"\x02" * 4))
    records = Pic32BootloaderProtocol().parse_hex_file(image)
    assert all(r[4:-1] != b"\x02" * 4 for r in records)


def test_an_out_of_range_image_is_refused_before_any_device_io() -> None:
    device = _f._FakeStreamingDevice()
    hid = _f._FakeHidTransport()
    service = _service(hid, _f._FakeHidEnumerator([]))
    path = _write(_image(_base(0x1D00), _data(0x0480, b"\x01" * 4), _base(0x1FC0), _data(0, b"\x02" * 4)))
    try:
        with pytest.raises(ValueError, match="outside the flash"):
            service.update_firmware(device, path)
    finally:
        os.remove(path)
    assert device.sent_commands == [] and hid.writes == []
    assert service.current_state == FirmwareUpdateState.IDLE


# ---- 2: READ_CRC --------------------------------------------------------


def test_crc_regions_merge_touching_records_and_split_at_gaps() -> None:
    regions = Pic32BootloaderProtocol().compute_crc_regions(_GOOD)
    assert regions == [
        bootloader.FlashCrcRegion(0x9D000000, 8, Crc16(_RESET).crc),
        bootloader.FlashCrcRegion(0x9D000480, 32, Crc16(_BULK).crc),
    ]


def test_the_read_crc_message_carries_command_address_and_length_little_endian() -> None:
    payload = _decode_frame(bootloader.create_read_crc_message(0x9D000480, 0x1234))
    assert payload == bytes([0x04]) + (0x9D000480).to_bytes(4, "little") + (0x1234).to_bytes(4, "little")


def test_read_crc_replies_decode_and_bad_ones_are_none() -> None:
    assert bootloader.decode_read_crc_response(_construct_data_packet(bytes([0x04, 0x34, 0x12]))) == 0x1234
    assert bootloader.decode_read_crc_response(_construct_data_packet(bytes([0x03, 0x34, 0x12]))) is None
    good = bytearray(_construct_data_packet(bytes([0x04, 0x34, 0x12])))
    good[2] ^= 0xFF  # corrupt a payload byte: the frame CRC no longer matches
    assert bootloader.decode_read_crc_response(bytes(good)) is None


# ---- service flow -------------------------------------------------------


def _reply(*payload: int) -> bytes:
    return _construct_data_packet(bytes(payload))


_VERSION = _reply(0x01, 1, 2)
_ERASED = _reply(0x02)
_PROGRAMMED = _reply(0x03)


def _crc(value: int) -> bytes:
    return _reply(0x04, value & 0xFF, value >> 8)


class _Late:
    """A reply that arrives only after the read for it has given up."""

    def __init__(self, reply: bytes) -> None:
        self.reply = reply


class _Bootloader(_f._FakeHidTransport):
    """Answers each write with the next scripted reply, as the bootloader
    does: nothing is readable before the request it answers. None scripts
    silence; _Late(reply) a reply that turns up just after the read for it
    timed out."""

    def __init__(self, replies) -> None:
        super().__init__()
        self._script = list(replies)
        self._ready = []
        self._late = []

    def write(self, data) -> None:
        super().write(data)
        reply = self._script.pop(0) if self._script else None
        if isinstance(reply, _Late):
            self._late.append(reply.reply)
        elif reply is not None:
            self._ready.append(reply)

    def read(self, timeout=None) -> bytes:
        if not self._ready:
            # A late reply lands just after the read waiting for it gave up.
            self._ready += self._late
            self._late = []
            raise TimeoutError("No HID report.")
        return self._ready.pop(0)


def _write(lines) -> str:
    fd, path = tempfile.mkstemp(suffix=".hex")
    with os.fdopen(fd, "w") as handle:
        handle.write("\n".join(lines) + "\n")
    return path


def _service(hid, enumerator, retries: int = 2) -> DefaultFirmwareUpdateService:
    options = _f._fast_options()
    options.flash_write_retry_count = retries
    options.wifi_recovery_timeout = 40.0  # validated even for a PIC32 update
    return DefaultFirmwareUpdateService(
        hid_transport=hid,
        firmware_download_service=_f._FakeFirmwareDownloadService(),
        external_process_runner=_f._FakeExternalProcessRunner(),
        bootloader_protocol=Pic32BootloaderProtocol(),
        hid_device_enumerator=enumerator,
        options=options,
    )


def _bootloader():
    return _f._FakeHidEnumerator([[], [HidDeviceInfo(0x04D8, 0x003C, "path-1", "", "Bootloader")]])


def _run(hid, image=_GOOD, cancel_event=None, on_state=None):
    device = _f._FakeStreamingDevice()
    service = _service(hid, _bootloader())
    states = []

    def _changed(args):
        states.append(args.current_state)
        if on_state is not None:
            on_state(args.current_state)

    service.state_changed = _changed
    path = _write(image)
    try:
        service.update_firmware(device, path, cancel_event=cancel_event)
        error = None
    except FirmwareUpdateException as exc:
        error = exc
    finally:
        os.remove(path)
    return service, states, error


def _commands(hid):
    return [_decode_frame(w)[0] for w in hid.writes]


_REGION_CRCS = (Crc16(_RESET).crc, Crc16(_BULK).crc)
# Every record of _GOOD is sent and acknowledged: address, 3 data, EOF.
_ALL_PROGRAMMED = (_PROGRAMMED,) * 5
# After a cleanup erase the reset vector and both regions read back blank.
_BLANKS = tuple(_crc(Crc16(b"\xff" * n).crc) for n in (4, 8, 32))


def test_verifying_reads_every_region_crc_back() -> None:
    hid = _Bootloader([_VERSION, _ERASED, *_ALL_PROGRAMMED, *map(_crc, _REGION_CRCS)])
    service, states, error = _run(hid)
    assert error is None and service.current_state == FirmwareUpdateState.COMPLETE
    assert _commands(hid) == [0x01, 0x02, *[0x03] * 5, 0x04, 0x04, 0x05]
    crc_requests = [_decode_frame(w) for w in hid.writes if _decode_frame(w)[0] == 0x04]
    assert [int.from_bytes(p[1:5], "little") for p in crc_requests] == [0x9D000000, 0x9D000480]
    assert [int.from_bytes(p[5:9], "little") for p in crc_requests] == [8, 32]


def test_a_crc_mismatch_fails_verification_without_a_retry_and_re_erases() -> None:
    hid = _Bootloader([_VERSION, _ERASED, *_ALL_PROGRAMMED, _crc(_REGION_CRCS[0]), _crc(_REGION_CRCS[1] ^ 0x0101), _ERASED, *_BLANKS])
    service, states, error = _run(hid)
    assert error is not None and error.failed_state == FirmwareUpdateState.VERIFYING
    assert "CRC mismatch" in str(error.__cause__)
    # Second region asked once (no retry), then the cleanup erase; no jump.
    assert _commands(hid) == [0x01, 0x02, *[0x03] * 5, 0x04, 0x04, 0x02, 0x04, 0x04, 0x04]
    assert states[-2:] == [_CLEANING_UP, _RECOVERED]
    assert service.current_state == _RECOVERED
    assert "re-erased" in error.recovery_guidance


def test_a_reply_that_isnt_a_crc_reply_fails_verification_without_a_retry() -> None:
    hid = _Bootloader([_VERSION, _ERASED, *_ALL_PROGRAMMED, b"\x01\x99\x04", _ERASED, *_BLANKS])
    service, _states, error = _run(hid)
    assert error.failed_state == FirmwareUpdateState.VERIFYING
    assert "wasn't a CRC reply" in str(error.__cause__)
    # One CRC request in verification (no retry), then the cleanup erase and
    # its three read-backs (reset vector, two regions).
    assert _commands(hid)[-5:] == [0x04, 0x02, 0x04, 0x04, 0x04]
    assert _commands(hid).count(0x04) == 4
    assert service.current_state == _RECOVERED


# Both regions hold the same bytes, so they expect the same CRC.
_TWIN = bytes(range(16))
_TWIN_IMAGE = _image(_base(0x1D00), _data(0x0000, _TWIN), _data(0x0480, _TWIN))


class _PerWrite(_Bootloader):
    """What becomes readable after each write, in order: one list per write."""

    def __init__(self, per_write) -> None:
        super().__init__([])
        self._per_write = list(per_write)

    def write(self, data) -> None:
        _f._FakeHidTransport.write(self, data)
        if self._per_write:
            self._ready += self._per_write.pop(0)


def test_a_late_program_ack_cant_make_a_corrupt_region_verify() -> None:
    # The EOF record's first ack is late, so it is retried, and the retry takes
    # the late ack; the retry's own ack then turns up only after verification's
    # drain, as the reply to region 1's CRC request. A retry of that request
    # would take region 1's real reply, and region 2 would read the retry's
    # stale reply: the regions hold equal bytes, so a corrupt region 2 would
    # verify. With no retries the stray ack fails the check as unknown.
    good = Crc16(_TWIN).crc
    hid = _PerWrite([
        [_VERSION], [_ERASED], [_PROGRAMMED], [_PROGRAMMED], [_PROGRAMMED],
        [],                                # EOF record: ack late
        [_PROGRAMMED],                     # its retry reads the late ack
        [_PROGRAMMED],                     # CRC 1: the retry's own ack, late
        [_crc(good)],                      # (CRC 1 retry): region 1's real reply
        [_crc(good), _crc(good ^ 0xFFFF)], # CRC 2: stale reply, then the bad one
        [_ERASED], [_ERASED],
    ])
    service, _states, error = _run(hid, image=_TWIN_IMAGE)
    assert error is not None and error.failed_state == FirmwareUpdateState.VERIFYING
    assert service.current_state != FirmwareUpdateState.COMPLETE


# ---- 3: re-erase after a failure ----------------------------------------


def test_a_programming_failure_re_erases_to_a_clean_bootloader() -> None:
    # The second record is never acknowledged (both attempts), then the
    # cleanup erase is.
    hid = _Bootloader([_VERSION, _ERASED, _PROGRAMMED, b"\x00", b"\x00", _ERASED, *_BLANKS])
    service, states, error = _run(hid)
    assert error.failed_state == FirmwareUpdateState.PROGRAMMING
    assert _commands(hid) == [0x01, 0x02, 0x03, 0x03, 0x03, 0x02, 0x04, 0x04, 0x04]
    assert states[-2:] == [_CLEANING_UP, _RECOVERED]
    assert "re-erased" in error.recovery_guidance


def test_a_failed_re_erase_ends_failed_and_says_the_board_may_be_half_written() -> None:
    hid = _Bootloader([_VERSION, _ERASED, _PROGRAMMED, b"\x00", b"\x00", b"\x00", b"\x00"])
    service, states, error = _run(hid)
    assert states[-2:] == [_CLEANING_UP, FirmwareUpdateState.FAILED]
    assert service.current_state == FirmwareUpdateState.FAILED
    assert error.failed_state == FirmwareUpdateState.PROGRAMMING
    assert "could not be re-erased" in error.recovery_guidance


class _DropsDuringProgramming(_Bootloader):
    """The bootloader goes away while the second record is programmed."""

    def read(self, timeout=None):
        if sum(1 for w in self.writes if _decode_frame(w)[0] == 0x03) == 2:
            self.is_connected = False
            raise OSError("device unplugged")
        return super().read(timeout)


def test_no_re_erase_when_the_bootloader_is_gone() -> None:
    hid = _DropsDuringProgramming([_VERSION, _ERASED, _PROGRAMMED])
    service, states, error = _run(hid)
    assert _CLEANING_UP not in states
    assert service.current_state == FirmwareUpdateState.FAILED
    assert _commands(hid).count(0x02) == 1
    assert "could not be re-erased" in error.recovery_guidance


class _CancelsDuringProgramming(_Bootloader):
    def __init__(self, cancel_event, replies):
        super().__init__(replies)
        self._cancel_event = cancel_event

    def write(self, data):
        super().write(data)
        if _decode_frame(data)[0] == 0x03:
            self._cancel_event.set()


def test_a_cancel_mid_flash_is_cleaned_up_too() -> None:
    cancel_event = threading.Event()
    hid = _CancelsDuringProgramming(cancel_event, [_VERSION, _ERASED, _PROGRAMMED, _ERASED, *_BLANKS])
    service, states, error = _run(hid, cancel_event=cancel_event)
    assert "canceled" in str(error).lower()
    assert _commands(hid) == [0x01, 0x02, 0x03, 0x02, 0x04, 0x04, 0x04]
    assert service.current_state == _RECOVERED


def test_a_failure_before_erase_doesnt_re_erase() -> None:
    hid = _Bootloader([b"\x00"])  # an unreadable version reply
    service, states, error = _run(hid)
    assert error.failed_state == FirmwareUpdateState.CONNECTING
    assert _CLEANING_UP not in states
    assert 0x02 not in _commands(hid)


def test_an_update_can_start_again_after_recovered() -> None:
    hid = _Bootloader([_VERSION, _ERASED, _PROGRAMMED, b"\x00", b"\x00", _ERASED, *_BLANKS])
    service, _states, _error = _run(hid)
    assert service.current_state == _RECOVERED
    path = _write(_GOOD)
    try:
        with pytest.raises(FirmwareUpdateException) as exc_info:
            service.update_firmware(_f._FakeStreamingDevice(), path)
    finally:
        os.remove(path)
    # It ran (and failed waiting for a bootloader), rather than refusing to start.
    assert exc_info.value.failed_state == FirmwareUpdateState.WAITING_FOR_BOOTLOADER


# ---- round 2 ------------------------------------------------------------


def test_a_cancel_before_the_erase_goes_out_doesnt_erase() -> None:
    # Cancelled as ERASING_FLASH is announced: no ERASE has been sent, the
    # application is intact, and no cleanup erase may destroy it.
    cancel_event = threading.Event()
    hid = _Bootloader([_VERSION, _ERASED])

    def on_state(state):
        if state == FirmwareUpdateState.ERASING_FLASH:
            cancel_event.set()

    service, states, error = _run(hid, cancel_event=cancel_event, on_state=on_state)
    assert "canceled" in str(error).lower()
    assert 0x02 not in _commands(hid)
    assert _CLEANING_UP not in states
    assert service.current_state == FirmwareUpdateState.FAILED


def test_an_unanswered_crc_request_fails_without_a_retry_that_could_take_a_late_reply() -> None:
    # Region 1's reply comes late. A retry would read it as its own answer and
    # leave its own reply queued for region 2 (replies carry no address). The
    # silence fails verification instead; the late reply is drained before the
    # cleanup erase.
    hid = _Bootloader([_VERSION, _ERASED, *_ALL_PROGRAMMED, _Late(_crc(_REGION_CRCS[0])), _ERASED, *_BLANKS])
    service, states, error = _run(hid)
    assert error.failed_state == FirmwareUpdateState.VERIFYING
    assert "didn't answer" in str(error.__cause__)
    assert _commands(hid) == [0x01, 0x02, *[0x03] * 5, 0x04, 0x02, 0x04, 0x04, 0x04]
    assert service.current_state == _RECOVERED


@pytest.mark.parametrize(
    "extra, message",
    [
        (_line(bytes([2, 0, 0, 2, 0x00, 0x00])), "type-02"),  # extended segment address
        (_line(bytes([2, 0, 0, 3, 0x00, 0x00])), "type-03"),
        (_line(bytes([4, 0, 0, 5, 0x9D, 0x00, 0x04, 0x80])), "type-05"),
    ],
)
def test_record_types_the_address_model_doesnt_implement_are_refused(extra, message) -> None:
    image = [_base(0x1D00), _data(0x0480, b"\x01" * 4), extra, _data(0x0490, b"\x02" * 4), _EOF]
    with pytest.raises(ValueError, match=message):
        Pic32BootloaderProtocol().parse_hex_file(image)


def test_records_after_the_end_of_file_are_refused() -> None:
    image = [_base(0x1D00), _data(0x0480, b"\x01" * 4), _EOF, _data(0x0490, b"\x02" * 4)]
    with pytest.raises(ValueError, match="after its end-of-file"):
        Pic32BootloaderProtocol().parse_hex_file(image)


# ---- round 4 ------------------------------------------------------------


def test_recovered_needs_the_flash_read_back_blank_not_just_an_erase_ack() -> None:
    # The bootloader acks ERASE when the NVM cycle completes, whatever the
    # outcome (nvm.c APP_NVMOperation checks no error flag): an acked erase
    # whose flash still reads back programmed is not RECOVERED.
    hid = _Bootloader([_VERSION, _ERASED, _PROGRAMMED, b"\x00", b"\x00", _ERASED,
                       _BLANKS[0], _BLANKS[1], _crc(Crc16(_BULK).crc)])
    service, states, error = _run(hid)
    assert states[-2:] == [_CLEANING_UP, FirmwareUpdateState.FAILED]
    assert service.current_state == FirmwareUpdateState.FAILED
    assert "could not be re-erased" in error.recovery_guidance


def test_an_unanswered_read_back_after_the_erase_is_not_recovered() -> None:
    hid = _Bootloader([_VERSION, _ERASED, _PROGRAMMED, b"\x00", b"\x00", _ERASED, None])
    service, states, error = _run(hid)
    assert service.current_state == FirmwareUpdateState.FAILED
    assert "could not be re-erased" in error.recovery_guidance


# ---- round 5 ------------------------------------------------------------

_SPARSE = _image(_base(0x1D0F), _data(0x0000, _RESET))  # nowhere near the reset vector


def test_recovered_needs_the_reset_vector_read_back_blank_even_outside_the_image() -> None:
    # The image's only region reads back blank (it always was), but the old
    # application's reset word survived an erase that was acked and did
    # nothing: the bootloader would start it (app.c checks APP_RESET_ADDRESS).
    reset_programmed = _crc(Crc16(b"\x00\x10\x00\x0b").crc)
    hid = _Bootloader([_VERSION, _ERASED, _PROGRAMMED, b"\x00", b"\x00", _ERASED,
                       reset_programmed, _crc(Crc16(b"\xff" * 8).crc)])
    service, states, error = _run(hid, image=_SPARSE)
    assert states[-2:] == [_CLEANING_UP, FirmwareUpdateState.FAILED]
    requests = [_decode_frame(w) for w in hid.writes if _decode_frame(w)[0] == 0x04]
    assert int.from_bytes(requests[0][1:5], "little") == 0x9D000000
    assert int.from_bytes(requests[0][5:9], "little") == 4


def test_a_sparse_image_recovers_once_the_reset_vector_and_its_region_read_blank() -> None:
    hid = _Bootloader([_VERSION, _ERASED, _PROGRAMMED, b"\x00", b"\x00", _ERASED,
                       _crc(Crc16(b"\xff" * 4).crc), _crc(Crc16(b"\xff" * 8).crc)])
    service, _states, _error = _run(hid, image=_SPARSE)
    assert service.current_state == _RECOVERED


def test_regions_closer_than_the_merge_gap_share_one_crc_window_with_the_gap_blank() -> None:
    image = _image(_base(0x1D00), _data(0x0100, b"\x01" * 16), _data(0x0180, b"\x02" * 16),
                   _data(0x0500, b"\x03" * 16))
    regions = Pic32BootloaderProtocol().compute_crc_regions(image)
    window = (b"\x01" * 16 + b"\xff" * (0x0180 - 0x0110) + b"\x02" * 16
              + b"\xff" * (0x0500 - 0x0190) + b"\x03" * 16)
    assert regions == [bootloader.FlashCrcRegion(0x9D000100, len(window), Crc16(window).crc)]


def test_regions_further_apart_than_the_merge_gap_stay_separate() -> None:
    image = _image(_base(0x1D00), _data(0x0000, b"\x01" * 16), _data(0x0410, b"\x02" * 16))
    assert len(Pic32BootloaderProtocol().compute_crc_regions(image)) == 2


def test_the_drain_before_each_crc_request_is_a_short_poll() -> None:
    from datetime import timedelta

    timeouts = []

    class _Recording(_Bootloader):
        def read(self, timeout=None):
            timeouts.append(timeout)
            return super().read(timeout)

    hid = _Recording([_VERSION, _ERASED, *_ALL_PROGRAMMED, *map(_crc, _REGION_CRCS)])
    _run(hid)
    drains = [t for t in timeouts if t is not None and t < timedelta(seconds=0.1)]
    assert drains and max(drains) <= timedelta(milliseconds=1)


def test_overlapping_records_are_refused() -> None:
    # 16 bytes at 0, then 4 at 4 and 4 at 16: the second rewrites bytes the
    # first programmed, which flash can't do without an erase.
    image = _image(_base(0x1D00), _data(0x0000, b"\x01" * 16), _data(0x0004, b"\x02" * 4),
                   _data(0x0010, b"\x03" * 4))
    with pytest.raises(ValueError, match="overlapping"):
        Pic32BootloaderProtocol().parse_hex_file(image)
    with pytest.raises(ValueError, match="overlapping"):
        Pic32BootloaderProtocol().compute_crc_regions(image)


def test_touching_records_are_not_overlapping() -> None:
    image = _image(_base(0x1D00), _data(0x0000, b"\x01" * 16), _data(0x0010, b"\x02" * 16))
    assert len(Pic32BootloaderProtocol().compute_crc_regions(image)) == 1
