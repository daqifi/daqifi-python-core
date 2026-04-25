"""Diagnostic: dump raw bytes from COM7 during a streaming attempt.

We're getting only ~98 bytes when ~36000 are expected at 1 kHz over 2s.
Print the actual byte content (hex + ascii) to understand what we ARE getting.
"""

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


def drain_and_dump(ser, label, timeout=0.5):
    deadline = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < deadline:
        n = ser.in_waiting
        if n > 0:
            buf.extend(ser.read(n))
        else:
            time.sleep(0.005)
    print(f"\n--- {label}: {len(buf)} bytes ---")
    if buf:
        hexdump(bytes(buf), prefix="  ")
    else:
        print("  (empty)")
    return bytes(buf)


def main():
    transport = SerialStreamTransport(
        port_name=PORT,
        baud_rate=115200,
    )
    transport.connect()
    ser = transport._serial
    try:
        # Initial drain
        drain_and_dump(ser, "initial idle drain")

        send(ser, "SYST:StopStreamData", delay=1.0)
        drain_and_dump(ser, "after StopStreamData")

        send(ser, "SYST:STR:BENCHmark 0", delay=0.2)
        drain_and_dump(ser, "after BENCHmark 0")

        send(ser, "SYST:STR:TESTpattern 0", delay=0.2)
        drain_and_dump(ser, "after TESTpattern 0")

        send(ser, "SYST:STOR:SD:ENAble 0", delay=1.0)
        drain_and_dump(ser, "after SD:ENAble 0")

        send(ser, "ENABle:VOLTage:DC 0", delay=0.2)
        drain_and_dump(ser, "after ENABle:VOLT:DC 0")

        send(ser, "SYST:STR:INT 0", delay=0.2)
        drain_and_dump(ser, "after STR:INT 0")

        send(ser, "SYST:MEM:RESet", delay=0.3)
        drain_and_dump(ser, "after MEM:RESet")

        # Identity query — proves the device responds at all
        send(ser, "*IDN?", delay=0.5)
        drain_and_dump(ser, "after *IDN?")

        # Now enable channel 0 and start
        send(ser, "ENABle:VOLTage:DC 1", delay=0.2)
        drain_and_dump(ser, "after ENABle:VOLT:DC 1")

        send(ser, "SYST:StartStreamData 100", delay=0.0)

        # Read whatever flows for 2s
        deadline = time.monotonic() + 2.0
        buf = bytearray()
        while time.monotonic() < deadline:
            n = ser.in_waiting
            if n > 0:
                buf.extend(ser.read(n))
            else:
                time.sleep(0.001)
        print(f"\n--- 2s of streaming @ 100 Hz: {len(buf)} bytes ---")
        if buf:
            # Only print first 256 bytes if huge
            preview = bytes(buf[:256])
            hexdump(preview, prefix="  ")
            if len(buf) > 256:
                print(f"  ... ({len(buf) - 256} more bytes)")
        else:
            print("  (empty)")

        send(ser, "SYST:StopStreamData", delay=1.0)
        drain_and_dump(ser, "after final stop")

        # Sanity: query stream stats
        send(ser, "SYST:STR:STATS?", delay=0.5)
        drain_and_dump(ser, "after STR:STATS?", timeout=1.0)

    finally:
        transport.disconnect()


if __name__ == "__main__":
    main()
