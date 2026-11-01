"""#188: the firmware's end-of-listing marker and incomplete listings.

Firmware main (sd_card_manager.c) ends every SD:LIST? reply with
"__END_OF_LIST__ OK", "INCOMPLETE" or "FAILED" (SD_LIST_END_*), and answers a
directory it can't open with "[Error:N]Failed to open directory [path]"
(N = SYS_FS_ERROR_NO_FILE 4 / NO_PATH 5 when it doesn't exist). The listing
now also sends SYSTem:ERRor? so a reply that never finished is told apart
from an empty card.
"""

import importlib.util
import os

import pytest

import daqifi.exceptions as _exc
import daqifi.sdcard as _sd
from daqifi.exceptions import SdCardFilesystemException, SdCardOperationException
from daqifi.sdcard import parse_sd_card_file_list


class _NotDefined(Exception):
    """Stands in for a name the tree under test lacks, so the tests still run
    against it and fail on behaviour rather than at import."""


SdCardDirectoryNotFoundException = getattr(_exc, "SdCardDirectoryNotFoundException", _NotDefined)
SdCardListIncompleteException = getattr(_exc, "SdCardListIncompleteException", _NotDefined)
sd_card_listing_status = getattr(_sd, "sd_card_listing_status", lambda lines: None)

_HERE = os.path.dirname(__file__)
_spec = importlib.util.spec_from_file_location(
    "sd_fakes", os.path.join(_HERE, "portomatic-generated", "test_sd_card_operations_impl.py")
)
_fakes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fakes)

# The firmware's own bytes (sd_card_manager.c, daqifi-nyquist-firmware main
# 2f8a5d5c9): each entry is "%s %u\r\n", and the marker has a leading CR/LF
# and NO line ending, so the next reply is glued to it.
SD_LIST_END_OK = b"\r\n__END_OF_LIST__ OK"
SD_LIST_END_INCOMPLETE = b"\r\n__END_OF_LIST__ INCOMPLETE"
SD_LIST_END_FAILED = b"\r\n__END_OF_LIST__ FAILED"
ENTRY = b"Daqifi/log_a.bin 3\r\n"
OK = ENTRY + SD_LIST_END_OK
# Before 3.8.0 there is no marker, and ListFilesInDirectoryChunked strips the
# final chunk's trailing CR/LF (firmware tag v3.7.3, sd_card_manager.c,
# "Remove trailing CRLF from final chunk"), so the last entry has no line
# ending and the SYSTem:ERRor? reply is glued to it.
OLD_LISTING = b"Daqifi/log_a.bin 3"
# SCPIStorageSD.c SD_CARD_NOT_PRESENT_ERROR_MSG (main 2f8a5d5c9).
NO_CARD = b"\r\nError !! No SD Card Detected\r\n"


def _names(files):
    return [f.file_name for f in files]


def test_the_marker_is_not_a_file() -> None:
    assert _names(parse_sd_card_file_list(["Daqifi/log_a.bin 3", "", "__END_OF_LIST__ OK"])) == ["log_a.bin"]


def test_a_file_named_like_the_marker_is_still_a_file() -> None:
    assert _names(parse_sd_card_file_list(["Daqifi/__END_OF_LIST__notes.csv 4"])) == ["__END_OF_LIST__notes.csv"]


@pytest.mark.parametrize(
    "lines, status",
    [
        (["a.bin 1", "__END_OF_LIST__ OK"], "complete"),
        (["a.bin 1", "__END_OF_LIST__ INCOMPLETE"], "incomplete"),
        (["[Error:5]Failed to open directory [Daqifi]", "__END_OF_LIST__ FAILED"], "failed"),
        (["__END_OF_LIST__ WHATEVER"], "incomplete"),
        (["a.bin 1"], "unterminated"),
        # A status is final once seen; a later line (an echo, say) keeps it.
        (["a.bin 1", "__END_OF_LIST__ INCOMPLETE", "SYSTem:ERRor?"], "incomplete"),
    ],
)
def test_the_listing_status_comes_from_the_last_marker(lines, status) -> None:
    assert sd_card_listing_status(lines) == status


@pytest.mark.parametrize("method", ["get_sd_card_files", "request_sd_card_file_list"])
def test_a_complete_listing_has_no_marker_entry(method) -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OK})
    getattr(device, method)()
    assert _names(device.sd_card_files) == ["log_a.bin"]


