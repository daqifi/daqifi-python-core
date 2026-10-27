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

## [0.1.0]

Initial development version: a port of the C# daqifi-core library covering
transport (USB CDC, TCP, USB HID), device and channel control, protobuf
streaming, Wi-Fi discovery, SD card operations, CSV export and the firmware
update service. No release has been tagged yet.
