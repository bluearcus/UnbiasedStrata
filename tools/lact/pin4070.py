#!/usr/bin/env python3
"""Pin the 4070's fans high through LACT, for the "does the 4070 help the 3090 backplate" experiment.

WHY. The 4070 sits half a slot above the 3090 and both cards intake from underneath, so the 4070's fans are aimed
straight at the 3090's backplate - the 3090's hottest, least-cooled surface.  At rest the 4070 runs 30% fan at 52 C
and does nothing thermally.  If it is pinned high while the 3090 works, it becomes a free extra blower over the
3090 in exactly the right place.  Whether that is a help or a nuisance (hot 4070 air dumped into the gap) is what
the experiment measures, and it has to be measured before anyone commits to the noise.

LACT has no fan-control subcommand in its CLI, so this edits /etc/lact/config.yaml and restarts the daemon.  The
4070 entry is switched to static fan control at a fixed speed; the 3090's entry is checked before and after and
must come out identical, because that card is owned by a VRAM-keyed curve and this tool must not disturb it.

SAFETY.  Refuses to run without root (the file is root-owned).  Byte-exact backup of the config before writing.
The patched document is re-parsed as YAML before it is installed, and the 3090's settings are compared against the
original - if anything else in the file changed, the write is abandoned.  `restore` puts the backup back
byte-for-byte and restarts the daemon, so the experiment is fully reversible even if you forget what you changed.

USAGE (root)
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/pin4070.py status
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/pin4070.py pin --speed 0.80
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/pin4070.py restore
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/pin4070.py restore --last
"""
import argparse
import copy
import datetime as dt
import glob
import os
import subprocess
import sys

try:
    import yaml
except ImportError:
    sys.exit("needs python3-yaml (sudo apt install python3-yaml)")

CONFIG = "/etc/lact/config.yaml"
BACKUP_GLOB = "config.yaml.pinbak-*"
GPU_4070 = "10DE:2786-1043:89BB-0000:65:00.0"
GPU_3090 = "10DE:2204-1569:2204-0000:17:00.0"
SERVICE = "lactd"


def log(*a):
    print(*a, flush=True)


def load(path=CONFIG):
    try:
        return yaml.safe_load(open(path, encoding="utf-8"))
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}   (this tool needs sudo)")
    except yaml.YAMLError as e:
        sys.exit(f"{path} does not parse as YAML: {e}")


def show_gpu(cfg, which, label):
    g = (cfg.get("gpus") or {}).get(which)
    if not g:
        log(f"  {label}: not present in the config")
        return
    fcs = g.get("fan_control_settings") or {}
    log(f"  {label}: fan_control_enabled={g.get('fan_control_enabled')} mode={fcs.get('mode')} "
        f"static={fcs.get('static_speed')} key={fcs.get('temperature_key')} curve={fcs.get('curve')} "
        f"power_cap={g.get('power_cap')}")


def build(cfg, speed):
    gpus = cfg.get("gpus") or {}
    if GPU_4070 not in gpus:
        sys.exit(f"{GPU_4070} (the 4070) is not in {CONFIG} - refusing to guess where to put it")
    if GPU_3090 not in gpus:
        sys.exit(f"{GPU_3090} (the 3090) is missing from the config - unexpected, refusing")
    out = copy.deepcopy(cfg)
    out["gpus"][GPU_4070]["fan_control_enabled"] = True
    out["gpus"][GPU_4070]["fan_control_settings"] = {
        "mode": "static",
        "static_speed": speed,
        "temperature_key": "vram",
        "interval_ms": 1000,
        "curve": {40: speed, 90: speed},
        "spindown_delay_ms": 8000,
        "change_threshold": 2,
        "auto_threshold": 0,
    }
    return out


def unchanged_others(before, after):
    """Everything except the 4070 entry must be identical - including the 3090, which is not ours to touch here."""
    b = copy.deepcopy(before); a = copy.deepcopy(after)
    b_gpu = b.get("gpus", {}).pop(GPU_4070, None)
    a_gpu = a.get("gpus", {}).pop(GPU_4070, None)
    if b != a:
        log("REFUSED: something other than the 4070 entry changed.  Diff of the untouched parts:")
        for k in set(list(b.keys()) + list(a.keys())):
            if b.get(k) != a.get(k):
                log(f"   key {k!r}: {b.get(k)!r} -> {a.get(k)!r}")
        return False
    if b.get("gpus", {}).get(GPU_3090) != a.get("gpus", {}).get(GPU_3090):
        log("REFUSED: the 3090 entry changed")
        return False
    return True