@pytest.mark.parametrize("method", ["get_sd_card_files", "request_sd_card_file_list"])
@pytest.mark.parametrize("word", [b"INCOMPLETE", b"FAILED"])
def test_a_listing_marked_incomplete_or_failed_raises(method, word) -> None:
    reply = ENTRY + b"\r\n__END_OF_LIST__ " + word
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": reply})
    with pytest.raises(SdCardListIncompleteException):
        getattr(device, method)()


def test_a_missing_directory_raises_directory_not_found() -> None:
    reply = b"\r\n[Error:5]Failed to open directory [Daqifi]\r\n" + SD_LIST_END_FAILED
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": reply})
    with pytest.raises(SdCardDirectoryNotFoundException) as excinfo:
        device.get_sd_card_files()
    assert isinstance(excinfo.value, SdCardFilesystemException)


def test_another_open_failure_stays_a_filesystem_error() -> None:
    reply = b"\r\n[Error:3]Failed to open directory [Daqifi]\r\n" + SD_LIST_END_FAILED
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": reply})
    with pytest.raises(SdCardFilesystemException) as excinfo:
        device.get_sd_card_files()
    assert not isinstance(excinfo.value, SdCardDirectoryNotFoundException)


@pytest.mark.parametrize("method", ["get_sd_card_files", "request_sd_card_file_list"])
def test_a_silent_device_is_not_an_empty_card(method) -> None:
    device = _fakes._make_device_with_serial_facade()
    # The drain before the listing is answered; the listing's terminator isn't.
    _with_error_queue(device, [b'0,"No error"\r\n', b""], then=b"")
    with pytest.raises(SdCardListIncompleteException):
        getattr(device, method)()
    sent = b"".join(device._transport.stream.written)
    assert sent.count(b"SD:LIST?") == 1  # no automatic retry (audit r2)


def test_an_empty_card_with_a_terminator_is_empty() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": SD_LIST_END_OK})
    assert device.get_sd_card_files() == ()


def test_a_delete_whose_relisting_never_finishes_is_not_retried() -> None:
    device = _fakes._make_device_with_serial_facade()
    _with_error_queue(device, [b'0,"No error"\r\n', b""], then=b"")
    with pytest.raises(SdCardListIncompleteException):
        device.delete_sd_card_file("log_a.bin")
    sent = b"".join(device._transport.stream.written)
    assert sent.count(b"SD:DELete") == 1


def _legacy(reply):
    from unittest import mock

    from daqifi.device import NyquistDevice

    device = NyquistDevice("/dev/null-fake")
    device._comm = mock.MagicMock()
    device._comm.send_command.return_value = reply
    return device


def test_the_legacy_listing_skips_the_marker() -> None:
    device = _legacy("Daqifi/log_a.bin 3\r\n\r\n__END_OF_LIST__ OK")
    assert device.sd_list_files() == ["Daqifi/log_a.bin"]


def test_the_legacy_listing_refuses_an_incomplete_one() -> None:
    device = _legacy("Daqifi/log_a.bin 3\r\n\r\n__END_OF_LIST__ INCOMPLETE")
    with pytest.raises(SdCardListIncompleteException):
        device.sd_list_files()


# Audit r1 on #212.

_ECHOED_TERMINATOR = b'DAQIFI> SYSTem:ERRor?\r\n0,"No error"\r\n'


@pytest.mark.parametrize("method", ["get_sd_card_files", "request_sd_card_file_list", "delete"])
def test_an_echo_of_the_terminator_query_does_not_hide_incomplete(method) -> None:
    # With SYSTem:ECHO 1 the echo of SYSTem:ERRor? followed the INCOMPLETE
    # marker, reset the status to "unterminated" and became a file.
    device = _fakes._make_device_with_serial_facade(
        {
            b"SD:LIST?": ENTRY + SD_LIST_END_INCOMPLETE,
            b"SYSTem:ERRor?": _ECHOED_TERMINATOR,
        }
    )
    with pytest.raises(SdCardListIncompleteException):
        if method == "delete":
            device.delete_sd_card_file("log_a.bin")
        else:
            getattr(device, method)()


@pytest.mark.parametrize("method", ["get_sd_card_files", "request_sd_card_file_list"])
def test_an_echo_is_never_a_file(method) -> None:
    device = _fakes._make_device_with_serial_facade(
        {b"SD:LIST?": b"DAQIFI> SYSTem:STORage:SD:LIST?\r\n" + OK, b"SYSTem:ERRor?": _ECHOED_TERMINATOR}
    )
    getattr(device, method)()
    assert _names(device.sd_card_files) == ["log_a.bin"]


