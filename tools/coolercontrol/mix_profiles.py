#!/usr/bin/env python3
"""Add Mix Profiles (CPU curve OR VRAM curve, whichever is higher) to the case fans.

WHERE THIS SITS IN THE PLAN.  The VRAM-only patch (vram_profiles.py) is already applied: fan5 and fan6 follow the
3090's memory temperature, fan3/fan4 are still on BIOS CPU curves, fan1 is still on its BIOS CPU curve.  A VRAM-only
curve is wrong for a shared case fan: at CPU-only work the engine is idle, the GPU is cool, and a VRAM-only curve
lets an exhaust fan drop to 50% while nine CPU workers are cooking - fan6's old BIOS pin was 70%.  A Mix Profile
computes both curves independently and takes the higher duty, so a fan never has to choose which component it cares
about.

ORDER MATTERS.  Do not run this before the CPU-sourced Graph profile exists in the GUI (open item O4), because
temp_source.temp_name is CoolerControl's internal sensor id and only the daemon knows it.  This script DISCOVERS that
profile rather than hard-coding it: it looks for a Graph profile whose temperature source is not the VRAM File sensor.
If it cannot find exactly one, it stops and tells you what it saw.

WHAT IT CHANGES
  fan3  rear exhaust        -> Mix (CPU or VRAM exhaust curve)   was: BIOS CPU curve
  fan4  top forward exhaust -> Mix (CPU or VRAM exhaust curve)   was: BIOS CPU curve
  fan6  top rear exhaust    -> Mix (CPU or VRAM exhaust curve)   was: VRAM-only profile
WHAT IT LEAVES ALONE
  fan5  GPU intake          stays VRAM-only: its job is the GDDR6X, and it is the only unobstructed intake
  fan1  AIO radiator        stays on its BIOS curve until it is calibrated (it was never calibrated; stalling two
                            AIO fans on an unknown duty floor is not a risk worth taking for a marginal gain)
  fan2  AIO pump            never touched

SAFETY.  Same contract as vram_profiles.py: refuses to run while coolercontrold is active; byte-exact validated
backup before any write; the patched text is re-parsed and every assignment is checked to resolve to the intended
profile under [device-settings.<device uid>]; `coolercontrold check` failure rolls back; --start rolls back if the
daemon will not boot the new config; revert restores the backup byte-for-byte.  Existing keys inside the device
table are REPLACED, never duplicated - a duplicate key is invalid TOML and would take the config offline.

USAGE (absolute paths; root, daemon stopped)
  sudo systemctl stop coolercontrold
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/mix_profiles.py plan
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/mix_profiles.py apply --start
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/mix_profiles.py revert
"""
import argparse
import datetime as dt
import glob
import os
import re
import shutil
import subprocess
import sys
import tomllib

CONFIG = "/etc/coolercontrol/config.toml"
BACKUP_GLOB = "config.toml.mixbak-*"

MB_DEVICE = "8b76273cb2328ccea47d925147ddb6ab7acacd5ebee2f4fd1e814716d9470129"   # NCT6797 hwmon
VRAM_SENSOR_DEVICE = "19e098e312e1b1b39163a343ea22b6ea17f18ec1a803ffe0ce44f5bacd6076ee"   # "Custom Sensors" pseudo-device

# From vram_profiles.py: the VRAM-only exhaust curve is the right VRAM member for a shared case fan.
VRAM_EXHAUST_UID = "7c1f6a02-9b4e-4f1a-8d30-5e2b0c7a9102"
MIX_UID = "9d3a5b14-2c8f-4a60-b7e1-6f0c9a2d4e03"
MIX_NAME = "Case Fans (CPU or VRAM)"

TARGETS = {
    "fan3": "rear exhaust",
    "fan4": "top forward exhaust",
    "fan6": "top rear exhaust",
}

HEADER_RE = re.compile(r"^\[\[?[A-Za-z0-9_.-]+\]\]?\s*$")
DEV_HEADER = f"[device-settings.{MB_DEVICE}]"


def log(*a):
    print(*a, flush=True)


def read_config(path):
    try:
        return open(path, encoding="utf-8").read()
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}  (this script needs root for the real config)")


def split_segments(text):
    """Split into top-level table segments, handing trailing comment/blank lines to the FOLLOWING segment."""
    segs = [[None, []]]
    for line in text.split("\n"):
        if HEADER_RE.match(line):
            segs.append([line, []])
        else:
            segs[-1][1].append(line)
    for i in range(len(segs) - 1):
        body = segs[i][1]
        keep = len(body)
        while keep > 0 and (not body[keep - 1].strip() or body[keep - 1].lstrip().startswith("#")):
            keep -= 1
        if keep < len(body):
            segs[i + 1][1] = body[keep:] + segs[i + 1][1]
            segs[i][1] = body[:keep]
    return segs


