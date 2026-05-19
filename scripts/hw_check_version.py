"""Query every plausible firmware-version SCPI command on COM7."""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from daqifi.transport import SerialStreamTransport


def send(ser, line, delay=0.3):
    ser.write((line + "\r").encode("ascii"))
    try:
        ser.flush()
    except Exception:
        pass
    time.sleep(delay)


def drain_collect(ser, timeout=0.5):
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        n = ser.in_waiting
        if n > 0:
            buf.extend(ser.read(n))
        else:
            time.sleep(0.005)
    return bytes(buf)


def query(ser, cmd):
    send(ser, cmd, delay=0.2)
    reply = drain_collect(ser, timeout=0.6)
    text = reply.decode("ascii", errors="replace")
    # Strip the echoed command and trailing prompt
    lines = [ln.strip() for ln in text.split("\r\n")]
    lines = [ln for ln in lines
             if ln and ln != cmd and ln != "DAQIFI>"]
    err = any("ERROR" in ln for ln in lines)
    flag = " [ERROR]" if err else ""
    print(f"  > {cmd}{flag}")
    for ln in lines:
        print(f"      {ln}")


def main():
    transport = SerialStreamTransport(port_name="COM7", baud_rate=115200)
    transport.connect()
    ser = transport._serial
    try:
        # Clear errors first
        send(ser, "*CLS", delay=0.2)
        drain_collect(ser, timeout=0.3)

        candidates = [
            "*IDN?",
            "*OPT?",
            "SYSTem:VERSion?",
            "SYST:VERS?",
            "SYST:VER?",
            "SYSTem:FWREV?",
            "SYST:FWREV?",
            "SYST:FW?",
            "SYST:FIRMware?",
            "SYST:BUILD?",
            "SYSTem:BUILD?",
            "SYST:HW?",
            "SYST:SN?",
            "SYST:SERial?",
            "SYSTem:INFO?",
            "SYST:INFO?",
            # Daqifi-specific guesses
            "SYST:DEV?",
            "SYST:DEVice?",
            "SYST:DEV:VERS?",
            "SYSTem:NQ:VERSion?",
            "SYST:NQ:VERS?",
            "?",
        ]
        for cmd in candidates:
            query(ser, cmd)
    finally:
        transport.disconnect()


if __name__ == "__main__":
    main()