def test_a_missing_terminator_is_not_retried() -> None:
    # Audit r2: a timed-out attempt's tail could arrive during a retry and
    # end it, and no quiet interval proves the first attempt finished, so
    # there is no automatic retry; the caller decides.
    device = _fakes._make_device_with_serial_facade(
        {b"SD:LIST?": OK}
    )
    _with_error_queue(device, [b'0,"No error"\r\n', b""], then=b"")
    device._sd_card_files = ["sentinel"]
    with pytest.raises(SdCardListIncompleteException):
        device.get_sd_card_files()
    assert b"".join(device._transport.stream.written).count(b"SD:LIST?") == 1
    assert device._sd_card_files == ["sentinel"]  # nothing cached


def _with_error_queue(device, replies, then=b'0,"No error"\r\n'):
    """Answer successive SYSTem:ERRor? queries from ``replies``, then ``then``."""
    stream = device._transport.stream
    stream.response_map.pop(b"SYSTem:ERRor?", None)
    real_write = stream.write
    queue = list(replies)

    def _write(data):
        n = real_write(data)
        if b"SYSTem:ERRor?" in bytes(data):
            stream.feed(queue.pop(0) if queue else then)
        return n

    stream.write = _write


@pytest.mark.parametrize("version", ["3.8.0", "3.9.1", ""])
def test_an_aborted_listing_with_files_is_refused(version) -> None:
    # Audit r2: the firmware sends no marker on abort and answers ERR? with
    # -200; the files that did arrive were cached as the whole card.
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"Daqifi/log_a.bin 3\r\n"})
    device._metadata.firmware_version = version
    # Drain sees an empty queue; the listing's own terminator says -200.
    _with_error_queue(device, [b'0,"No error"\r\n', b'-200,"Execution error"\r\n'])
    with pytest.raises(SdCardListIncompleteException):
        device.get_sd_card_files()


def test_a_missing_marker_is_refused_on_firmware_that_always_sends_one() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"Daqifi/log_a.bin 3\r\n"})
    device._metadata.firmware_version = "3.8.0"
    with pytest.raises(SdCardListIncompleteException):
        device.get_sd_card_files()


def test_a_listing_without_a_marker_from_older_firmware_is_accepted() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OLD_LISTING})
    device._metadata.firmware_version = "3.7.3"
    assert _names(device.get_sd_card_files()) == ["log_a.bin"]


def test_a_stale_error_is_drained_before_the_listing() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OK})
    _with_error_queue(device, [b'-200,"Execution error"\r\n'])
    assert _names(device.get_sd_card_files()) == ["log_a.bin"]


@pytest.mark.parametrize("terminator", [b'+0,"No error"\r\n', b"0,No error\r\n"])
def test_terminator_spellings_drain_errors_accepts_are_accepted_after_the_marker(terminator) -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OK, b"SYSTem:ERRor?": terminator})
    device._metadata.firmware_version = "3.8.0"
    assert _names(device.get_sd_card_files()) == ["log_a.bin"]


def test_no_files_and_no_marker_is_not_an_empty_card_on_marking_firmware() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b""})
    device._metadata.firmware_version = "3.8.0"
    with pytest.raises(SdCardListIncompleteException):
        device.get_sd_card_files()


# Audit r3 on #212.

def test_an_empty_directory_on_older_firmware_is_empty() -> None:
    # Before 3.8.0 no listing is marked; a clean terminator is the evidence.
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b""})
    device._metadata.firmware_version = "3.7.3"
    assert device.get_sd_card_files() == ()


def test_deleting_the_last_file_on_older_firmware_lists_an_empty_card() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b""})
    device._metadata.firmware_version = "3.7.3"
    device.delete_sd_card_file("log_a.bin")
    assert device.sd_card_files == ()


def test_seventeen_stale_errors_are_all_drained() -> None:
    # drain_errors stopped at 16 and its "inconclusive" result was ignored,
    # so the 17th stale error was blamed on a healthy listing.
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OK})
    _with_error_queue(device, [b'-200,"Execution error"\r\n'] * 17)
    assert _names(device.get_sd_card_files()) == ["log_a.bin"]


def test_an_error_queue_that_cannot_be_emptied_is_not_blamed_on_the_listing() -> None:
    from daqifi.exceptions import SdCardOperationException

    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OK})
    _with_error_queue(device, [], then=b"")  # the drain never gets an answer
    with pytest.raises(SdCardOperationException) as excinfo:
        device.get_sd_card_files()
    assert "could not be emptied" in str(excinfo.value)
    assert not isinstance(excinfo.value, SdCardListIncompleteException)
    assert b"SD:LIST?" not in b"".join(device._transport.stream.written)


