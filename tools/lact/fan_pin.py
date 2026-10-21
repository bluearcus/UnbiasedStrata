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
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/fan_pin.py curve --gpu 3090 --preset moderate --scope active
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

# Fan curves in EDGE-temperature units, because on 30-series the BIOS fan API accepts no other curve source; VRAM and
# per-chip sensors are readable but not selectable.  Measured anchors: idle 26-29 C, desktop 40-45 C, sustained decode
# 49-57 C, and edge SATURATES at 56-57 C under load - so the only band a curve can act in is ~45-58 C.
CURVES = {
    # what is live now, written in VRAM units and therefore never reaching its upper half
    "now": {40: 0.30, 50: 0.40, 60: 0.55, 70: 0.75, 80: 1.00},
    # same 30% floor so idling stays silent, ~78% at the 57 C plateau
    "moderate": {40: 0.30, 45: 0.35, 48: 0.45, 51: 0.55, 54: 0.65, 57: 0.78, 60: 1.00},
    # summer setting: ~88% at the plateau, still flat at the floor below 40 C
    "summer": {40: 0.30, 45: 0.38, 48: 0.50, 51: 0.62, 54: 0.75, 57: 0.88, 60: 1.00},
}
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


def build(cfg, target, speed, labels):
    out = copy.deepcopy(cfg)
    for label in labels:
        g = get_scope_container(out, target, label)
        g["fan_control_enabled"] = True
        g["fan_control_settings"] = {
            "mode": "static",
            "static_speed": speed,
            "temperature_key": "edge",
            "interval_ms": 1000,
            "curve": {40: speed, 90: speed},
            "spindown_delay_ms": 8000,
            "change_threshold": 2,
            "auto_threshold": 0,
        }
    return out


def unchanged_others(before, after, target):
    """Everything except the target's entries must be identical - including the other card, which is not ours here."""
    b = copy.deepcopy(before); a = copy.deepcopy(after)
    b.get("gpus", {}).pop(target, None)
    a.get("gpus", {}).pop(target, None)
    for prof in (b.get("profiles") or {}).values():
        (prof.get("gpus") or {}).pop(target, None)
    for prof in (a.get("profiles") or {}).values():
        (prof.get("gpus") or {}).pop(target, None)
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


def scopes(cfg, target):
    """Every place in the config that can hold fan settings for this GPU: the base entry, plus each profile."""
    out = []
    base = (cfg.get("gpus") or {}).get(target)
    if base is not None:
        out.append(("base", base))
    for name, prof in (cfg.get("profiles") or {}).items():
        g = ((prof or {}).get("gpus") or {}).get(target)
        if g is not None:
            out.append((f"profile:{name}", g))
    return out


def effective_scope(cfg, target):
    """Which scope the daemon actually reads: the active profile if it carries this GPU, else the base entry."""
    active = cfg.get("current_profile")
    if active:
        g = (((cfg.get("profiles") or {}).get(active) or {}).get("gpus") or {}).get(target)
        if g is not None:
            return f"profile:{active}", g
    base = (cfg.get("gpus") or {}).get(target)
    return ("base", base) if base is not None else (None, None)


def pick_scopes(cfg, target, choice):
    """Resolve --scope into the set of scope labels to write."""
    all_labels = [lbl for lbl, _ in scopes(cfg, target)]
    if choice in ("active", "auto"):
        lbl, _ = effective_scope(cfg, target)
        if lbl is None:
            sys.exit(f"{target} has no fan settings anywhere in {CONFIG}")
        return [lbl]
    if choice == "all":
        return all_labels
    if choice == "base":
        return ["base"]
    if choice.startswith("profile:"):
        if choice not in all_labels:
            sys.exit(f"no fan settings for {target} in {choice}; available: {', '.join(all_labels)}")
        return [choice]
    sys.exit(f"unknown --scope {choice!r}")


def get_scope_container(cfg, target, label):
    if label == "base":
        return (cfg.get("gpus") or {})[target]
    name = label.split(":", 1)[1]
    return (cfg["profiles"][name].setdefault("gpus", {}))[target]


def curve_at(pts, t):
    """Evaluate a sparse curve at temperature t, clamped at both ends."""
    pts = sorted(pts.items()) if isinstance(pts, dict) else pts
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t <= t1:
            return v0 + (t - t0) / (t1 - t0) * (v1 - v0)
    return pts[-1][1]


