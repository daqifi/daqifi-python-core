# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - TBD

The first tagged release. Everything since the 0.1.0 development version,
which was never tagged.

### Behaviour changes
- A serial `SerialInterface.open()` now FAILS on a port that will not take
  output, instead of hanging. After writing its echo-disable command it waits
  at most 1 s (`open_drain_timeout`) for the output queue to empty; a port
  that misses that, raises an I/O error while draining, or has no
  `out_waiting` is refused and released, and `connect()` raises
  `SerialPortConnectError` with the original error as `__cause__`. A failed
  echo-disable write is still tolerated, as before (#262, #267).
- Every serial send now RAISES after 5 s (`send_drain_timeout`) if the
  device stops taking bytes, instead of blocking the calling thread forever
  with no exception. `send_command` and `query_binary` raise `RuntimeError`,
  stream start raises `CommandError`, and `sd_get_file`,
  `sd_get_file_binary` and a serial `DaqifiDevice` send raise
  `serial.SerialTimeoutException` (or the raw `OSError` if the port died
  mid-drain). No serial send calls `flush()` any more. A healthy port is
  unaffected; its queue drains in milliseconds (#263, #267).
- `SerialInterface.close()` (and so `NyquistDevice.disconnect()`) no longer
  hangs on a device that stopped draining. It waits at most 20 s
  (`close_drain_timeout`), always releases the port, and returns `False`
  with the cause in `last_close_error` instead of raising (#259, #261).
- `NyquistDevice.sd_get_file()` and `sd_get_file_binary()` return the
  file's bytes as sent. They used to delete every occurrence of the GET
  command, the `DAQIFI>` prompt and the end marker anywhere in the file,
  drop everything before a `DAQIFI>` in the first 200 bytes, and `.strip()`
  leading and trailing whitespace. Now only the echoed command and prompt
  at the very head are removed, and only when a prompt proves they are
  framing. They no longer raise `SdCardTransferErrorException`: a transfer
  the device aborts times out instead, and a file containing
  `__END_OF_FILE__` still ends there. See Known issues for the one head
  shape that is still stripped (#154, #206 item 5, #257).
- `NyquistDevice.sd_get_file_size()` returns 0 (unknown) instead of a stale
  cached size after `sd_enable`, `sd_set_logging`, `sd_delete_file`,
  `sd_format`, `sd_benchmark` or `disconnect`. Each of those can change or
  invalidate what is on the card, so a size cached before it no longer
  describes the file (#257).
- `send()` now WAITS while another flow holds the device (see Changed), and
  can raise `ExchangeTimeoutError` instead of returning (#238, #251).

### Added
- `NyquistDevice.connect(..., verify=callable)` runs `callable(port)` on the
  freshly opened port BEFORE the library writes anything to it, on the same
  handle the library then uses, so checking which board is on a path (for
  example with `*IDN?`) no longer changes that board first. A falsy result
  raises `DeviceVerificationError` (in `daqifi.exceptions`) and an exception
  from the callable is re-raised unchanged; either way the port is closed
  with nothing written. A cancellation wins over a truthy verdict (#259,
  #261).
- `SerialInterface.open_drain_timeout` (1 s), `send_drain_timeout` (5 s),
  `close_drain_timeout` (20 s) and `last_close_error` (#261, #267).
- `ExchangeTimeoutError`: a `send()` could not get exclusive use of the
  device, and the command was NOT sent -- from an ordinary thread after
  `send_exchange_wait_s` (60 s by default, settable per device), or at once
  from the message-received callback thread while another flow holds the
  device (#238, #251).
- `update_wifi_module(..., skip_version_check=True)` flashes the WiFi module
  even when it already reports the latest published version. Mirrors
  `skipVersionCheck` on the C# `UpdateWifiModuleAsync`. Without it an
  up-to-date module cannot be reflashed at all, so the WiFi update path cannot
  be exercised on a bench whose boards are already current. It overrides the
  version check only — every other refusal on the path still applies (#190).
- `DaqifiDevice.delivery_fault` and `DeliveryFaultError`: why this
  connection can no longer be trusted to have received what it was told --
  today, a message consumer that could not be restarted after a text
  exchange, so nothing is reading the stream. `start_streaming()` raises it
  instead of reporting success, `is_streaming` reads False, and a successful
  `connect()` clears it (#238).
- `DaqifiDevice.run_exclusive(fn)` runs `fn()` as one exchange, so a
  sequence of commands cannot be interleaved with another thread's `send()`
  or text exchange — including a configure-then-query sequence or an SD
  listing, since holding the exchange lock is the whole permission to nest
  an exchange. Re-entrant for a thread already inside an exchange on that
  device; refused on the message-received callback thread, and inside a
  text capture (from a `setup_action`), which is the nesting that would
  pause an already-paused consumer or move the SPI bus under a live
  reader (#225).
- `execute_text_command(..., max_total_ms=...)` caps the whole capture;
  without it a device that keeps sending lines keeps the capture going
  (#190).
- `CONTRIBUTING.md`, `SECURITY.md` and this changelog.
- `pyproject.toml` declaring the PEP 517 build backend and the pytest
  configuration (#155).
- `HidLibraryTransport.connect(..., device_path=...)` opens exactly the
  enumerated device; `SerialStreamTransport.port_name` (#161).
- `SdCardLogEntry.analog_channels` names the channel of each analog value;
  `SdCardParseOptions.analog_channels` gives a protobuf log's channels,
  which the log doesn't record (#194).
- PIC32 updates verify the flash: `VERIFYING` reads each programmed region's
  CRC back from the bootloader (`READ_CRC`) and compares it with the image's.
  `Pic32BootloaderProtocol.compute_crc_regions`, `create_read_crc_message`
  and `decode_read_crc_response`, and `FlashCrcRegion` (#198).
- A PIC32 update that fails (or is canceled) while erasing, programming or
  verifying re-erases the flash: new states `FirmwareUpdateState.CLEANING_UP`
  and `RECOVERED` (#198).
- `CsvExportOptions.relative_time_decimals` (0–7, default 3) sets the
  relative-time resolution (#195).
- `TimestampProcessor.set_timestamp_frequency(device_id, hz)` and
  `get_tick_period(device_id)`: a device's reported `timestamp_freq` sets
  its tick period instead of the fixed 20 ns (#204).
- `StreamManager`/`AsyncStreamManager`/`NyquistDevice.create_stream(...,
  integer_units=...)` and `Sample.units` for live JSON/CSV streams (#222).
- `TcpStreamTransport(..., local_interface=...)` and
  `DaqifiDeviceFactory.connect_tcp(..., local_interface=...)` bind the TCP
  connection to a local IP as its source address; `connect_from_device_info`
  passes the discovering NIC's IP. This doesn't select the outgoing NIC: on
  Linux the routing table still picks the route (#196).
- Typed errors (#201): `DeviceNotConnectedError` (a `RuntimeError`, as the
  not-connected guards raised before; `is_shutting_down` when a disconnect
  cut a send off) and `SerialPortConnectError` with `reason`
  (`SerialPortConnectFailure`: not found, in use, access denied, unknown;
  also a `serial.SerialException`). `SerialInterface.last_open_error` keeps
  why `open()` failed.
- SD card listing, download, delete and format work over WiFi on firmware
  v3.7.0 and later (the firmware routes the SD reply back over the TCP
  connection), keeping the LAN interface up; older or unknown firmware is
  refused with `FeatureNotSupportedError`. SD logging stays USB-only.
  `start_sd_card_logging` returns the log file name (#200).

### Changed
- `send()` is serialized against text exchanges: while another flow runs
  one (`execute_text_command`, `drain_errors`, `run_exclusive`, an SD
  listing or download) it WAITS, so the command lands after that exchange
  rather than in the middle of it, where the exchange could read its reply
  as its own. Returning means the bytes are on the wire. The wait is
  bounded: from an ordinary thread by `send_exchange_wait_s` (60 s by
  default -- a caller's-patience bound, so a send during a long SD download
  or format raises unless it is raised), and from the message-received
  callback thread it does not wait at all, raising `ExchangeTimeoutError` if
  the device is held. The thread that owns the exchange is exempt (its
  `setup_action` is what sends). A send that took the lock re-checks the
  connection after acquiring, so it no longer puts its command on a link
  the exchange ahead of it lost (which left the device `Lost` and
  `Streaming` at once) (#225, #238, #251).
- `StreamMessageConsumer.start()` waits up to
  `STALE_READER_GRACE_MS` (1 s) for a stopped-but-not-yet-exited reader
  instead of refusing at once. A reader asked to stop while it is inside a
  message-received callback cannot exit until that callback returns, and
  refusing in that window turned a momentary overlap into a permanently
  dead pipeline. A reader that outlasts the grace is still refused
  (#238; mirrors daqifi-core#383).
- A status event is never delivered while the exchange lock is held: a
  subscriber can hand recovery to another thread and wait for it, and that
  thread's `send()` or exchange needs the lock the subscriber's own caller
  cannot release until the subscriber returns. Events raised under the lock
  are queued and delivered by whichever frame releases it (#225).
- The hardware-free test run (`pytest tests --ignore=tests/integration/hardware`)
  no longer probes serial ports: the device smoke test moved under
  `tests/integration/hardware`, and a layout test keeps any test that needs a
  board out of the hardware-free path. The download-service tests now drive
  `GitHubFirmwareDownloadService` itself (#205).
- A PIC32 update now refuses to start while more than one DAQiFi board is
  connected over USB (application serial ports with 04D8:F794, plus boards
  already in bootloader mode), and says to disconnect the others; it also
  refuses when serial ports can't be listed. With one board it goes ahead. A board plugged in during the update is not caught.
  New options `application_vendor_id` / `application_product_id` (#161).
- `python_requires` raised to `>=3.9`, forced by the protobuf runtime floor
  of the generated bindings (#155).
- `requirements.txt` now lists the full runtime set (pyserial, protobuf,
  ifaddr), matching `install_requires` (#155).
- `StreamManager.read()`'s `timeout` is the total for all samples, not per
  sample; its default allows twice the samples' duration at the configured
  rate plus 1 s. `read_all(duration)` collects for `duration` seconds and
  returns just those samples, even on a stalled stream. The stream buffer
  keeps the newest `buffer_size` samples (default 100,000; `None` for
  unbounded). A channel absent from a `Sample` reads NaN, not 0.0 (#199).
- python-core opens serial ports exclusively (`exclusive=True`: an advisory
  `flock` on POSIX; Windows COM ports already were), in `SerialInterface`,
  `SerialStreamTransport` and serial discovery. A second python-core open of
  a port in use now fails instead of sharing it. Clients that don't take the
  lock (a terminal program, a raw file descriptor) can still share a port on
  POSIX (#196).
- `DeviceUnresponsiveError` is now also a `TimeoutError`, and
  `DaqifiDevice.initialize()` refuses a non-positive `response_timeout_s`
  with `ValueError` (#201).
- `ConnectionRetryOptions` rejects invalid values when set: fewer than one
  attempt, negative delays, a backoff multiplier below 1, a connection
  timeout under 1 ms (#201).

### Removed
- `CompositeMessageParser`, and the content heuristics it routed on
  (`_detect_message_type` and friends). On a length-prefixed protocol a
  printable byte is also a valid length or field tag, so no byte-level rule
  could tell text from the start of a binary frame: two audit rounds of
  attempts each either ate a partial frame or starved complete text. No
  production code used it — the device already picks its parser by the mode
  it is in — and every caller wanted the protobuf parser it now names
  directly (#204 item 2, #231).

### Fixed
- `update_wifi_module()` works with the default options. The default
  `wifi_flash_tool_arguments_template` never passed `/v <version>`, which
  `winc_flash_tool.cmd` needs to find its programmer image, so every update
  failed in `PROGRAMMING` with "Firmware release not found", after the device
  was already in WiFi-update mode. The template is now
  `/p {port} /d WINC1500 /v {version} /k /e /i aio /w`, and `{version}` is a
  new placeholder. `update_wifi_module()` takes a `firmware_version=`
  argument: pass the version `download_wifi_firmware()` returns with the
  path, which is now the bundle's version folder (`19.7.7` under
  `files/winc1500/`) rather than the release tag; a downloaded bundle
  without exactly one version raises `RuntimeError`. When the template uses
  `{version}`, the update raises `ValueError` before anything is sent to the
  device unless the template has exactly one `/v` (any of `/v`, `-v`, `/V`,
  `-V`) whose value is exactly `{version}`, exactly one `/d` with a literal
  device name, no argument other than an option or the value right after
  one, and a value (or nothing) right before `/v` and before `/d`; unless
  every template argument is made only of letters, digits and
  `/ . _ : { } -`, the `/d` value only of letters, digits and `. _ -`, and
  the version, given or read from the bundle, only of letters, digits and
  `. _ -`; unless the port substituted for `{port}` (from
  `wifi_port_override` or the serial port name) is made only of letters,
  digits and `. _ : / -`; and unless `files/winc1500/<version>/firmware/programmer_firmware.bin`
  (`winc3400` for a `/d` ending in 3400) exists beside the tool. Without
  `firmware_version=` the version is read from that one device folder, which
  must then hold exactly one such image. A flash tool that cannot be found
  is still reported where it was before, when the flash request is built. A
  template without `{version}` behaves as before (#270).
- `update_firmware()` called from a message-received callback is refused at
  its entry, before anything reaches the device. It used to stop streaming
  first and only then refuse, so the caller got an exception AND a device
  whose stream had been stopped (#254, #255).
- `update_wifi_module()` likewise refuses a callback-thread call at its
  entry. There the earlier refusal was also swallowed: the version check
  catches the consumer-thread refusal of `get_lan_chip_info()` and answers
  "not up to date", so the update stopped streaming (two
  `SYSTem:STReam:STOP`s) before failing (#256).
- The protobuf schema decodes fields 70-72 as the firmware's streaming
  timebase (`stream_timer_freq`, `timestamp_ticks_per_sample`,
  `actual_rate_millihz`). They were declared as client-side `cap_*` fields,
  so a real SYSInfoPB frame reported its timer frequency as `cap_version`;
  the `cap_*` fields move to 80-85 (#157).
- With echo on, the device's protobuf reply no longer stalls behind the
  echoed command line. `ProtobufMessageParser` read the echo's first byte
  as a length prefix and waited for bytes that never came, so a
  `SYSTem:SYSInfoPB?` reply was never dispatched at all. The parser is now
  told what the library actually sent and skips an exact echo of it — plus
  a `DAQIFI> ` prompt, and a `**ERROR: ` line which goes to a sink rather
  than being dropped — but only at a frame boundary and only as a complete
  line, so a partial frame whose payload contains printable runs or CRLF is
  still held (#204 item 1, #231).
- A WiFi module update canceled while the flash tool was running no longer
  reports `FAILED` when the flash had already succeeded. The tool's success
  line is the authority on whether the module was programmed; what follows it
  leaves the USB-transparent bridge and restores the LAN configuration, so
  honouring a cancel there both misreported a completed update and left the
  device bridged with its LAN down. A cancel observed after that line is logged
  and the update completes; a cancel before it still recovers and reports
  `FAILED` (#190).
- A canceled firmware update chains `FirmwareUpdateCancelledError` as the
  `FirmwareUpdateException`'s `__cause__`. Telling "the user canceled" apart
  from "the update failed" previously meant naming a private sentinel (#190).
- A text exchange no longer leaves the protobuf consumer dead when a
  message-received callback delegates work to another thread and waits for
  it. The delegated `send()` blocked on the exchange lock, which stalled the
  callback, which made the exchange's consumer-stop time out; the capture
  went ahead anyway and the restart's failure was swallowed, so the consumer
  exited for good and a later `start_streaming()` reported success with
  nothing reading. The grace period lets the restart succeed, and a restart
  that still fails is recorded on the device (`delivery_fault`) instead of
  discarded (#238, #251).
- A text exchange started from a message-received callback is refused
  outright instead of killing the protobuf consumer. It ran on the
  consumer's own thread, so pausing the consumer stopped the thread it ran
  on, the restart raised and was swallowed, and the call returned a
  plausible result while nothing read the stream again — a later
  `start_streaming()` then reported streaming with no reader. The guard
  refused only when another exchange held the lock, so a callback that
  found it free — the common case — went through. `drain_errors`,
  `get_sd_card_files`, `request_sd_card_file_list`, `delete_sd_card_file`
  and `format_sd_card` now raise a `RuntimeError` naming the thread, and
  the consumer keeps reading (#238).
- An SD operation or text exchange started from a download's progress
  callback read the download's own remaining bytes and its end-of-file
  marker off the stream: the nested call reported success with the outer
  file's data while the download itself timed out short. A capture —
  whether it reads text or raw bytes — now marks itself for its whole
  duration, so a second one underneath it is refused (#225).
- An SD download the device ended with `__TRANSFER_ERROR__` (a card read
  error part-way through the file) waited out its whole timeout (30 s, or
  30 min for `SdCardFileReceiver`) and could leave the marker in the file.
  `SdCardFileReceiver` and `download_sd_card_file` now raise
  `SdCardTransferErrorException` (`file_name`, `bytes_received`), keeping
  the real content received before it. With a known size, marker text in
  the file is still content; a real mid-file error there surfaces at the
  idle timeout (#154).
- Live streaming timestamps jumped back to near zero every time the
  device's 32-bit tick counter rolled over (about every 86 s at 50 MHz,
  102 s at 42 MHz), in all three stream formats, so any session longer than
  that had wrong times. `StreamManager` now unwraps ticks per session: each
  is placed by its signed distance from the previous one, so time keeps
  rising across a rollover, a slightly older sample steps back a little,
  and the first sample of a session keeps its raw time. Limit: a gap in the
  device's own samples longer than half a period (42.9 s at 50 MHz) reads as
  a step back; a running stream doesn't produce one (#193).
- An unplugged serial (USB) device was never reported lost: pyserial's
  port stays open after an unplug, so the device stayed `Connected` while
  its reader logged a read error ten times a second forever. The reader
  now tells the serial transport how each read went, and five failed reads
  in a row with no success between are taken as the device gone: the
  transport reports itself disconnected, the device moves to
  `ConnectionStatus.Lost` and its reader stops, as for TCP. A single
  failed read that recovers changes nothing, and `disconnect()` is never
  reported as a loss (#191).
- SD listings on current firmware returned a file named `__END_OF_LIST__`:
  the firmware ends every listing with `__END_OF_LIST__ OK`, `INCOMPLETE`
  or `FAILED`, and a listing marked INCOMPLETE or FAILED came back as if
  it were the whole card. A listing that timed out came back as an empty
  card. The marker is no longer a file. The listing now empties the error
  queue, then sends `SYSTem:ERRor?` after `LIST?` to mark the end of the
  reply, and raises `SdCardListIncompleteException` when that answer never
  comes, when it is an error (the firmware's answer to an aborted listing),
  when the marker says INCOMPLETE or FAILED, or when the marker is missing
  on firmware 3.8.0 and later (which always sends one). On older firmware
  an empty listing is an empty directory only with a clean terminator. An
  error queue that can't be emptied first raises rather than being blamed
  on the listing. It isn't retried automatically. A missing directory
  (`[Error:4]` / `[Error:5] Failed to open directory`) raises
  `SdCardDirectoryNotFoundException`, a `SdCardFilesystemException`
  subclass. Echoes of the listing's own commands are dropped. The legacy
  `NyquistDevice.sd_list_files()` skips the marker and refuses a listing
  marked incomplete too (#188).
- The WiFi module update didn't follow the firmware's sequence.
  `LAN:FWUpdate` only requests the update; the following `LAN:APPLY` enters
  the USB-transparent bridge, and `LAN:APPLY` from the bridge leaves it. The
  update never sent that APPLY, and after a successful flash it restored LAN
  with the bridge still on, so the commands went raw to the WiFi chip and
  the console stayed dead until a power cycle. The update now sends
  `POWer:STATe 1`, `FWUpdate`, `APPLY`, waits until the bridge is up
  (on a board in WiFi retry backoff that takes up to 30 s; the update is
  refused up front on a board left in standby, or connected over TCP
  rather than USB serial, and stops any stream first), runs the tool,
  then sends `SYSTem:USB:SetTransparentMode 0` before the LAN restore. After a failed
  or cancelled update it sends the bridge exit and `APPLY` until the error
  queue shows the APPLY was accepted, then watches the console for longer
  than the firmware's longest WiFi restart delay, exiting again if the
  bridge comes up late. It saves nothing, and the failure says when the
  device could not be confirmed back to normal. Success now needs the flash
  tool's own success line, not just exit code 0, and the update waits for
  the OS to release the serial port before starting the tool. New options:
  `power_on_wifi_module_before_lan_update_mode`,
  `power_on_wifi_module_settle_delay`,
  `post_lan_disconnect_port_release_delay`,
  `post_usb_transparent_mode_exit_delay`, `wifi_recovery_timeout` (at least
  one 40 s bridge watch), `wifi_flash_success_marker` (#190).
- Every NQ3 DAC write was rejected by the firmware. The voltage setter sent
  `SOUR:VOLT:LEV <ch> <volts>`, which the firmware answers with -101
  Invalid character; it now sends `<ch>,<volts>`. The DAC enable commands
  (`ENAble:SOURce:DC`) no longer exist in the firmware, so
  `AnalogOutputChannel.enabled` and `AnalogOutputs.enable_all()` /
  `disable_all()` now raise `FeatureNotSupportedError` instead of a
  `CommandError` from the device; an output drives the voltage last set
  (#189).
- SD CSV logs written at voltage precision 0 (integer millivolts) were read
  as raw ADC codes, and `SdCardParseOptions.integer_units` applied to JSON
  only. CSV now follows the same option through the shared firmware value
  rule: `"millivolts"` reads integers as mV, unscaled; unset reads them as
  raw codes with one warning per parse; each sample's `analog_units` says
  which (#163).
- SD JSON logs written by the firmware parsed to no samples: the parser
  expected one object per line with an `analog` key, but the firmware
  writes a `{"meta":...}` header and then each sample object across several
  lines with `"ai":[{"ch":n,"val":v},...]` and `"di":[...]`. The file is now
  read as a sequence of JSON objects; the header supplies the tick rate
  and device name, analog values are calibrated by their `ch` number, and
  the old one-line `analog` form still parses (#163).
- `StreamManager.start()` read and discarded everything the board sent with
  its START reply: the CSV/JSON metadata header (so the tick rate and the
  column-to-channel mapping were lost for the session) and the first
  protobuf frames. Those bytes now seed the stream's own buffers; only a
  leading echo and prompt are dropped, and a START rejection is still
  detected (#162).
- Protobuf samples mapped values onto the caller's channel list instead of
  the channels enabled on the device, so an extra enabled channel's data
  was relabelled and the apparent rate doubled. Values now follow the
  device's enabled channels (read at start), and the caller's list selects
  which ones a sample keeps (#162).
- `analog_in_data_ts` is treated as an offset to `msg_time_stamp`, as
  `DaqifiOutMessage.proto` defines it, not as an absolute time (#162).
- SD CSV logs in the firmware's default voltage mode (volts with decimals)
  were truncated to whole volts: every value was converted to an integer as
  if it were a raw ADC code. A value written with decimals is now kept as
  volts, unscaled; integer values are still scaled as codes. A non-finite
  value skips its row instead of aborting the log (#163).
- SD downloads ended at the first `__END_OF_FILE__` token, so a file
  containing that text was cut short and reported as a success. With the
  size from the SD listing, exactly that many bytes are read and the marker
  must follow; a short or long transfer raises. Without a size the marker
  counts only at the end of the transfer, and the result's `size_verified`
  is False (#163).
- `get_sd_card_files()` ended an active SD logging session (it stops the
  stream and hands the bus back to LAN); it now refuses while logging, as
  delete and download do (#163).
- `request_sd_card_file_list()` and `get_sd_logging_state()` sent
  `SD:ENAble 1` during an SD recording, which the firmware handles by
  tearing the write session down. The listing now refuses while logging.
  `get_sd_logging_state()` asked `SD:LOGging?`, which no firmware
  implements, after that same `SD:ENAble 1`. The firmware has no query
  for the logging state, so it now returns this session's state, the same
  as `is_logging_to_sd_card`, without sending anything. A recording started
  by another client, another session or an earlier process reads `False`
  (#187).
- After a JSON or CSV SD logging session the device's stream format, one
  global setting shared with the live stream, stayed non-protobuf, so live
  streaming decoded nothing; `stop_sd_card_logging()` restores protobuf
  (#163).
- SD downloads lost leading CR/LF bytes of the file: they were stripped
  whether or not a prompt followed. Only CR/LF before a prompt are dropped
  now (#163).
- SD downloads ignored the destination's `write()` return value, so a raw
  stream that took only part of a chunk lost the rest; writes are now
  completed (#163).
- The text-command SD listing kept a `DAQIFI> ` prompt sharing a line with
  the first file name, so that file came out as `DAQIFI>` (#163).
- SD logs: consecutive frames with the same timestamp were merged even when
  each carried its own analog data, keeping only the first frame's values;
  only complementary frames (the firmware's separate analog and digital
  messages for one period) are merged now (#163).
- SD CSV logs: calibration was looked up by column position, but the
  firmware writes only enabled channels (`ain7_ts,ain7_val`), so a sparse
  log was scaled with the wrong channel's coefficients; it now follows the
  channel number in the column name (#163).
- `StreamManager.stop()` returned without sending STOP once a callback
  exception had moved the state to ERROR, leaving the board streaming; it
  forgot a worker still running after its 2 s join, so a restart ran two
  readers on one port; and called from inside the callback it tried to
  join its own thread and never sent STOP. stop() now sends STOP whenever
  the device was started, doesn't join from the worker itself, and
  start() refuses while a previous worker is still running (#162).
- SD-card operations on one device could interleave across threads: a raw
  capture (list, delete, download) didn't take the text-exchange lock, so a
  listing could switch the SPI bus under a download and read its reply off
  the same stream, which left the download hung and its file truncated.
  Every SD operation that switches the bus (list, delete, download, format,
  start/stop logging, the logging-state query) now runs inside that lock,
  including the restore of the LAN interface, as upstream does since
  daqifi-core #406/#417 (#164).
- The HID error messages told users to `pip install hid`, which is a
  different library with an incompatible API (`hid.Device`); the code
  needs the PyPI `hidapi` package (`hid.device()`). With the wrong package
  installed, bootloader enumeration silently found nothing and opening a
  device raised `AttributeError`. Both now raise an `ImportError` naming
  `hidapi`, and a `daqifi[hid]` extra installs it.
- Package homepage URL pointed at a repository that does not exist.
- PIC32 firmware update could erase a different board: a bootloader that was
  already enumerated before `SYSTem:FORceBoot` is no longer accepted; on Linux
  the bootloader must also sit on the board's own USB port; and the selected
  bootloader is opened by device path rather than by VID/PID (#161). See
  README "Several boards on one host" for the residual risk on other
  platforms.
- PIC32 firmware update erased flash for an image with no data records
  (EOF-only, or all data in the protected range); such images are now
  refused before any device I/O (#161).
- Intel HEX records whose byte count disagrees with their length, and data
  records that run into the protected range, are refused instead of
  accepted (#161).
- HEX data records whose escaped program frame exceeds one 64-byte HID
  report are split into 16-byte records instead of failing after erase (#161).
- Bootloader replies are now checked for framing, CRC, EOT and the command
  they answer; truncated or mismatched replies are no longer accepted (#161).
- WiFi module flashing passed the device's display name (`DaqifiDevice` by
  default) to the flash tool as its serial port; it now uses the serial
  transport's port, and fails before entering WiFi-update mode when there
  is none and no `wifi_port_override` (#161).
- WiFi flashing with a relative firmware or tool path looked the tool up
  relative to its own directory; paths are anchored to the caller's
  working directory first (#161).
- WiFi flashing through `winc_flash_tool.cmd` never ran on Windows: the
  `cmd.exe` arguments went through `list2cmdline`, whose `\"` escapes
  cmd.exe does not honour, so the script was "not recognized". Native
  Windows now passes one verbatim `/d /s /c "..."` line; under WSL the
  arguments go separately (WSL quotes them) and Linux paths are translated
  with `wslpath -w` (#161).
- The `cmd.exe` line now runs with `/v:off`, so `!NAME!` in a path is not
  expanded where delayed expansion is on in the registry; under WSL the
  arguments template's own literals are checked for cmd.exe
  metacharacters like the substituted values, and on native Windows a `%`
  in the template is refused (#161).
- The WiFi flash arguments template is split into arguments before the
  port and firmware path are put in, each as one whole argument, so a
  value is never parsed again: an apostrophe inside a quoted
  `"{firmwarePath}"` no longer fails after the device entered update
  mode, a placeholder the template already quoted is quoted once on the
  cmd.exe line (nested quotes left `&` in the path bare), and a native
  tool's arguments go as a list (C-runtime quoting, so a path ending in a
  backslash no longer swallows the next argument). An unbalanced template
  is refused before update mode (#161).
- The arguments template is split by the rules of the program that will
  read it: cmd.exe rules for a `.cmd`/`.bat` script, C-runtime rules for a
  Windows program (natively, or an `.exe`/`.com` started from WSL, whose
  firmware path is translated with `wslpath -w`), and shlex for a POSIX
  program. A native tool's `-c "print(\"done\")"` and a WSL `.exe`'s
  `C:\fw\image.bin` were mangled before (#161).
- A `.cmd` template's `^` escapes are consumed as cmd.exe does (`A^&B` is
  `A&B`; inside quotes `^` stays literal); they were kept and then quoted,
  so the script got `^&` (#161).
- A Ctrl-C while `SubprocessRunner` was joining its output readers skipped
  the rest of cleanup, so a descendant still holding stdout kept firing
  the caller's callbacks after `run()` had raised. The cleanup now always
  runs, and the interrupt is re-raised afterwards (#161).
- README: a bootloader that has sat idle stops answering (Windows selective
  suspend); the updater then stops before ERASE (#161).
- README: the idle bootloader is suspended about 5–6 s after it enumerates
  (11 bench trials), not about 37 s as first stated; see
  daqifi-nyquist-firmware #1172 (#174).
- `SubprocessRunner` killed only the direct child on timeout, so a
  descendant holding the pipes kept `run()` blocked; the whole process
  tree is killed now (a process group on POSIX, a Job Object on Windows,
  which also reaches workers whose launcher has already exited), and a
  surviving descendant can no longer hang cleanup. A run is complete only
  when its output pipes close, so a descendant's late failure output is
  captured (#161).
- On Windows, a string argument list reached the child with literal quote
  characters; it is now passed through as the command line verbatim (#161).
- `FirmwareVersion` aliases (`3.0.0b1`, `3.0.0beta1`, `3.0.0-beta.1`)
  compared as both greater than each other, so the update check offered
  the installed firmware as an update (#161).
- Over TCP, every `execute_text_command` left the protobuf consumer
  stopped (its reader was blocked in a socket read and could not be
  restarted), so no stream frame was dispatched again until reconnect.
  TCP reads are now bounded to 0.5 s, as upstream does (#164).
- A device closing its TCP connection (reboot, Wi-Fi drop) went
  unnoticed: the session stayed "connected" and the reader spun on empty
  reads. End of stream now marks the transport disconnected, raises a
  status event, and the device reports `ConnectionStatus.Lost` (#164).
- `connect()` and `disconnect()` on one device no longer run alongside each
  other (port of upstream `LifecycleGate`): a `connect()` that finds one in
  progress waits `lifecycle_lock_timeout` (10 s) and then raises
  `TimeoutError` without opening anything; a `disconnect()` waits
  `teardown_lock_timeout` (30 s) and then only marks the device
  Disconnected. A transport loss is reported as `Lost` without tearing the
  consumer down from the transport's thread (#164).
- `StreamManager` fell behind a board streaming at its configured rate:
  the callback thread slept a whole sample period after every sample, and
  `read()` slept 1 ms after every sample, so the backlog of undelivered
  samples grew for the whole session (about 70 a second at 1 kHz) until
  buffers overflowed. Readers now wait only when a poll finds nothing
  (#192).
- README examples that failed as written now run: streaming reads
  `sample.channels` and imports `time`; the SD example uses
  `get_sd_card_files()` and `file_name`/`size`; the CSV export's source
  accepts `cancel_event`; the firmware readiness probe uses
  `drain_errors()`; the Quick Start, `examples/basic_adc.py` and the
  package docstring set the DAC and input range only on NQ3.
  `examples/streaming_callback.py` measures its duration in device time.
  The README no longer points at a missing `testing/suites/`, names
  classes that don't exist, lists `pyyaml` as a runtime dependency or
  sends support requests to the firmware repo. `tests/test_readme_examples.py`
  runs the examples against fakes (#197).
- Docstrings that had gone stale: text replies on `DaqifiStreamingDevice`,
  per-channel DIO commands, the SD CSV/JSON parsers, the TCP socket view's
  end of stream and HID `disconnect()` waiting for a read (#197).
- SD CSV rows with an empty `,,` pair (a channel with no conversion that
  period, or every analog pair of a DIO-only row) were dropped whole; the
  missing channel is now left out of that row. A stray `ch` line before the
  column header no longer hides it. Protobuf logs' raw ADC codes are marked
  `analog_units="raw"` and warn when nothing supplies the scaling, float
  values are marked `"volts"`, and calibration follows the channels given
  in `analog_channels` rather than the packed position. The calibration
  offset is added after the internal scale factor, as the firmware does
  (#194).
- A PIC32 HEX image with data outside the lower program-flash panel (the
  upper panel, boot flash), which the bootloader neither erases nor programs
  since firmware #909, is refused before any device I/O instead of being
  flashed partly or over unerased flash (#198).
- `DaqifiStreamingDevice` reported every DIO pin as an input and every
  analog channel as disabled: it now reads the status message's direction
  mask (field 37, bit set = input) and enable mask (field 22). A pin or
  channel the device doesn't report keeps the old default (#202).
- Two legacy `DigitalIOChannel` direction or state setters run at once on
  one device could lose one update (an unlocked read-modify-write of the
  port mask); they now share a per-device lock (#202).
- `NyquistDevice.create_stream` raised `NameError` instead of
  `ConfigurationError` for an invalid channel (and for every call on a
  board with no analog inputs, which now says so); `sd_get_file_size`
  raised `AttributeError` before `sd_list_files` had run and now returns
  0 (#203).
- After a WiFi module update, restoring LAN sent `LAN:APPLY` then
  `LAN:SAVE`; when the firmware's apply gate refused the apply (a REINIT
  in flight), the separate SAVE still persisted the unapplied settings. It
  now sends `LAN:APPLY 1`, which saves only an accepted apply (#220).
- CSV export progress fired only when the running count landed exactly on
  a multiple of 5000 (with 7 channels, twice in 70,000 samples); it now
  fires as each multiple is passed. Formula neutralisation skips any
  Unicode whitespace before `=`, `+`, `-` or `@`, and fields with
  whitespace at their edges are quoted (#195).
- Live JSON streaming: a digital-only record no longer becomes a fake 0 V
  sample; one malformed record no longer discards the good ones buffered
  behind it; records are logged at debug level instead of printed to stdout
  (#199).
- Items carried forward from #163 (#206): `start_streaming` accepts up to
  the firmware's 22000 Hz ceiling instead of 1000 (the device refuses a
  rate its channel configuration can't sustain); `TimestampProcessor` no
  longer accumulates whole-microsecond rounding step by step; an unusable
  CSV `# Timestamp Tick Rate:` header reads as absent instead of aborting
  the parse; the CSV row parser lets `MemoryError` through.
- Live JSON and CSV streaming read the firmware's values as millivolts. At
  the firmware's default voltage precision they are volts with decimals, so
  JSON reported 2.5 V as 0.0025 V and CSV dropped the channel. Both now use
  the SD-log parsers' rule: decimals are volts, integers follow
  `integer_units` (unset: raw codes, with a warning). A stream at voltage
  precision 0 now needs `integer_units="millivolts"` (#222).
- Serial discovery (`DeviceFinder`, `SerialDeviceFinder`) probes only DAQiFi
  application ports (USB 04D8:F794, plus CDC-ACM ports whose IDs can't be
  read) instead of every port, so it no longer writes SCPI (including a
  power-on) to unrelated devices; on macOS a board is listed once. Each
  probe has a hard timeout: one hung port no longer blocks discovery, and
  isn't probed again while its abandoned probe runs (#196).
- `WiFiDeviceFinder` fills `DeviceInfo.local_interface_address` with the NIC
  a device answered on (#196).
- `update_network_configuration` ends with `LAN:APPLY 1`, which applies the
  configuration and, only if the apply is accepted, saves it, in one
  firmware handler. The save it sent after the apply never arrived over a
  WiFi/TCP control link (the apply restarts the WiFi module), and the old
  configuration came back at the next reboot (#196).
- `execute_text_command` caps every exchange at five times
  `response_timeout_ms` when `max_total_ms` isn't given, reads LF-only
  replies, and treats a blank-only reply as an answer instead of waiting
  out the timeout (#201).
- `send()` cut off by a `disconnect()` on another thread raised a raw
  `ValueError` and overwrote Disconnected with Lost/Error; it now raises
  `DeviceNotConnectedError(is_shutting_down=True)` and the status stays
  Disconnected (#201).
- An SD operation (list, delete, format, download) that stopped a live
  stream left the device state saying Streaming. It now leaves the device
  Ready and logs that it stopped the stream; it doesn't resume it, so call
  `start_streaming()` again when you're ready (#200).
- A transport error after `DELete` was sent now raises
  `SdCardDeleteOutcomeUnknownException` with the cause chained, since the
  file may be gone or not; one before it was sent stays a definite failure
  (#200).
- A delete the device refused returned success with the file still there,
  because the first reply's code was thrown away. It now raises
  `SdCardOperationException`. A -200 from a card fault partway through a
  delete also reads as refused, though the file is gone; list the card to
  check (#200, #242).
- `enable_adc_channels` (and `start_sd_card_logging(channel_mask=...)`) take
  a decimal mask only: a hex mask like `"FF"` was sent, the firmware refused
  it, and the recording ran with the previous channel set (#200).
- `run_sd_benchmark` takes its size in KB, 1 to 1024, as the firmware does
  (#200).

### Known issues
- **An SD download can lose the head of a file whose content begins with
  the device's own console framing.** If a file's first bytes are a
  `DAQIFI> ` prompt, the echoed `SYSTem:STORage:SD:GET` line for that same
  file, a line ending and another `DAQIFI> ` -- byte for byte what an idle
  console sends ahead of a real download -- `sd_get_file` and
  `sd_get_file_binary` remove them as framing. The loss is the length of
  that framing, so it grows with the filename: 49 bytes for `log.csv`, 214
  for a 176-character name. A file that merely begins with `DAQIFI> ` loses
  those 8 bytes. Before 0.2.0 the text download already lost these bytes
  and the binary download kept most or all of them. Ordinary log files are
  not affected. The shortened file is returned without an error. A
  device-side fix is in progress (daqifi/daqifi-nyquist-firmware#1265);
  python-core will use it under #260.
- **After an SD download times out, the next download on the same device
  object can return the previous file's bytes.** A timeout leaves the rest
  of that file still arriving, and the next `sd_get_file` or
  `sd_get_file_binary` reads it as part of its own file, with no error.
  There is no verified workaround yet, so do not trust a download made on
  the same device object after one has timed out. Tracked in #268.

## [0.1.0]

Initial development version: a port of the C# daqifi-core library covering
transport (USB CDC, TCP, USB HID), device and channel control, protobuf
streaming, Wi-Fi discovery, SD card operations, CSV export and the firmware
update service. Never tagged; 0.2.0 is the first tagged release.