# Audit r4 on #212.

@pytest.mark.parametrize("method", ["get_sd_card_files", "request_sd_card_file_list"])
def test_the_firmwares_marker_with_the_reply_glued_on_is_a_complete_listing(method) -> None:
    # The marker has no line ending, so '__END_OF_LIST__ OK0,"No error"'
    # arrives as one line; every successful listing used to raise.
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OK})
    device._metadata.firmware_version = "3.8.0"
    getattr(device, method)()
    assert _names(device.sd_card_files) == ["log_a.bin"]


def test_a_filename_shaped_like_a_reply_does_not_end_a_marked_listing() -> None:
    device = _fakes._make_device_with_serial_facade(
        {b"SD:LIST?": ENTRY + b"0,logs/b.bin 4\r\n" + SD_LIST_END_INCOMPLETE}
    )
    device._metadata.firmware_version = "3.8.0"
    with pytest.raises(SdCardListIncompleteException):
        device.get_sd_card_files()


def test_a_filename_shaped_like_a_reply_is_not_a_terminator_on_older_firmware() -> None:
    # Old firmware has no marker; only the quoted form the firmware sends
    # counts, so an unanswered SYSTem:ERRor? isn't faked by a filename.
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": ENTRY + b"0,logs/b.bin 4"})
    device._metadata.firmware_version = "3.7.3"
    _with_error_queue(device, [b'0,"No error"\r\n', b""], then=b"")
    with pytest.raises(SdCardListIncompleteException):
        device.get_sd_card_files()


def test_a_failed_delete_does_not_hide_an_aborted_relisting() -> None:
    # The DELete's own error is read by its own SYSTem:ERRor?, and it's the
    # answer: the device refused (#200 audit r8; it used to be thrown away,
    # leaving only the listing's -200 to fail on).
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OLD_LISTING})
    device._metadata.firmware_version = "3.7.3"
    _with_error_queue(
        device,
        [b'0,"No error"\r\n', b'-256,"File name not found"\r\n', b'-200,"Execution error"\r\n'],
    )
    with pytest.raises(SdCardOperationException, match="-256"):
        device.delete_sd_card_file("log_a.bin")


def test_a_delete_answered_clean_does_not_hide_an_aborted_relisting() -> None:
    # The listing's -200 is the listing's own, so the delete's outcome is
    # unknown (a subclass of the listing's error).
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OLD_LISTING})
    device._metadata.firmware_version = "3.7.3"
    _with_error_queue(
        device,
        [b'0,"No error"\r\n', b'0,"No error"\r\n', b'-200,"Execution error"\r\n'],
    )
    with pytest.raises(SdCardListIncompleteException):
        device.delete_sd_card_file("log_a.bin")


def test_a_successful_delete_relists_the_rest() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"Daqifi/log_b.bin 5\r\n" + SD_LIST_END_OK})
    device._metadata.firmware_version = "3.8.0"
    device.delete_sd_card_file("log_a.bin")
    assert _names(device.sd_card_files) == ["log_b.bin"]


# Audit r5 on #212.

@pytest.mark.parametrize("method", ["get_sd_card_files", "request_sd_card_file_list"])
def test_an_old_firmware_listing_with_the_reply_glued_to_its_last_entry(method) -> None:
    # 'Daqifi/log_a.bin 30,"No error"': the entry's size is 3, the reply's
    # code 0. Every successful old-firmware listing used to raise.
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OLD_LISTING})
    device._metadata.firmware_version = "3.7.3"
    getattr(device, method)()
    assert [(f.file_name, f.size) for f in device.sd_card_files] == [("log_a.bin", 3)]


def test_an_old_firmware_abort_glued_to_its_last_entry_is_refused() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OLD_LISTING})
    device._metadata.firmware_version = "3.7.3"
    _with_error_queue(device, [b'0,"No error"\r\n', b'-200,"Execution error"\r\n'])
    with pytest.raises(SdCardListIncompleteException):
        device.get_sd_card_files()


@pytest.mark.parametrize("version", ["3.8.0", "3.7.3"])
def test_a_missing_card_raises_not_present_whatever_the_firmware(version) -> None:
    # No card: the firmware prints its message, queues -200 and sends no
    # marker. The missing-marker rule used to win over the explicit report.
    from daqifi.exceptions import SdCardNotPresentException

    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": NO_CARD})
    device._metadata.firmware_version = version
    _with_error_queue(device, [b'0,"No error"\r\n', b'-200,"Execution error"\r\n'])
    with pytest.raises(SdCardNotPresentException):
        device.get_sd_card_files()
