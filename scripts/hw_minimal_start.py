"""Try various minimal start sequences to find what works on this firmware.

This firmware (DAQiFi,Nq1,0,01-02) appears older than test_harness targets:
- SYST:STR:BENCHmark, SYST:STR:TESTpattern, SYST:STOR:SD:ENAble all
  return -113 "Undefined header"
- SYST:STReam:START 100 returns -200 "Execution error" after the
  test_harness device_reset sequence
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


def looks_like_error(reply: bytes) -> bool:
    return b"**ERROR" in reply


def try_start(ser, label, setup_lines, start_line, stream_seconds=2.0):
    print(f"\n========== {label} ==========")
    # Drain any stale output first
    send(ser, "SYST:STReam:STOP", delay=0.5)
    drain_collect(ser, timeout=1.0)

    for line in setup_lines:
        send(ser, line, delay=0.2)
        reply = drain_collect(ser, timeout=0.3)
        marker = "  [error]" if looks_like_error(reply) else ""
        print(f"  > {line}{marker}")
        if looks_like_error(reply):
            # Print errors only
            for chunk in reply.split(b"\r\n"):
                if b"ERROR" in chunk:
                    print(f"    {chunk.decode('ascii', errors='replace')}")

    # Drain echo of start command immediately, then watch for stream data
    send(ser, start_line, delay=0.0)
    deadline = time.monotonic() + stream_seconds
    buf = bytearray()
    while time.monotonic() < deadline:
        n = ser.in_waiting
        if n > 0:
            buf.extend(ser.read(n))
        else:
            time.sleep(0.001)

    print(f"  > {start_line} [{stream_seconds}s window]")
    print(f"  Bytes received: {len(buf)}")
    if len(buf) < 200:
        # Likely just echo + error
        hexdump(bytes(buf), prefix="    ")
    else:
        # Likely streaming — show first 80 bytes
        hexdump(bytes(buf[:80]), prefix="    ")
        print(f"    ... ({len(buf) - 80} more bytes)")

    send(ser, "SYST:STReam:STOP", delay=0.5)
    drain_collect(ser, timeout=1.0)


def main():
    transport = SerialStreamTransport(port_name=PORT, baud_rate=115200)
    transport.connect()
    ser = transport._serial
    try:
        # Minimal: just enable ch0 and start
        try_start(
            ser,
            "Variant A: bitmask enable + STReam:START",
            ["ENABle:VOLTage:DC 1"],
            "SYST:STReam:START 100",
        )

        # Per-channel enable form
        try_start(
            ser,
            "Variant B: per-channel ENABLE:VOLT:DC 0,1 + STReam:START",
            ["ENABle:VOLTage:DC 0,1"],
            "SYST:STReam:START 100",
        )

        # Add MEM:RESet before
        try_start(
            ser,
            "Variant C: MEM:RESet then enable then start",
            ["SYST:MEM:RESet", "ENABle:VOLTage:DC 1"],
            "SYST:STReam:START 100",
        )

        # All bits set (all 16 channels)
        try_start(
            ser,
            "Variant D: enable all channels + start",
            ["ENABle:VOLTage:DC 65535"],
            "SYST:STReam:START 100",
        )

        # Try short form
        try_start(
            ser,
            "Variant E: short SYST:STR:STATe with enable",
            ["ENABle:VOLT:DC 1"],
            "SYST:STARtSTReamDATa 100",
        )

    finally:
        transport.disconnect()


if __name__ == "__main__":
    main()
