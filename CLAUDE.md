# Hardware test rig

Persistent hardware identifiers for this workstation. Use these
when running integration tests; never assume a default port.

## DAQiFi NQ1 (DUT — this is OUR device)

- **Serial number**: `9090603604263365351`
- **Board name**: `DAQiFi-A7B4`
- **Hardware**: NQ1, HW 2.0.0, FW 3.4.6b1
- **Windows COM port**: `COM9`
- **WiFi IP (when on LAN)**: `192.168.1.60`
- **WSL2 mapping**: `/dev/ttyACM<N>` after `usbipd attach --wsl --busid <X-Y>`

`<N>` is `0` or `1` depending on enumeration order. ALWAYS verify
the SN before any write — another agent's NQ1 may also be exposed.

```bash
# Walk every exposed ACM port; the matching SN is the user's device.
for port in /dev/ttyACM0 /dev/ttyACM1 /dev/ttyACM2 /dev/ttyACM3; do
    [ -e "$port" ] || continue
    echo "=== $port ==="
    bash ~/.claude/skills/scpi/scpi.sh "SYST:INFo?" "$port" 2000
done
```

## Other agent's device — DO NOT WRITE

A second NQ1 (SN `9090684023231015079`, IP `192.168.1.160`)
belongs to another agent's session. If `SYST:INFo?` returns that
SN on any port — STOP. Never write to it.

## Running hardware integration tests

After identifying the user's device port, run the full hardware
integration suite — not just the targeted file for the change:

```bash
cd /mnt/c/Users/User/Documents/GitHub/daqifi-python-core
DAQIFI_DEVICE_PORT=/dev/ttyACM<N> DAQIFI_DEVICE_REQUIRED=1 \
    python3 -m pytest tests/integration/hardware/ -v
```

WiFi credentials (needed for B3 / B12 network-config tests) live
in `daqifi-python-core/.env` (gitignored), loaded automatically
by tests that need them.
