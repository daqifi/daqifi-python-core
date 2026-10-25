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
such as `.claude/hardware-rig.md` or in your shell environment, never in a
tracked file.

### Identity-gating destructive operations

Before code issues `*RST`, changes Wi-Fi credentials, wipes SD files, or
flashes firmware, gate it on the expected serial number:

```python
expected = os.environ["DAQIFI_EXPECTED_SERIAL_NUMBER"]
if dev.metadata.serial_number != expected:
    raise RuntimeError("Refusing destructive op on an unexpected device")
```

### Running the hardware suite

```bash
DAQIFI_DEVICE_PORT=/dev/ttyACM<N> DAQIFI_DEVICE_REQUIRED=1 \
    DAQIFI_EXPECTED_SERIAL_NUMBER=<YOUR_SN> \
    python -m pytest tests/integration/hardware/ -v
```

Wi-Fi round-trip tests read `DAQIFI_TEST_WIFI_SSID` and
`DAQIFI_TEST_WIFI_PASSWORD` from the environment; a gitignored `.env` works.

**WSL2:** attach the device with `usbipd attach --wsl --busid <X-Y>`. After a
`*RST` or reboot it does not re-attach on its own; set
`DAQIFI_USBIPD_BUSID=<X-Y>` and the suite's `wait_for_device_back` fixture
re-attaches it. UDP discovery does not work from WSL2; set
`DAQIFI_DEVICE_LAN_IP=<ip>` to probe by TCP instead.

## Reporting bugs and security issues

Open an issue for bugs. For security issues, follow [SECURITY.md](SECURITY.md)
instead of opening a public issue.

By contributing you agree your work is released under the [MIT License](LICENSE).