def build_curve(cfg, target, preset, labels):
    out = copy.deepcopy(cfg)
    for label in labels:
        g = get_scope_container(out, target, label)
        g["fan_control_enabled"] = True
        fcs = g.setdefault("fan_control_settings", {})
        fcs["mode"] = "curve"
        fcs["temperature_key"] = "edge"
        fcs.setdefault("interval_ms", 1000)
        fcs.setdefault("change_threshold", 2)
        fcs.setdefault("auto_threshold", 0)
        fcs.setdefault("spindown_delay_ms", 8000)
        fcs["curve"] = dict(CURVES[preset])
    return out


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
    eff_label, _ = effective_scope(cfg, target)
    log(f"current_profile={cfg.get('current_profile')!r}  scopes in effect for {a.gpu}: {eff_label}")
    for lbl, g in scopes(cfg, target):
        f = g.get("fan_control_settings") or {}
        log(f"  {'*' if lbl == eff_label else ' '} {lbl:22s} key={f.get('temperature_key')!r:8s} {f.get('curve')}")
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
    labels = pick_scopes(cfg, target, a.scope)
    out = build(cfg, target, a.speed, labels)
    if a.dry_run:
        log(f"dry run - writing to {', '.join(labels)}; this is the {a.gpu} entry that would be written:")
        for label in labels:
            log(yaml.safe_dump({label: get_scope_container(out, target, label)}, sort_keys=False).rstrip())
        if not unchanged_others(cfg, out, target):
            return 1
        log("dry run: nothing written")
        return 0
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
    log(f"{a.gpu} pinned to {a.speed:.0%} static in {', '.join(labels)}")
    verify_fan(target)
    log("\nundo:")
    log(f"  sudo python3 {os.path.abspath(__file__)} restore")
    return 0


def cmd_curve(a):
    cfg = load()
    target = resolve_target(a.gpu)
    eff_label, eff = effective_scope(cfg, target)
    log(f"scopes holding fan settings for {a.gpu}:")
    for lbl, g in scopes(cfg, target):
        f = g.get("fan_control_settings") or {}
        mark = "  <-- IN EFFECT" if lbl == eff_label else ""
        log(f"  {lbl:22s} key={f.get('temperature_key')!r:8s} curve={f.get('curve')}{mark}")
    labels = pick_scopes(cfg, target, a.scope)
    out = build_curve(cfg, target, a.preset, labels)
    log(f"writing to: {', '.join(labels)}")
    cur = (eff or {}).get("fan_control_settings") or {}
    log(f"  -> preset {a.preset!r}: {CURVES[a.preset]} key='edge'")
    # show what actually changes at the temperatures this card really occupies
    for t in (27, 43, 49, 53, 57):
        before = curve_at(cur.get("curve") or {40: 0.3}, t) * 100
        after = curve_at(CURVES[a.preset], t) * 100
        log(f"   edge {t:2d}C: {before:5.0f}% -> {after:5.0f}%")
    if a.dry_run:
        if not unchanged_others(cfg, out, target):
            return 1
        log("dry run: nothing written")
        return 0
    if not unchanged_others(cfg, out, target):
        return 1
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{CONFIG}.curvebak-{stamp}"
    original = open(CONFIG, encoding="utf-8").read()
    open(bak, "w", encoding="utf-8").write(original)
    os.chmod(bak, 0o600)
    yaml.safe_load(open(bak, encoding="utf-8"))
    log(f"backup written and validated: {bak}")
    try:
        open(CONFIG, "w", encoding="utf-8").write(yaml.safe_dump(out, sort_keys=False))
        yaml.safe_load(open(CONFIG, encoding="utf-8"))
    except Exception as e:
        log(f"write failed ({e}); restoring")
        open(CONFIG, "w", encoding="utf-8").write(original)
        return 1
    if not restart():
        log("daemon will not start with the new curve - restoring the backup")
        open(CONFIG, "w", encoding="utf-8").write(original)
        restart()
        return 1
    log(f"{a.gpu} curve set to preset {a.preset!r}, key=edge")
    verify_fan(target)
    log("\nverify it is really edge-keyed and responding, under load:")
    log(f"  python3 {os.path.dirname(os.path.abspath(__file__))}/which_sensor.py --gpu {a.gpu} --seconds 120")
    return 0


