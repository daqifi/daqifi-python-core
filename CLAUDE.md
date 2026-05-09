# Hardware test rig setup

Documents how an agent (or developer) running this repo against a
real DAQiFi NQ1 should identify which connected device is theirs.
Real serial numbers, IPs, COM ports etc. are workstation-specific
and DO NOT belong in this tracked file — record them in a local
gitignored file instead (see "Local rig file" below).

## Identify your device

Multiple DAQiFi units may be exposed on the same workstation
(e.g. when more than one agent session is attached to the same
machine). ALWAYS verify the SCPI `SYST:INFO?` response identifies
**your** device before any write.

Walk every exposed port and use the in-tree `daqifi` library to
identify each device. Match the `serial_number` against the unit
on your bench:

```bash
# WSL2 / Linux — walk every exposed CDC-ACM port.
for port in /dev/ttyACM0 /dev/ttyACM1 /dev/ttyACM2 /dev/ttyACM3; do
    [ -e "$port" ] || continue
    echo "=== $port ==="
    python -c "
from daqifi.device import NyquistDevice
dev = NyquistDevice.connect('$port')
print(f'  SN: {dev.serial_number} | model: {dev.model} | fw: {dev.firmware_version}')
dev.disconnect()
"
done
```

The matching port is your `DAQIFI_DEVICE_PORT` for the integration
suite. If multiple ports match no SN you recognize, STOP and check
which units the workstation owner expects to be attached.

## Local rig file (gitignored)

Per-workstation identifiers (your own device SN, COM port, LAN IP,
agent-private notes about other devices on the bench) belong in
`.claude/hardware-rig.md` — `.claude/` is gitignored, so this file
is never committed. Suggested template:

```markdown
# My hardware rig (NOT committed)
- DUT serial: <YOUR_DEVICE_SN>
- Windows COM port: <COMx>
- WiFi IP (when on LAN): <YOUR_DEVICE_IP>
- WSL2 device: /dev/ttyACM<N>
- Other devices to avoid writing to:
  - <OTHER_AGENT_SN> at <OTHER_AGENT_IP>
```

## Running the hardware integration suite

After identifying your device port, run the full hardware
integration suite — not just the targeted file for the change:

```bash
DAQIFI_DEVICE_PORT=/dev/ttyACM<N> DAQIFI_DEVICE_REQUIRED=1 \
    python -m pytest tests/integration/hardware/ -v
```

WiFi credentials (needed for B3 / B12 network-config tests) live
in `.env` (gitignored). The integration suite reads them from
`os.environ`, so export them yourself before running:

```bash
set -a; source .env; set +a
DAQIFI_DEVICE_PORT=/dev/ttyACM<N> DAQIFI_DEVICE_REQUIRED=1 \
    DAQIFI_EXPECTED_SERIAL_NUMBER=<YOUR_SN> \
    DAQIFI_TEST_WIFI_TRANSIENT_SSID=b12-transient-do-not-associate \
    DAQIFI_TEST_WIFI_TRANSIENT_PASSWORD=b12-transient-password-x \
    python -m pytest tests/integration/hardware/ -v
```

`.env` keys this expects:

| Key | Purpose |
|---|---|
| `DAQIFI_TEST_WIFI_SSID` | Real AP the destructive round-trip test restores to (the device is left associated to this SSID after each round-trip) |
| `DAQIFI_TEST_WIFI_PASSWORD` | Password for `DAQIFI_TEST_WIFI_SSID` |

These are credential-shaped values; never paste them into PR
descriptions, commit messages, or chat transcripts. If a password
ever leaks into chat, rotate it on the AP and update `.env`.

## STA-mode hardware testing (device on the LAN)

By default the NQ1 boots in **AP mode** (broadcasts its own
`DAQiFi-XXXX` SSID, sits at `192.168.1.1`). To exercise the WiFi
discovery + LAN-streaming tests, the device must first be
associated to a real AP — i.e. run the round-trip
network-config test once, then either reboot or wait for the
saved credentials to drive a fresh association on next boot.

Once the device is in **STA mode** and on the workstation's LAN:

- `tests/integration/hardware/test_discovery.py` — the WiFi
  discovery test that previously skipped (because our unit wasn't
  in discovery results) starts passing once the device is
  reachable on the LAN.
- The B3 LAN-streaming tests can also exercise TCP streaming over
  WiFi.

The `wait_for_device_back` fixture (in
`tests/integration/hardware/conftest.py`) handles the post-`*RST` /
post-`SYSTem:REboot` USB drop + re-enumeration cleanly, so STA-mode
tests that toggle WiFi state can run alongside USB-CDC tests
without leaving the suite in a cascading-failure state.

### Recovering from WiFi-SCPI silence (#73)

After many TCP/9760 connect-disconnect cycles in the same session
(especially mixed with USB CDC SCPI activity), the device's
WiFi-SCPI listener can wedge: the TCP socket still establishes
cleanly but `*IDN?` / `SYSTem:INFo?` / the protobuf `SYSInfoPB?`
return nothing. `DaqifiDeviceFactory.connect_tcp(...).initialize()`
now detects this — the call raises
`daqifi.exceptions.DeviceUnresponsiveError` after the configured
timeout (default 5 s on TCP) instead of returning silently with
empty metadata.

Recovery is a USB-side reboot:

```python
from daqifi.devices import DaqifiDeviceFactory
usb = DaqifiDeviceFactory.connect_serial(port_name="/dev/ttyACM0")
usb.send("SYSTem:REboot")  # WiFi-SCPI listener restarts on the next boot
```

The wedge is on the firmware side (parser context corruption on
the WiFi-SCPI path); the matching firmware ticket is
[daqifi-nyquist-firmware#437](https://github.com/daqifi/daqifi-nyquist-firmware/issues/437).

## WSL2 / usbipd setup (one-time, then per-disconnect)

WSL2 doesn't see USB devices without [usbipd-win](https://github.com/dorssel/usbipd-win):

```powershell
# Once, from elevated PowerShell on Windows
usbipd list                                    # find Bus-ID for your DAQiFi
usbipd bind --busid <X-Y>                      # share it (one-time per device)
usbipd attach --wsl --busid <X-Y>              # bind it into WSL
```

After each `*RST` or `SYSTem:REboot` the device drops from USB
and usbipd does NOT auto-reattach. Two ways to handle this in
the hardware suite:

1. **Manual** — `usbipd attach --wsl --busid <X-Y>` between
   destructive test runs. Tedious for full-suite runs because
   `test_reset_returns_to_clean_baseline` and
   `test_update_network_configuration_round_trip` both reboot.
2. **Automatic** — set `DAQIFI_USBIPD_BUSID=<X-Y>` and the
   `wait_for_device_back` fixture will re-attach every ~3s
   inside its 30s polling window. No-op on Windows native and
   bare Linux:

```bash
DAQIFI_DEVICE_PORT=/dev/ttyACM<N> DAQIFI_DEVICE_REQUIRED=1 \
    DAQIFI_EXPECTED_SERIAL_NUMBER=<YOUR_SN> \
    DAQIFI_USBIPD_BUSID=<X-Y> \
    python -m pytest tests/integration/hardware/ -v
```
