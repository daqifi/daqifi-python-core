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
    python -m pytest tests/integration/hardware/ -v
```