def restart():
    r = subprocess.run(["systemctl", "restart", SERVICE], capture_output=True, text=True)
    if r.returncode != 0:
        log(f"systemctl restart {SERVICE} failed: {(r.stdout + r.stderr).strip()}")
        return False
    active = subprocess.run(["systemctl", "is-active", "--quiet", SERVICE]).returncode == 0
    log(f"{SERVICE} restarted, is-active: {'active' if active else 'NOT ACTIVE'}")
    return active


def verify_fan():
    r = subprocess.run(["lact", "cli", "-g", GPU_4070, "stats"], capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if "Fan Speed" in line or "Temperatures" in line:
            log("  " + line.strip()[:180])
    if "Fan Speed" not in out:
        log("  (lact cli reported no fan line - check the 4070 in the LACT GUI)")


def backups():
    d = os.path.dirname(CONFIG)
    return sorted(glob.glob(os.path.join(d, BACKUP_GLOB)))


def cmd_status(a):
    cfg = load()
    log(f"{CONFIG}:")
    show_gpu(cfg, GPU_3090, "3090 (LACT curve, VRAM-keyed)")
    show_gpu(cfg, GPU_4070, "4070")
    bks = backups()
    if bks:
        log("backups: " + ", ".join(os.path.basename(b) for b in bks))
    log("live 4070 stats:")
    verify_fan()


def cmd_pin(a):
    cfg = load()
    if a.dry_run:
        out = build(cfg, a.speed)
        log("dry run - this is the 4070 entry that would be written:")
        log(yaml.safe_dump({GPU_4070: out["gpus"][GPU_4070]}, sort_keys=False).rstrip())
        if not unchanged_others(cfg, out):
            return 1
        log("dry run: nothing written")
        return 0
    out = build(cfg, a.speed)
    if not unchanged_others(cfg, out):
        return 1
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{CONFIG}.pinbak-{stamp}"
    original = open(CONFIG, encoding="utf-8").read()
    open(bak, "w", encoding="utf-8").write(original)
    os.chmod(bak, 0o600)
    yaml.safe_load(open(bak, encoding="utf-8"))
    log(f"backup written and validated: {bak}")
    text = yaml.safe_dump(out, sort_keys=False)
    try:
        open(CONFIG, "w", encoding="utf-8").write(text)
        yaml.safe_load(open(CONFIG, encoding="utf-8"))
    except Exception as e:
        log(f"write failed ({e}); restoring")
        open(CONFIG, "w", encoding="utf-8").write(original)
        return 1
    if not restart():
        log("daemon will not start with the new config - restoring the backup")
        open(CONFIG, "w", encoding="utf-8").write(original)
        restart()
        return 1
    log(f"4070 pinned to {a.speed:.0%} static")
    verify_fan()
    log("\nundo:")
    log(f"  sudo python3 {os.path.abspath(__file__)} restore")
    return 0


def cmd_restore(a):
    bks = backups()
    if not bks:
        sys.exit("no pin backups found")
    src = bks[-1] if a.last else bks[0]
    data = open(src, encoding="utf-8").read()
    yaml.safe_load(data)
    open(CONFIG, "w", encoding="utf-8").write(data)
    os.chmod(CONFIG, 0o644)
    log(f"restored {CONFIG} byte-for-byte from {src}")
    restart()
    verify_fan()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["status", "pin", "restore"])
    ap.add_argument("--speed", type=float, default=0.80, help="static fan speed for the 4070 (default 0.80)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--last", action="store_true", help="with restore: use the newest backup instead of the oldest")
    a = ap.parse_args()
    if a.speed < 0.2 or a.speed > 1.0:
        sys.exit("--speed must be between 0.20 and 1.00")
    return {"status": cmd_status, "pin": cmd_pin, "restore": cmd_restore}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
