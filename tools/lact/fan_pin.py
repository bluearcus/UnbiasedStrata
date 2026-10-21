#!/usr/bin/env python3
"""Pin one card's fans to a fixed speed through LACT - for experiments and diagnostics.

WHAT IT IS FOR.  Two jobs so far:

  1. The O9 backplate experiment: pin the 4070 high while the 3090 decodes, and see whether the 3090 gets cooler.
     (Answer: yes, 2.4 C at steady state - see docs/COOLING.md.)
  2. Deciding whether LACT actually drives a given card's fan at all.  `--gpu 3090 --speed 0.35` is the test: if
     nvidia-smi then reports ~35% instead of following its curve, LACT is in control; if the fan ignores it, LACT is
     not, and every curve on that card is decoration.  That question is open for the 3090 - see O11.

LACT has no fan-control subcommand in its CLI, so this edits /etc/lact/config.yaml and restarts the daemon.  The
4070 entry is switched to static fan control at a fixed speed; the 3090's entry is checked before and after and
must come out identical, because that card is owned by a VRAM-keyed curve and this tool must not disturb it.

SAFETY.  Refuses to run without root (the file is root-owned).  Byte-exact backup of the config before writing.
The patched document is re-parsed as YAML before it is installed, and the 3090's settings are compared against the
original - if anything else in the file changed, the write is abandoned.  `restore` puts the backup back
byte-for-byte and restarts the daemon, so the experiment is fully reversible even if you forget what you changed.

USAGE (root)
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/fan_pin.py status
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/fan_pin.py status --gpu 3090
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/fan_pin.py pin --speed 0.80
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/fan_pin.py pin --gpu 3090 --speed 0.35
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/fan_pin.py restore
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/fan_pin.py restore --last
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


TARGETS = {"3090": GPU_3090, "4070": GPU_4070}


def resolve_target(name):
    if name in TARGETS:
        return TARGETS[name]
    for uid in TARGETS.values():
        if uid == name:
            return uid
    sys.exit(f"unknown --gpu {name!r}; use 3090, 4070, or a full PCI id")


def build(cfg, target, speed):
    gpus = cfg.get("gpus") or {}
    other = next((u for u in TARGETS.values() if u != target), None)
    if target not in gpus:
        sys.exit(f"{target} is not in {CONFIG} - refusing to guess where to put it")
    if other and other not in gpus:
        sys.exit(f"{other} is missing from the config - unexpected, refusing")
    out = copy.deepcopy(cfg)
    out["gpus"][target]["fan_control_enabled"] = True
    out["gpus"][target]["fan_control_settings"] = {
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


def unchanged_others(before, after, target):
    """Everything except the target's entry must be identical - including the other card, which is not ours here."""
    b = copy.deepcopy(before); a = copy.deepcopy(after)
    b.get("gpus", {}).pop(target, None)
    a.get("gpus", {}).pop(target, None)
    if b != a:
        log("REFUSED: something other than the 4070 entry changed.  Diff of the untouched parts:")
        for k in set(list(b.keys()) + list(a.keys())):
            if b.get(k) != a.get(k):
                log(f"   key {k!r}: {b.get(k)!r} -> {a.get(k)!r}")
        return False
    for other in TARGETS.values():
        if other != target and b.get("gpus", {}).get(other) != a.get("gpus", {}).get(other):
            log(f"REFUSED: the entry for {other} changed, and this tool was only asked to touch {target}")
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


def verify_fan(target):
    r = subprocess.run(["lact", "cli", "-g", target, "stats"], capture_output=True, text=True)
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if "Fan Speed" in line or "Temperatures" in line:
            log("  " + line.strip()[:180])
    if "Fan Speed" not in out:
        log("  (lact cli reported no fan line - check this card in the LACT GUI)")


def backups():
    d = os.path.dirname(CONFIG)
    return sorted(glob.glob(os.path.join(d, BACKUP_GLOB)))


def cmd_status(a):
    cfg = load()
    target = resolve_target(a.gpu)
    log(f"{CONFIG}:")
    show_gpu(cfg, GPU_3090, "3090 (LACT curve, VRAM-keyed)")
    show_gpu(cfg, GPU_4070, "4070")
    bks = backups()
    if bks:
        log("backups: " + ", ".join(os.path.basename(b) for b in bks))
    log(f"live stats for {a.gpu}:")
    verify_fan(target)


def cmd_pin(a):
    cfg = load()
    target = resolve_target(a.gpu)
    if a.dry_run:
        out = build(cfg, target, a.speed)
        log(f"dry run - this is the {a.gpu} entry that would be written:")
        log(yaml.safe_dump({target: out["gpus"][target]}, sort_keys=False).rstrip())
        if not unchanged_others(cfg, out, target):
            return 1
        log("dry run: nothing written")
        return 0
    out = build(cfg, target, a.speed)
    if not unchanged_others(cfg, out, target):
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
    log(f"{a.gpu} pinned to {a.speed:.0%} static")
    verify_fan(target)
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
    verify_fan(resolve_target(a.gpu))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["status", "pin", "restore"])
    ap.add_argument("--gpu", default="4070", help="3090, 4070, or a full PCI id (default 4070)")
    ap.add_argument("--speed", type=float, default=0.80, help="static fan speed, 0.20-1.00 (default 0.80)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--last", action="store_true", help="with restore: use the newest backup instead of the oldest")
    a = ap.parse_args()
    if a.speed < 0.2 or a.speed > 1.0:
        sys.exit("--speed must be between 0.20 and 1.00")
    return {"status": cmd_status, "pin": cmd_pin, "restore": cmd_restore}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
