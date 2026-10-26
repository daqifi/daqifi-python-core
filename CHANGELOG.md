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
- `python_requires` raised to `>=3.9`, forced by the protobuf runtime floor
  of the generated bindings (#155).
- `requirements.txt` now lists the full runtime set (pyserial, protobuf,
  ifaddr), matching `install_requires` (#155).

### Fixed
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
  relative to its own directory; paths are made absolute first (#161).
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

## [0.1.0]

Initial development version: a port of the C# daqifi-core library covering
transport (USB CDC, TCP, USB HID), device and channel control, protobuf
streaming, Wi-Fi discovery, SD card operations, CSV export and the firmware
update service. No release has been tagged yet.
