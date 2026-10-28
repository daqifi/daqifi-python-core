# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project
aims to follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `CONTRIBUTING.md`, `SECURITY.md` and this changelog.
- `pyproject.toml` declaring the PEP 517 build backend and the pytest
  configuration (#155).
- `HidLibraryTransport.connect(..., device_path=...)` opens exactly the
  enumerated device; `SerialStreamTransport.port_name` (#161).

### Changed
- A PIC32 update now refuses to start while more than one DAQiFi board is
  connected over USB (application serial ports with 04D8:F794, plus boards
  already in bootloader mode), and says to disconnect the others; it also
  refuses when serial ports can't be listed. With one board it goes ahead. A board plugged in during the update is not caught.
  New options `application_vendor_id` / `application_product_id` (#161).
- `python_requires` raised to `>=3.9`, forced by the protobuf runtime floor
  of the generated bindings (#155).
- `requirements.txt` now lists the full runtime set (pyserial, protobuf,
  ifaddr), matching `install_requires` (#155).

### Fixed
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
- `get_sd_card_files()` ended an active SD logging session (it stops the
  stream and hands the bus back to LAN); it now refuses while logging, as
  delete and download do. `request_sd_card_file_list()` keeps a session
  running (#163).
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

## [0.1.0]

Initial development version: a port of the C# daqifi-core library covering
transport (USB CDC, TCP, USB HID), device and channel control, protobuf
streaming, Wi-Fi discovery, SD card operations, CSV export and the firmware
update service. No release has been tagged yet.
