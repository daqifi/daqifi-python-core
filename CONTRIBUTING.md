# Contributing to daqifi-python-core

Thanks for helping. This library is the Python port of the C#
[daqifi-core](https://github.com/daqifi/daqifi-core) and talks to DAQiFi
Nyquist hardware over USB CDC, Wi-Fi TCP and USB HID.

## Development setup

```bash
git clone https://github.com/daqifi/daqifi-python-core.git
cd daqifi-python-core
pip install -e .
pip install pytest pytest-asyncio
```

Python 3.9 or newer is required. The floor is forced by the protobuf runtime
the generated bindings need; see the comments in `setup.py`.

## Running the tests

The hardware-free suite needs no device:

```bash
python -m pytest tests --ignore=tests/integration/hardware
```

Run it before every pull request. Hardware tests are covered below.

## Pull requests

- Branch from `main` and keep each PR to one concern.
- Use [Conventional Commits](https://www.conventionalcommits.org/) titles,
  for example `fix(streaming): ...` or `feat(sdcard): ...`.
- Add a regression test with every fix, and show that it fails without
  the fix.
- Add a line under **Unreleased** in `CHANGELOG.md` for any user-visible change.
- Never commit workstation-specific values: device serial numbers, COM
  ports, LAN IPs, or Wi-Fi credentials. Read them from the environment.

### Protobuf schema

`daqifi/_internal/proto/DaqifiOutMessage.proto` mirrors the schema in
[daqifi-nyquist-firmware](https://github.com/daqifi/daqifi-nyquist-firmware)
(`firmware/src/services/DaqifiPB/DaqifiOutMessage.proto`). The firmware is
the producer, so its field numbers win. After editing the `.proto`:

```bash
pip install grpcio-tools
python scripts/regenerate_protobuf.py
```

Commit the regenerated `DaqifiOutMessage_pb2.py`. A drift test fails if the
two disagree.

## Hardware testing

### Identify your device

Several units may be attached to one workstation. Before any write, confirm
the port you are using is **your** unit by its protobuf serial number:

```python
from daqifi.device import NyquistDevice
dev = NyquistDevice.connect("/dev/ttyACM0")  # or COMx
print(dev.metadata.serial_number, dev.model, dev.firmware_version)
dev.disconnect()
```

Use `dev.metadata.serial_number`, not `dev.serial_number`. Some firmware
reports a literal `0` in the `*IDN?` serial field.

### Local rig file

Keep your own identifiers (device SN, port, LAN IP) in a gitignored file
such as an untracked local note or your shell environment, never in a
tracked file.

### Identity-gating destructive operations

Before code issues `*RST`, changes Wi-Fi credentials, wipes SD files, or
flashes firmware, gate it on the expected serial number:

```python
from daqifi._internal.serial_identity import serial_number_matches

expected = os.environ["DAQIFI_EXPECTED_SERIAL_NUMBER"]
if not serial_number_matches(expected, dev.metadata.serial_number):
    raise RuntimeError("Refusing destructive op on an unexpected device")
```

The same serial comes in two spellings. `dev.metadata.serial_number` is
**decimal**, while `*IDN?` and `SYST:SERialNUMber?` report it in **hex**. A
plain `!=` against a hex value refuses the correct board, so the suite's gates
use `serial_number_matches`, which accepts either form. Their refusal messages
show the connected board's serial in both forms.

Each value names exactly one board. A `0x` prefix or any letter a-f means hex,
and all digits means decimal. A hex serial that happens to be all digits needs
the `0x` prefix; without it, the gate reads it as decimal and refuses.

### Running the hardware suite

```bash
DAQIFI_DEVICE_PORT=/dev/ttyACM<N> DAQIFI_DEVICE_REQUIRED=1 \
    DAQIFI_EXPECTED_SERIAL_NUMBER=<YOUR_SN> \
    python -m pytest tests/integration/hardware/ -v
```

`DAQIFI_EXPECTED_SERIAL_NUMBER` may be decimal or hex (all-digit hex needs `0x`).

Wi-Fi round-trip tests read `DAQIFI_TEST_WIFI_SSID` and
`DAQIFI_TEST_WIFI_PASSWORD` from the environment; a gitignored `.env` works.
The Wi-Fi discovery test reads the expected serial from the board on
`DAQIFI_DEVICE_PORT`; set `DAQIFI_TEST_WIFI_SN` only to override it.

### Several boards on one bench

**Always set `DAQIFI_DEVICE_PORT`.** Without it, the suite falls back to
`DeviceFinder.discover_first()`, which binds the first DAQiFi it finds, and
that may be another unit. The serial gates protect destructive tests, but
read-only tests would quietly run against the wrong board.

**Check that the port reached the interpreter.** An environment prefix in a
WSL shell (`DAQIFI_DEVICE_PORT=COMx python.exe -m pytest ...`) does **not**
reach a Windows `python.exe` unless the variable is listed in `WSLENV`. The
forced port is then silently unset, and discovery picks a board for you.
Verify it from the same interpreter that runs the suite:

```bash
python.exe -c "import os; print(os.environ.get('DAQIFI_DEVICE_PORT'))"
```

If that prints `None`, append the variables to `WSLENV` rather than
replacing it, since other tools may already use it:

```bash
export WSLENV="$WSLENV:DAQIFI_DEVICE_PORT:DAQIFI_EXPECTED_SERIAL_NUMBER"
```

Or set the variables in a Windows shell instead.

**WSL2:** attach the device with `usbipd attach --wsl --busid <X-Y>`. After a
`*RST` or reboot it does not re-attach on its own; set
`DAQIFI_USBIPD_BUSID=<X-Y>` and the suite's `wait_for_device_back` fixture
re-attaches it. UDP discovery does not work from WSL2; set
`DAQIFI_DEVICE_LAN_IP=<ip>` to probe by TCP instead.

## Reporting bugs and security issues

Open an issue for bugs. For security issues, follow [SECURITY.md](SECURITY.md)
instead of opening a public issue.

By contributing you agree your work is released under the [MIT License](LICENSE).
