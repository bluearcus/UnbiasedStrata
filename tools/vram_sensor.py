#!/usr/bin/env python3
"""vram_sensor - publish the 3090's GDDR6X temperature where CoolerControl can read it.

This is a sensor exporter, not a control loop.  CoolerControl (daemon + web UI) does the fan control: hwmon
channels on the motherboard's NCT6797, Graph Profiles, Mix Profiles, Functions for hysteresis and response time,
and a failsafe of 100 C when a source disappears.  Its NVIDIA backend exposes the GPU temperature and, on some
cards, the hotspot - but not memory temperature (`nvidia-smi -q -d TEMPERATURE` says `Memory Current Temp: N/A`
on this card; the per-chip GDDR6X readings come from NvAPI, which LACT reaches).

CoolerControl's "File" custom sensor is designed for exactly this: "useful when an external tool exports a
reading not natively detected by CoolerControl", temperature in millidegrees Celsius.  So this script reads the
VRAM temperature from the LACT daemon socket and writes one whole number to a file.  Everything else - curve,
smoothing, hysteresis, which header it drives - is configured in CoolerControl's interface.

USAGE
  vram_sensor [--file PATH] [--interval 5] [--key VRAM] [--gpu ID]

WHEN LACT IS UNREACHABLE it writes 100000 (100 C) rather than leaving a stale, lower number in the file: a
missing GPU reading should make the case fans faster, not slower.
"""
import argparse
import json
import os
import socket
import sys
import tempfile
import time

DEFAULT_FILE = "/run/vramsensor/vram.temp1"
DEFAULT_GPU = "10DE:2204-1569:2204-0000:17:00.0"   # the RTX 3090
SOCK = os.environ.get("VRAM_SENSOR_LACT", "/run/lactd.sock")


def lact_call(cmd, args=None):
    req = {"command": cmd} if args is None else {"command": cmd, "args": args}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(10)
    s.connect(SOCK)
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while b"\n" not in buf:
        c = s.recv(65536)
        if not c:
            break
        buf += c
    s.close()
    return json.loads(buf.decode().split("\n")[0])


def read_vram(gpu_id, key):
    d = lact_call("device_stats", {"id": gpu_id}).get("data", {})
    temps = d.get("temps", {})
    if key not in temps:
        raise KeyError(f"{key!r} not in LACT stats; available: {', '.join(list(temps)[:6])}...")
    agg = float(temps[key]["current"])
    chips = [v["current"] for k, v in temps.items() if k.startswith("VRAM Chip")]
    return max([agg] + chips)          # the hottest thing on the PCB, not the average


def write_millideg(path, temp_c):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d or ".", prefix=".vram")
    os.fchmod(fd, 0o644)               # mkstemp defaults to 0600; the reader is another program (and you)
    with os.fdopen(fd, "w") as f:
        f.write(f"{int(round(temp_c * 1000))}\n")
    os.replace(tmp, path)              # atomic: CoolerControl never reads a half-written file


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument("--interval", type=float, default=5)
    ap.add_argument("--key", default="VRAM")
    ap.add_argument("--gpu", default=DEFAULT_GPU)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    while True:
        try:
            t = read_vram(a.gpu, a.key)
            write_millideg(a.file, t)
            print(time.strftime("%H:%M:%S"), f"{a.file} <- {t:.1f} C", flush=True)
        except Exception as e:
            try:
                write_millideg(a.file, 100.0)
                print(time.strftime("%H:%M:%S"), f"read failed ({e}); wrote 100000 so the fans go to full", flush=True)
            except Exception as e2:
                print(time.strftime("%H:%M:%S"), f"read failed ({e}) and the file could not be written ({e2})", file=sys.stderr, flush=True)
        if a.once:
            return 0
        time.sleep(a.interval)


if __name__ == "__main__":
    sys.exit(main())
