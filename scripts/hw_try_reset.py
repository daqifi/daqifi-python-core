"""Try *CLS and *RST to clear whatever state is blocking StartStreamData."""

from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from daqifi.transport import SerialStreamTransport


PORT = "COM7"


def hexdump(data: bytes, prefix: str = "") -> None:
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print(f"{prefix}{i:04x}  {hex_part:<48}  {ascii_part}")


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


def step(ser, line, delay=0.3, drain_timeout=0.4, label=None):
    send(ser, line, delay=delay)
    reply = drain_collect(ser, timeout=drain_timeout)
    label = label or line
    has_err = b"ERROR" in reply
    flag = " [ERROR]" if has_err else ""
    print(f"  > {label}{flag} ({len(reply)} bytes)")
    if has_err or len(reply) < 80:
        for chunk in reply.split(b"\r\n"):
            chunk = chunk.strip()
            if chunk and chunk != b"DAQIFI>":
                try:
                    print(f"    {chunk.decode('ascii', errors='replace')}")
                except Exception:
                    print(f"    {chunk!r}")
    return reply


def main():
    transport = SerialStreamTransport(port_name=PORT, baud_rate=115200)
    transport.connect()
    ser = transport._serial
    try:
        print("\n=== Step 1: drain anything stale ===")
        drain_collect(ser, timeout=1.0)

        print("\n=== Step 2: *CLS — clear error queue ===")
        step(ser, "*CLS")

        print("\n=== Step 3: *ESR? — read event status register ===")
        step(ser, "*ESR?")

        print("\n=== Step 4: SYST:ERR? — read error queue ===")
        for _ in range(8):
            step(ser, "SYST:ERR?")

        print("\n=== Step 5: enable ch0 + try start ===")
        step(ser, "ENABle:VOLTage:DC 1")
        step(ser, "SYST:StartStreamData 100", delay=0.0, drain_timeout=1.5)

        print("\n=== Step 6: check stream stats ===")
        reply = step(ser, "SYST:STR:STATS?", delay=0.5, drain_timeout=1.5)

        print("\n=== Step 7: stop & try with *RST first ===")
        step(ser, "SYST:StopStreamData", delay=1.0)
        drain_collect(ser, timeout=1.0)

        step(ser, "*RST", delay=2.0, drain_timeout=2.0)
        step(ser, "*CLS")
        step(ser, "*IDN?")
        step(ser, "ENABle:VOLTage:DC 1")
        step(ser, "SYST:StartStreamData 100", delay=0.0, drain_timeout=1.5)

        # See if data flows now
        print("\n=== Step 8: 1.5s data window after *RST start ===")
        deadline = time.monotonic() + 1.5
        buf = bytearray()
        while time.monotonic() < deadline:
            n = ser.in_waiting
            if n > 0:
                buf.extend(ser.read(n))
            else:
                time.sleep(0.001)
        print(f"  bytes received: {len(buf)}")
        if len(buf) < 256:
            hexdump(bytes(buf), prefix="    ")
        else:
            hexdump(bytes(buf[:128]), prefix="    ")
            print(f"    ... ({len(buf) - 128} more bytes)")

        step(ser, "SYST:StopStreamData", delay=1.0)

    finally:
        transport.disconnect()


if __name__ == "__main__":
    main()