def join_segments(segs):
    out = []
    for header, body in segs:
        if header is not None:
            out.append(header)
        out.extend(body)
    return "\n".join(out)


def find_cpu_profile(parsed, text):
    """The profile the GUI created: a Graph profile with a temperature source that is not the VRAM File sensor."""
    cands = []
    for p in parsed.get("profiles", []):
        if p.get("p_type") != "Graph":
            continue
        ts = p.get("temp_source") or {}
        uid = p.get("uid")
        if uid in (VRAM_EXHAUST_UID, "7c1f6a02-9b4e-4f1a-8d30-5e2b0c7a9101"):
            continue
        if ts.get("device_uid") == VRAM_SENSOR_DEVICE:
            continue
        if not ts:
            continue
        cands.append(p)
    if not cands:
        sys.exit("no CPU-sourced Graph profile found.  Create it in the GUI first (open item O4): Profiles -> add -> "
                 "type Graph -> temperature source = the CPU sensor.  Nothing is hard-coded here on purpose.")
    if len(cands) > 1:
        log("ambiguous - more than one non-VRAM Graph profile exists, so this script will not guess:")
        for p in cands:
            log(f"   {p.get('name')!r} uid={p.get('uid')} temp_source={p.get('temp_source')}")
        sys.exit("delete or adjust the extras so exactly one CPU-sourced profile remains, then re-run.")
    return cands[0]


def build_patch(text, parsed):
    if MIX_UID in text:
        sys.exit("already applied (the mix profile uid is present).  Use 'revert' to undo.")
    cpu = find_cpu_profile(parsed, text)
    log(f"CPU profile found: {cpu.get('name')!r} uid={cpu['uid']} temp_source={cpu['temp_source']}")
    if VRAM_EXHAUST_UID not in {p.get("uid") for p in parsed.get("profiles", [])}:
        sys.exit(f"the VRAM exhaust profile {VRAM_EXHAUST_UID} is missing - run vram_profiles.py first")

    segs = split_segments(text)
    idx = next((i for i, (h, _) in enumerate(segs) if h == DEV_HEADER), None)
    if idx is None:
        sys.exit(f"no {DEV_HEADER} table in the config - run vram_profiles.py first")

    block = segs[idx][1]
    keys = [i for i, l in enumerate(block) if re.match(r"^[A-Za-z0-9_]+ = ", l)]
    insert_at = (keys[-1] + 1) if keys else 0
    new_lines = []
    for ch, note in TARGETS.items():
        existing = [i for i in keys if block[i].startswith(f"{ch} = ")]
        line = f'{ch} = {{ profile_uid = "{MIX_UID}" }}   # {note}'
        if existing:
            log(f"replacing {ch}: {block[existing[0]].split('#')[0].strip()}")
            block[existing[0]] = line
        else:
            new_lines.append((insert_at, line))
            insert_at += 1
    for offset, line in sorted(new_lines, reverse=True):
        block.insert(offset, line)

    profile = f'''[[profiles]]
uid = "{MIX_UID}"
name = "{MIX_NAME}"
p_type = "Mix"
# Each member computes its own curve from its own temperature source; the mix takes the higher duty.  A shared case
# fan should never have to decide whether it is a CPU fan or a GPU fan.
member_profile_uids = ["{cpu['uid']}", "{VRAM_EXHAUST_UID}"]
mix_function_type = "Max"
function_uid = "0"
'''
    # Put the mix profile with the other profiles: insert before the first [[profiles]] segment that follows a
    # header, i.e. keep the profiles array contiguous.
    prof_idx = [i for i, (h, _) in enumerate(segs) if h == "[[profiles]]"]
    if not prof_idx:
        sys.exit("no [[profiles]] array found")
    last = prof_idx[-1]
    segs.insert(last + 1, ["[[profiles]]", profile.split("\n")[1:]])
    return join_segments(segs), cpu


def validate(text):
    try:
        parsed = tomllib.loads(text)
    except Exception as e:
        sys.exit(f"REFUSED: the result is not valid TOML ({e})")
    profiles = {p.get("uid"): p for p in parsed.get("profiles", [])}
    mix = profiles.get(MIX_UID)
    if not mix:
        sys.exit("REFUSED: the mix profile is not in the result")
    if mix.get("p_type") != "Mix" or mix.get("mix_function_type") != "Max":
        sys.exit(f"REFUSED: mix profile malformed: {mix}")
    members = mix.get("member_profile_uids") or []
    if VRAM_EXHAUST_UID not in members or len(members) != 2:
        sys.exit(f"REFUSED: mix members wrong: {members}")
    for m in members:
        if m not in profiles:
            sys.exit(f"REFUSED: mix member {m} does not name an existing profile")
    dev = parsed.get("device-settings", {}).get(MB_DEVICE)
    if not isinstance(dev, dict):
        sys.exit("REFUSED: [device-settings.<device uid>] missing")
    counts = {}
    for p in parsed.get("profiles", []):
        counts[p.get("uid")] = counts.get(p.get("uid"), 0) + 1
    for uid, n in counts.items():
        if n > 1:
            sys.exit(f"REFUSED: duplicate profile uid {uid} ({n} copies)")
    for ch in TARGETS:
        if dev.get(ch, {}).get("profile_uid") != MIX_UID:
            sys.exit(f"REFUSED: {ch} -> {dev.get(ch)!r}, expected the mix profile")
    log("validated: TOML parses, no duplicate keys, mix profile well formed, assignments resolve:")
    for ch, note in TARGETS.items():
        log(f"   {ch} ({note}) -> {MIX_NAME}  [members: " +
            ", ".join(profiles[m].get('name', m) for m in members) + ", Max]")
    keep = {k: v for k, v in dev.items() if k not in TARGETS}
    for ch, v in keep.items():
        log(f"   {ch} left as: {v}")
    return parsed


