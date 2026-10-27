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