def cmd_restore(a):
    bks = backups()
    if not bks:
        sys.exit(f"no backups found matching {os.path.dirname(CONFIG)}/{BACKUP_GLOB}")
    if a.from_path:
        # accept a bare backup name ("curvebak-2026...") as well as a full path, so the command stays short enough to
        # paste without the shell wrapping it mid-option
        src = a.from_path
        if "/" not in src:
            src = os.path.join(os.path.dirname(CONFIG), src)
    elif a.last:
        src = bks[-1]
    elif a.scope != "full":
        src = bks[-1]          # scoped restores want the most recent snapshot by default
    else:
        src = bks[0]
    if not os.path.exists(src):
        avail = "\n  ".join(os.path.basename(b) for b in bks) or "(none)"
        sys.exit(f"no such backup: {src}\navailable:\n  {avail}")
    snap = yaml.safe_load(open(src, encoding="utf-8"))

    if a.scope == "full":
        data = open(src, encoding="utf-8").read()
        if a.dry_run:
            log(f"dry run: would restore {CONFIG} byte-for-byte from {src}")
            return 0
        open(CONFIG, "w", encoding="utf-8").write(data)
        os.chmod(CONFIG, 0o644)
        log(f"restored {CONFIG} byte-for-byte from {src}")
        restart()
        verify_fan(resolve_target(a.gpu))
        return 0

    # scoped restore: take only this GPU's entry, only in the named scope, out of the snapshot
    live = load()
    target = resolve_target(a.gpu)
    labels = pick_scopes(live, target, a.scope)
    out = copy.deepcopy(live)
    for label in labels:
        want = None
        if label == "base":
            want = (snap.get("gpus") or {}).get(target)
        else:
            name = label.split(":", 1)[1]
            want = ((((snap.get("profiles") or {}).get(name) or {}).get("gpus")) or {}).get(target)
        have = get_scope_container(live, target, label)
        fw = (want or {}).get("fan_control_settings") or {}
        fh = (have or {}).get("fan_control_settings") or {}
        log(f"  {label}: key {fh.get('temperature_key')!r} -> {fw.get('temperature_key')!r}, "
            f"curve {fh.get('curve')} -> {fw.get('curve')}")
        if want is None:
            log(f"  {label}: not present in {os.path.basename(src)} - leaving it alone")
            continue
        if label == "base":
            out["gpus"][target] = copy.deepcopy(want)
        else:
            name = label.split(":", 1)[1]
            out["profiles"][name].setdefault("gpus", {})[target] = copy.deepcopy(want)
    if a.dry_run:
        log(f"dry run: nothing written (snapshot {os.path.basename(src)})")
        return 0
    if not unchanged_others(live, out, target):
        return 1
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{CONFIG}.preRestore-{stamp}"
    original = open(CONFIG, encoding="utf-8").read()
    open(bak, "w", encoding="utf-8").write(original)
    os.chmod(bak, 0o600)
    log(f"backup written and validated: {bak}")
    try:
        open(CONFIG, "w", encoding="utf-8").write(yaml.safe_dump(out, sort_keys=False))
        yaml.safe_load(open(CONFIG, encoding="utf-8"))
    except Exception as e:
        log(f"write failed ({e}); restoring")
        open(CONFIG, "w", encoding="utf-8").write(original)
        return 1
    if not restart():
        log("daemon will not start - restoring the pre-restore snapshot")
        open(CONFIG, "w", encoding="utf-8").write(original)
        restart()
        return 1
    log(f"scope {', '.join(labels)} restored from {os.path.basename(src)}")
    verify_fan(target)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["status", "pin", "curve", "restore"])
    ap.add_argument("--preset", default="moderate", choices=sorted(CURVES),
                    help="with curve: which edge-unit preset to install")
    ap.add_argument("--scope", default=None,
                    help="curve: active (default) / base / profile:<name> / all.  "
                         "restore: full (default, whole file) / active / base / profile:<name>")
    ap.add_argument("--gpu", default="4070", help="3090, 4070, or a full PCI id (default 4070)")
    ap.add_argument("--speed", type=float, default=0.80, help="static fan speed, 0.20-1.00 (default 0.80)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--last", action="store_true", help="with restore: use the newest backup instead of the oldest")
    ap.add_argument("--from", dest="from_path", metavar="FILE",
                    help="with restore: use this backup file instead of picking one")
    a = ap.parse_args()
    if a.scope is None:
        a.scope = "full" if a.command == "restore" else "active"
    if a.speed < 0.2 or a.speed > 1.0:
        sys.exit("--speed must be between 0.20 and 1.00")
    return {"status": cmd_status, "pin": cmd_pin, "curve": cmd_curve, "restore": cmd_restore}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
