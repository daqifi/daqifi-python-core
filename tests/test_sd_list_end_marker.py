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
from daqifi.exceptions import SdCardFilesystemException
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

OK = b"Daqifi/log_a.bin 3\r\n__END_OF_LIST__ OK\r\n"


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
    reply = b"Daqifi/log_a.bin 3\r\n__END_OF_LIST__ " + word + b"\r\n"
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": reply})
    with pytest.raises(SdCardListIncompleteException):
        getattr(device, method)()


def test_a_missing_directory_raises_directory_not_found() -> None:
    reply = b"\r\n[Error:5]Failed to open directory [Daqifi]\r\n__END_OF_LIST__ FAILED\r\n"
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": reply})
    with pytest.raises(SdCardDirectoryNotFoundException) as excinfo:
        device.get_sd_card_files()
    assert isinstance(excinfo.value, SdCardFilesystemException)


def test_another_open_failure_stays_a_filesystem_error() -> None:
    reply = b"\r\n[Error:3]Failed to open directory [Daqifi]\r\n__END_OF_LIST__ FAILED\r\n"
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
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"__END_OF_LIST__ OK\r\n"})
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
    device = _legacy("Daqifi/log_a.bin 3\r\n__END_OF_LIST__ OK\r\n")
    assert device.sd_list_files() == ["Daqifi/log_a.bin"]


def test_the_legacy_listing_refuses_an_incomplete_one() -> None:
    device = _legacy("Daqifi/log_a.bin 3\r\n__END_OF_LIST__ INCOMPLETE\r\n")
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
            b"SD:LIST?": b"Daqifi/log_a.bin 3\r\n__END_OF_LIST__ INCOMPLETE\r\n",
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
        {b"SD:LIST?": b"Daqifi/log_a.bin 3\r\n__END_OF_LIST__ OK\r\n"}
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
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": b"Daqifi/log_a.bin 3\r\n"})
    device._metadata.firmware_version = "3.7.3"
    assert _names(device.get_sd_card_files()) == ["log_a.bin"]


def test_a_stale_error_is_drained_before_the_listing() -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OK})
    _with_error_queue(device, [b'-200,"Execution error"\r\n'])
    assert _names(device.get_sd_card_files()) == ["log_a.bin"]


@pytest.mark.parametrize("terminator", [b'+0,"No error"\r\n', b"0,No error\r\n"])
def test_terminator_spellings_drain_errors_accepts_are_accepted(terminator) -> None:
    device = _fakes._make_device_with_serial_facade({b"SD:LIST?": OK, b"SYSTem:ERRor?": terminator})
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
