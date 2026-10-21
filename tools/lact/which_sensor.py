#!/usr/bin/env python3
"""Which temperature sensor is a card's LACT fan curve ACTUALLY following?

WHY THIS EXISTS.  The 3090's config says `temperature_key: vram`, and the card behaves as though it were keyed on edge
temperature.  Either the key is being honoured and the curve's useful band lies outside where the card operates (edge
saturates at 57 C under sustained load), or the key is silently ignored.  Reading the config cannot tell the two apart;
a curve that falls back to its default looks identical in YAML.

So measure it.  Sample the card's fan alongside every sensor candidate - edge, hotspot, the aggregate VRAM reading, and
each of the 24 per-chip VRAM sensors - evaluate the configured curve at each, and report which candidate's prediction
matches the reported fan speed.  The winner is the key in effect, whatever the config claims.

Run it under load: idle temperatures do not move enough to discriminate, and the script says so rather than pretending.

USAGE
  # with the engine running (a cooling_soak.sh in another shell, or any GPU load)
  python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/which_sensor.py --gpu 3090

  # drive the load yourself, pinned to the whole duration
  python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/which_sensor.py --gpu 3090 --seconds 120

No root needed: `lact cli stats` only reads.
"""
import argparse
import re
import statistics
import subprocess
import sys
import time

try:
    import yaml
except ImportError:
    sys.exit("needs python3-yaml (sudo apt install python3-yaml)")

CONFIG = "/etc/lact/config.yaml"
TARGETS = {"3090": "10DE:2204-1569:2204-0000:17:00.0", "4070": "10DE:2786-1043:89BB-0000:65:00.0"}


def load_curve(uid):
    cfg = yaml.safe_load(open(CONFIG, encoding="utf-8"))
    g = (cfg.get("gpus") or {}).get(uid) or {}
    fcs = g.get("fan_control_settings") or {}
    curve = fcs.get("curve") or {}
    pts = sorted((float(k), float(v)) for k, v in curve.items())
    return pts, fcs


def curve_at(pts, t):
    if not pts:
        return None
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t <= t1:
            return v0 + (t - t0) / (t1 - t0) * (v1 - v0)
    return pts[-1][1]


def sample(uid):
    r = subprocess.run(["lact", "cli", "-g", uid, "stats"], capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    d = {}
    m = re.search(r"GPU: (\d+)", out)
    if m:
        d["edge"] = int(m.group(1))
    m = re.search(r"GPU Hotspot: (\d+)", out)
    if m:
        d["hotspot"] = int(m.group(1))
    m = re.search(r"\bVRAM: (\d+)", out)
    if m:
        d["vram"] = int(m.group(1))
    for chip, face, val in re.findall(r"Chip (\w+) \((Front|Back)\): (\d+)", out):
        d[f"{chip} {face}"] = int(val)
    m = re.search(r"Fan Speed: (\d+)%", out)
    if m:
        d["_fan"] = int(m.group(1))
    return d


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gpu", default="3090", help="3090 or 4070 (default 3090)")
    ap.add_argument("--seconds", type=int, default=90, help="sampling window (default 90)")
    ap.add_argument("--interval", type=float, default=5.0, help="sample interval in seconds (default 5)")
    a = ap.parse_args()
    uid = TARGETS.get(a.gpu, a.gpu)

    pts, fcs = load_curve(uid)
    if not pts:
        sys.exit(f"no curve configured for {a.gpu} in {CONFIG}")
    print(f"{a.gpu}: mode={fcs.get('mode')} temperature_key={fcs.get('temperature_key')!r}")
    print(f"curve: {' '.join(f'{t:.0f}:{v:.2f}' for t, v in pts)}")
    print(f"sampling every {a.interval:.0f}s for {a.seconds}s - this needs GPU load to discriminate\n")

    rows = []
    end = time.time() + a.seconds
    while time.time() < end:
        d = sample(uid)
        if "_fan" in d:
            rows.append(d)
        time.sleep(a.interval)
    if len(rows) < 3:
        sys.exit(f"only {len(rows)} samples - is LACT reachable?")

    fans = [r["_fan"] for r in rows]
    keys = [k for k in rows[0] if not k.startswith("_")]
    span = {k: max(r[k] for r in rows) - min(r[k] for r in rows) for k in keys}
    if max(span.values()) < 4:
        print("WARNING: no sensor moved more than 4 C - there is not enough load to discriminate.\n"
              "         Start a GPU workload and re-run, or use --seconds with load already running.")

    print(f"{'candidate key':12s} {'mean err':>8s} {'max err':>8s}  {'predicted vs actual fan':>26s}")
    scored = []
    for k in keys:
        errs = [abs(curve_at(pts, r[k]) * 100 - r["_fan"]) for r in rows]
        scored.append((statistics.mean(errs), max(errs), k))
    scored.sort()
    for mean_err, max_err, k in scored:
        bar = "  <-- best fit" if (mean_err, max_err, k) == scored[0] else ""
        print(f"  {k:14s} {mean_err:7.1f}% {max_err:7.1f}%{bar}")

    winner = scored[0][2]
    configured = fcs.get("temperature_key")
    print(f"\nfan tracks: {winner}      config claims: {configured}")
    if winner != configured:
        print("  -> the curve is following a different sensor than the config states.")
    print(f"fan range over the window: {min(fans)}% - {max(fans)}%")
    print("\nper-sample detail (last 5):")
    cols = ["edge", "hotspot", "vram", winner, "_fan"]
    print("  " + "  ".join(f"{c:>10s}" for c in cols))
    for r in rows[-5:]:
        print("  " + "  ".join(f"{r.get(c, '?'):>10}" for c in cols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