def backups(path):
    d = os.path.dirname(path) or "."
    return sorted(glob.glob(os.path.join(d, BACKUP_GLOB)))


def make_backup(path):
    bak = f"{path}.mixbak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, bak)
    os.chmod(bak, 0o600)
    tomllib.loads(open(bak, encoding="utf-8").read())
    log(f"backup written and validated: {bak}")
    return bak


def restore(path):
    baks = backups(path)
    if not baks:
        sys.exit("no backups made by this script; use 'sudo coolercontrold restore' for the daemon's own backups")
    latest = baks[-1]
    shutil.copy2(latest, path)
    os.chmod(path, 0o644)
    tomllib.loads(open(path, encoding="utf-8").read())
    log(f"restored {path} byte-for-byte from {latest}")


def daemon_active():
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "coolercontrold"]).returncode == 0
    except FileNotFoundError:
        return False


def daemon_check():
    if shutil.which("coolercontrold") is None:
        log("NOTE: coolercontrold not found; skipping its validator")
        return True
    r = subprocess.run(["coolercontrold", "check"], capture_output=True, text=True)
    out = (r.stdout + r.stderr).strip()
    if out:
        log("coolercontrold check:\n" + "\n".join("   " + l for l in out.splitlines()[-15:]))
    return r.returncode == 0


def cmd_plan(a):
    text = read_config(a.config)
    parsed = tomllib.loads(text)
    patched, cpu = build_patch(text, parsed)
    validate(patched)
    log("\nnothing written.")
    if a.show:
        log("--- full patched text ---")
        print(patched, end="")
    if a.out:
        try:
            open(a.out, "w", encoding="utf-8").write(patched)
            log(f"patched text written to {a.out}")
        except OSError as e:
            sys.exit(f"could not write {a.out}: {e}   (the plan itself validated fine)")


def cmd_apply(a):
    if daemon_active():
        sys.exit("coolercontrold is RUNNING - it rewrites config.toml on shutdown.  sudo systemctl stop coolercontrold first.")
    text = read_config(a.config)
    try:
        parsed = tomllib.loads(text)
    except Exception as e:
        sys.exit(f"the CURRENT config does not parse ({e}) - stop and look at it before editing anything")
    patched, cpu = build_patch(text, parsed)
    validate(patched)
    bak = make_backup(a.config)
    try:
        open(a.config, "w", encoding="utf-8").write(patched)
        tomllib.loads(open(a.config, encoding="utf-8").read())
    except Exception as e:
        log(f"write failed ({e}); rolling back")
        shutil.copy2(bak, a.config)
        return 1
    if not daemon_check():
        log("coolercontrold check FAILED - rolling back to the backup")
        shutil.copy2(bak, a.config)
        return 1
    if a.start:
        subprocess.run(["systemctl", "start", "coolercontrold"], capture_output=True)
        if not daemon_active():
            log("daemon refused to start with the new config - rolling back and restarting on the old one")
            subprocess.run(["systemctl", "stop", "coolercontrold"], capture_output=True)
            shutil.copy2(bak, a.config)
            subprocess.run(["systemctl", "start", "coolercontrold"], capture_output=True)
            return 1
        log("coolercontrold started with the new config")
    log("\napplied.  To undo:")
    log(f"  sudo systemctl stop coolercontrold")
    log(f"  sudo python3 {os.path.abspath(__file__)} revert")
    log(f"  sudo systemctl start coolercontrold")
    return 0


def cmd_revert(a):
    if daemon_active():
        sys.exit("coolercontrold is RUNNING - stop it first so it does not overwrite the restore.")
    restore(a.config)


def cmd_backups(a):
    baks = backups(a.config)
    if not baks:
        log("no backups made by this script")
        return 0
    for b in baks:
        log(f"  {b}  ({os.path.getsize(b)} bytes)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["plan", "apply", "revert", "backups"])
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    return {"plan": cmd_plan, "apply": cmd_apply, "revert": cmd_revert, "backups": cmd_backups}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
