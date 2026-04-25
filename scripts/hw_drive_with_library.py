"""Drive COM7 through the daqifi library, checking SYST:ERR? after every command.

Library inventory:
- `daqifi._internal.comm.SerialInterface` provides send_command / send_query /
  send_set (auto-checks errors only on .send_set) / clear_errors / reset.
- `daqifi._internal.scpi_commands` provides typed factories: turn_device_on,
  enable_adc_channels, set_stream_interface, set_protobuf_stream_format,
  start_streaming, stop_streaming, ... — each returns an ScpiMessage.
- `daqifi._internal.scpi.System.POWER_STATE_QUERY/SET` for waking from Standby.

What this script does that the library doesn't (yet):
1. Wraps `send_command` in a small `checked()` helper that ALWAYS queries
   `SYST:ERR?` afterwards and surfaces the failure.
2. Sequences the wake/init so the device leaves Standby before
   StartStreamData is attempted.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from daqifi._internal.comm import SerialInterface
from daqifi._internal import scpi_commands as cmds
from daqifi._internal.scpi import StreamInterface, System


PORT = "COM7"


class CheckedSerial:
    """Wrap SerialInterface with mandatory SYST:ERR? after every command.

    Records the SCPI trace so a single failure dump shows the whole sequence.
    """

    def __init__(self, port: str):
        self._inner = SerialInterface(port=port, baudrate=115200, timeout=1.0)
        self.trace: list[tuple[str, str, str]] = []

    def open(self) -> bool:
        return self._inner.open()

    def close(self) -> None:
        self._inner.close()

    def _read_err(self) -> str:
        # Bypass send_query (which would recurse). Inline a single query.
        return self._inner.send_command("SYST:ERR?", delay=0.15)

    def query(self, q: str, delay: float = 0.2) -> tuple[str, str]:
        resp = self._inner.send_command(q, delay)
        err = self._read_err()
        self.trace.append((q, resp, err))
        return resp, err

    def send(self, cmd: str, delay: float = 0.2) -> str:
        resp = self._inner.send_command(cmd, delay)
        err = self._read_err()
        self.trace.append((cmd, resp, err))
        return err

    def clear_errors(self) -> None:
        self._inner.clear_errors()


def run_step(label: str, ok: bool) -> None:
    flag = "OK " if ok else "ERR"
    print(f"  [{flag}] {label}")


def is_no_error(err_response: str) -> bool:
    return "No error" in err_response or '0,"No error"' in err_response


def main() -> int:
    ser = CheckedSerial(PORT)
    if not ser.open():
        print(f"FAILED to open {PORT}")
        return 2
    print(f"opened {PORT}\n")

    try:
        # 1. Clear any stale errors
        ser.clear_errors()
        print("error queue cleared\n")

        # 2. Identify and read state
        idn, err = ser.query(cmds.ScpiMessage("*IDN?").data
                             if False else "*IDN?")
        print(f"  *IDN? -> {idn!r}  err: {err!r}")
        run_step("*IDN?", is_no_error(err))

        info, err = ser.query("SYST:INFO?")
        print(f"  SYST:INFO? -> ({len(info)} bytes); err: {err!r}")
        # Show 'Power state' and 'Enabled user ch' lines
        for line in info.split("\n"):
            line = line.strip()
            if "State:" in line or "Enabled user ch" in line or "Streaming:" in line:
                print(f"      {line}")
        run_step("SYST:INFO?", is_no_error(err))

        # 3. Wake from Standby — POWer:STATe 1
        pow_q, err = ser.query(System.POWER_STATE_QUERY)
        print(f"  power state pre-wake: {pow_q!r}  err: {err!r}")
        run_step("SYST:POW:STAT? (pre)", is_no_error(err))

        wake_msg = cmds.turn_device_on()
        err = ser.send(wake_msg.data, delay=0.5)
        print(f"  sent {wake_msg.data!r}  err: {err!r}")
        run_step("turn_device_on()", is_no_error(err))

        time.sleep(0.5)

        pow_q, err = ser.query(System.POWER_STATE_QUERY)
        print(f"  power state post-wake: {pow_q!r}  err: {err!r}")
        run_step("SYST:POW:STAT? (post)", is_no_error(err))

        # 4. Set stream interface USB
        si_msg = cmds.set_stream_interface(StreamInterface.Usb)
        err = ser.send(si_msg.data)
        print(f"  sent {si_msg.data!r}  err: {err!r}")
        run_step("set_stream_interface(Usb)", is_no_error(err))

        # 5. Set stream format protobuf
        fmt_msg = cmds.set_protobuf_stream_format()
        err = ser.send(fmt_msg.data)
        print(f"  sent {fmt_msg.data!r}  err: {err!r}")
        run_step("set_protobuf_stream_format()", is_no_error(err))

        # 6. Enable channel 0 (bitmask 1)
        ch_msg = cmds.enable_adc_channels("1")
        err = ser.send(ch_msg.data)
        print(f"  sent {ch_msg.data!r}  err: {err!r}")
        run_step('enable_adc_channels("1")', is_no_error(err))

        # 7. Re-check enabled channel count + power state via SYST:INFO?
        info, err = ser.query("SYST:INFO?")
        for line in info.split("\n"):
            line = line.strip()
            if "State:" in line or "Enabled user ch" in line or "Streaming:" in line:
                print(f"      {line}")
        run_step("SYST:INFO? (post-enable)", is_no_error(err))

        # 8. Start streaming at 100 Hz
        start_msg = cmds.start_streaming(100)
        err = ser.send(start_msg.data, delay=0.5)
        print(f"  sent {start_msg.data!r}  err: {err!r}")
        run_step("start_streaming(100)", is_no_error(err))

        # 9. Read streaming data for 1.5s
        time.sleep(0.2)
        data = b""
        deadline = time.time() + 1.5
        while time.time() < deadline:
            n = ser._inner._serial.in_waiting
            if n > 0:
                data += ser._inner._serial.read(n)
            else:
                time.sleep(0.001)
        print(f"\n  streamed {len(data)} bytes in 1.5s @ 100 Hz")
        if 0 < len(data) < 200:
            print("  hex preview:")
            for i in range(0, min(len(data), 80), 16):
                chunk = data[i:i + 16]
                hex_part = " ".join(f"{b:02x}" for b in chunk)
                ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                print(f"    {i:04x}  {hex_part:<48}  {ascii_part}")

        # 10. Stop streaming
        stop_msg = cmds.stop_streaming()
        err = ser.send(stop_msg.data, delay=0.5)
        run_step("stop_streaming()", is_no_error(err))

        return 0 if len(data) > 0 else 1

    finally:
        # Trace dump on any path
        print("\n========== SCPI trace ==========")
        for cmd, resp, err in ser.trace:
            ok = "OK " if is_no_error(err) else "ERR"
            err_short = err.replace("\n", " ").replace("\r", "")[:80]
            resp_short = resp.replace("\n", " ").replace("\r", "")[:60]
            print(f"  [{ok}] {cmd!r:40} resp={resp_short!r:62} err={err_short!r}")
        ser.close()


if __name__ == "__main__":
    sys.exit(main())
