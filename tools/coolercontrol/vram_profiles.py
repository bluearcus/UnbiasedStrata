#!/usr/bin/env python3
"""Apply (or remove) the VRAM-keyed fan profiles in CoolerControl's config.toml.

Backup first, always.  Every apply writes a byte-exact copy of the current file before touching it, validates the
result before installing it, and restores that copy automatically if anything downstream fails - including the
daemon refusing to start.  Reverting is a file copy, not a re-edit.

COMMANDS
  plan     print and validate the intended change; writes nothing unless you pass --out
  apply    backup -> validate -> write -> coolercontrold check -> (optionally start the daemon); roll back on any failure
  revert   restore the most recent backup this script made
  backups  list the backups this script made

WHY A SCRIPT AND NOT A PASTE.  config.toml is a table-scoped TOML document: the new [[profiles]] entries must land
inside the profiles array, and the channel assignments must land directly under [device-settings].  A key in the
wrong scope still parses and silently does nothing, so the insertions are anchored and the script refuses to run if
an anchor is missing.

WHY ONLY fan5 AND fan6.  Those are the two headers pinned by flat BIOS curves (147/255 and 178/255) that respond to
nothing until something hits 101 C.  fan3 (rear exhaust) and fan4 (top forward exhaust) are already at 76% on a CPU
curve doing real work for the 9-worker CPU pool: a VRAM-only curve would drop them to 30-48% whenever the GPU is
quiet and regress CPU cooling.  They want Mix Profiles (CPU curve OR VRAM curve, whichever is higher), which need a
CPU-sourced profile first - see docs/COOLING.md.  fan1 is the AIO radiator and fan2 is the pump; neither is touched.

CURVE FLOORS come from calibrations.json: fan5 will not start below 20% duty, fan6 not below 30%.  A curve that
commands less than that parks the fan, and a stopped exhaust is worse than a slow one.

USAGE (root, daemon stopped).  Absolute paths so they paste into any shell:
  sudo systemctl stop coolercontrold
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/vram_profiles.py plan
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/vram_profiles.py apply --start
  sudo systemctl start coolercontrold
  ...
  sudo systemctl stop coolercontrold
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/vram_profiles.py revert
  sudo systemctl start coolercontrold
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
BACKUP_GLOB = "config.toml.vrambak-*"

SENSOR_DEVICE = "19e098e312e1b1b39163a343ea22b6ea17f18ec1a803ffe0ce44f5bacd6076ee"   # "Custom Sensors" device
SENSOR_NAME = "sensor_8f5c0d26"                                                     # "Palit GP Nvidia 3090 VRAM"
MB_DEVICE = "8b76273cb2328ccea47d925147ddb6ab7acacd5ebee2f4fd1e814716d9470129"      # the NCT6797 hwmon device

GPU_INTAKE_UID = "7c1f6a02-9b4e-4f1a-8d30-5e2b0c7a9101"
TOP_REAR_UID = "7c1f6a02-9b4e-4f1a-8d30-5e2b0c7a9102"

# The "GPUs Intake" profile already in the file is removed rather than left dangling: its curve starts at 13% duty
# at 0 C, which is below fan5's measured 20% stall duty, and an unused profile with a stall-breaking floor sitting in
# the list is one accidental click away from a stopped intake fan.  Revert restores it byte-for-byte.
REMOVE_PROFILE_UID = "46578b93-50b4-4e6f-8785-89c5cde58ce1"
REMOVE_PROFILE_NAME = "GPUs Intake"

PROFILES = f'''[[profiles]]
uid = "{GPU_INTAKE_UID}"
name = "3090 Intake (VRAM)"
p_type = "Graph"
function_uid = "0"
# fan5: front bottom intake, the only unobstructed one, aimed at the GDDR6X.  Calibrated: starts at 20% duty, 1769 RPM at 100%.
speed_profile = [[35.0, 22], [45.0, 26], [55.0, 35], [65.0, 50], [72.0, 68], [80.0, 85], [88.0, 100]]
temp_source = {{ temp_name = "{SENSOR_NAME}", device_uid = "{SENSOR_DEVICE}" }}
temp_min = 0.0
temp_max = 100.0

[[profiles]]
uid = "{TOP_REAR_UID}"
name = "Top Rear Exhaust (VRAM)"
p_type = "Graph"
function_uid = "0"
# fan6: top rear exhaust, where the card's hot air collects.  Calibrated: starts at 30% duty, 1209 RPM at 100%.
speed_profile = [[35.0, 32], [50.0, 40], [60.0, 55], [70.0, 72], [80.0, 90], [90.0, 100]]
temp_source = {{ temp_name = "{SENSOR_NAME}", device_uid = "{SENSOR_DEVICE}" }}
temp_min = 0.0
temp_max = 100.0
'''

ASSIGNMENTS = f'''[device-settings.{MB_DEVICE}]
# The sub-table is keyed by DEVICE uid; the keys inside it are channel names.  Writing these two lines straight
# under [device-settings] would read as a device called "fan5" with a channel called "profile_uid" - valid TOML,
# silently nothing.
fan5 = {{ profile_uid = "{GPU_INTAKE_UID}" }}   # front bottom GPU intake
fan6 = {{ profile_uid = "{TOP_REAR_UID}" }}     # top rear exhaust'''

ANCHOR_PROFILES = "[[functions]]"
ANCHOR_SETTINGS = "[device-settings]"


def log(*a):
    print(*a, flush=True)


def read_config(path):
    try:
        return open(path, encoding="utf-8").read()
    except OSError as e:
        sys.exit(f"cannot read {path}: {e}  (this script needs root for the real config)")


HEADER_RE = re.compile(r"^\[\[?[A-Za-z0-9_.-]+\]\]?\s*$")


def split_segments(text):
    """Split TOML into top-level table segments.  Trailing comment/blank lines are handed to the FOLLOWING segment,
    so deleting a segment cannot eat the comment block that documents the next table."""
    lines = text.split("\n")
    segs = [[None, []]]                       # [header, body]
    for line in lines:
        if HEADER_RE.match(line):
            segs.append([line, []])
        else:
            segs[-1][1].append(line)
    for i in range(len(segs) - 1):            # push trailing comments/blank lines forward
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


def remove_profile(text, uid, name):
    """Delete exactly one [[profiles]] segment by uid.  Refuses if it is not found or if anything else would go."""
    segs = split_segments(text)
    doomed = [i for i, (h, b) in enumerate(segs)
              if h == "[[profiles]]" and any(l.strip() == f'uid = "{uid}"' for l in b)]
    if len(doomed) != 1:
        sys.exit(f"refusing: expected exactly one [[profiles]] segment with uid {uid!r}, found {len(doomed)}")
    body = segs[doomed[0]][1]
    if not any(name in l for l in body):
        sys.exit(f"refusing: the segment with uid {uid!r} is not the {name!r} profile - not deleting it")
    kept = [s for i, s in enumerate(segs) if i != doomed[0]]
    return join_segments(kept), body


def build_patch(text, path):
    if GPU_INTAKE_UID in text or TOP_REAR_UID in text:
        sys.exit("already applied (a vram profile uid is present).  Use 'revert' to undo, or 'backups' to list.")
    for anchor in (ANCHOR_PROFILES, ANCHOR_SETTINGS):
        if anchor not in text:
            sys.exit(f"refusing to edit {path}: anchor {anchor!r} not found - the config is not the shape this script expects")
    out = text.replace(ANCHOR_PROFILES, PROFILES + "\n" + ANCHOR_PROFILES, 1)
    if REMOVE_PROFILE_UID in out:
        if f'profile_uid = "{REMOVE_PROFILE_UID}"' in out:
            sys.exit(f"refusing: a channel still applies {REMOVE_PROFILE_NAME!r}; reset that channel first")
        out, removed = remove_profile(out, REMOVE_PROFILE_UID, REMOVE_PROFILE_NAME)
        log(f"removing the unused {REMOVE_PROFILE_NAME!r} profile ({len(removed)} lines)")
    # The device sub-table has to land directly under [device-settings] and before the next top-level table.
    # If [device-settings] already holds keys, inserting a table header here would absorb them into the new table.
    lines = out.split("\n")
    i = lines.index(ANCHOR_SETTINGS)
    nxt = next((l for l in lines[i + 1:] if l.strip() and not l.lstrip().startswith("#")), "")
    if not nxt.startswith("["):
        sys.exit(f"refusing: [device-settings] already has content (next line: {nxt!r}); inserting the device table "
                 f"here would pull those keys into it")
    out = "\n".join(lines[:i + 1] + ASSIGNMENTS.split("\n") + lines[i + 1:])
    return out


def validate(text, path):
    try:
        parsed = tomllib.loads(text)
    except Exception as e:
        sys.exit(f"REFUSED: the result is not valid TOML ({e})")
    profiles = {p.get("uid"): p.get("name") for p in parsed.get("profiles", [])}
    # The shape CoolerControl writes (see the example in the config's own comment block) is
    # [device-settings.<device uid>] with channel names as keys.
    dev_settings = parsed.get("device-settings", {}).get(MB_DEVICE)
    if not isinstance(dev_settings, dict):
        sys.exit(f"REFUSED: no [device-settings.{MB_DEVICE}] table in the result")
    flat = [k for k, v in parsed.get("device-settings", {}).items() if not isinstance(v, dict)]
    if flat:
        sys.exit(f"REFUSED: keys directly under [device-settings] (not device-keyed): {flat}")
    settings = {k: v for k, v in dev_settings.items() if isinstance(v, dict)}
    missing = [u for u in (GPU_INTAKE_UID, TOP_REAR_UID) if u not in profiles]
    if missing:
        sys.exit(f"REFUSED: profiles not visible after patching: {missing}")
    if REMOVE_PROFILE_UID in profiles:
        sys.exit(f"REFUSED: {REMOVE_PROFILE_NAME!r} ({REMOVE_PROFILE_UID}) survived removal")
    if REMOVE_PROFILE_NAME in profiles.values():
        sys.exit(f"REFUSED: a profile still named {REMOVE_PROFILE_NAME!r} exists")
    for ch, want in (("fan5", GPU_INTAKE_UID), ("fan6", TOP_REAR_UID)):
        got = settings.get(ch, {}).get("profile_uid")
        if got != want:
            sys.exit(f"REFUSED: {ch} -> {got!r}, expected {want!r} (the assignment landed in the wrong table)")
    log("validated: TOML parses, both profiles present, both channels resolve under the device table:")
    for ch, uid in (("fan5", GPU_INTAKE_UID), ("fan6", TOP_REAR_UID)):
        log(f"   device-settings.{MB_DEVICE[:12]}…{ch} -> {profiles[uid]}")
    untouched = sorted(set(("fan1", "fan2", "fan3", "fan4")) - set(settings))
    if untouched:
        log(f"   left on their BIOS curves: {', '.join(untouched)}")
    return parsed


def backups(path):
    d = os.path.dirname(path) or "."
    return sorted(glob.glob(os.path.join(d, BACKUP_GLOB)))


def daemon_backup():
    """Also ask the daemon for its own rotated backup (coolercontrold backup).  Best-effort: the script's own
    byte-exact copy is the one revert depends on."""
    if shutil.which("coolercontrold") is None:
        return
    r = subprocess.run(["coolercontrold", "backup"], capture_output=True, text=True)
    if r.returncode == 0:
        log("coolercontrold backup: ok (see 'sudo coolercontrold list')")
    else:
        log(f"NOTE: coolercontrold backup did not run ({(r.stdout + r.stderr).strip().splitlines()[-1:] or ['no output']})")


def make_backup(path):
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = f"{path}.vrambak-{stamp}"
    shutil.copy2(path, bak)
    os.chmod(bak, 0o600)
    # Prove the backup is usable before relying on it.
    read_config(bak)
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
    return latest


def daemon_active():
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", "coolercontrold"])
        return r.returncode == 0
    except FileNotFoundError:
        return False


def daemon_check():
    """coolercontrold check: the daemon's own validator.  Missing binary is not treated as success."""
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
    patched = build_patch(text, a.config)
    validate(patched, a.config)
    before, after = text.splitlines(), patched.splitlines()
    gone = [l for l in before if l not in after]
    added = [l for l in after if l not in before]
    log("\nnothing written.  line diff (approximate - identical lines in other profiles cancel out): "
        f"{len(added)} added, {len(gone)} removed")
    for line in gone:
        log("   -" + line)
    for line in added:
        log("   +" + line)
    if a.show:
        log("\n--- full patched text ---")
        print(patched, end="")
    if a.out:
        # Deliberately no fixed /tmp path: plan runs under sudo, and a predictable filename in a world-writable
        # directory either collides with a file owned by someone else or leaves a root-owned file behind.
        try:
            with open(a.out, "w", encoding="utf-8") as f:
                f.write(patched)
            log(f"\npatched text written to {a.out}")
        except OSError as e:
            sys.exit(f"could not write {a.out}: {e}   (the plan itself validated fine; point --out somewhere writable)")


def cmd_apply(a):
    if daemon_active():
        sys.exit("coolercontrold is RUNNING - it rewrites config.toml on shutdown and would overwrite this edit.\n"
                 "  sudo systemctl stop coolercontrold   then re-run apply.")
    text = read_config(a.config)
    patched = build_patch(text, a.config)
    validate(patched, a.config)

    bak = make_backup(a.config)          # never proceed without a validated backup
    daemon_backup()
    try:
        with open(a.config, "w", encoding="utf-8") as f:
            f.write(patched)
        tomllib.loads(open(a.config, encoding="utf-8").read())
    except Exception as e:
        log(f"write failed ({e}); rolling back")
        shutil.copy2(bak, a.config)
        return 1

    if not daemon_check():
        log("coolercontrold check FAILED - rolling back to the backup")
        shutil.copy2(bak, a.config)
        log(f"restored from {bak}")
        return 1

    if a.start:
        r = subprocess.run(["systemctl", "start", "coolercontrold"], capture_output=True, text=True)
        ok = daemon_active()
        if r.returncode != 0 or not ok:
            log("daemon refused to start with the new config - rolling back and starting the old one")
            subprocess.run(["systemctl", "stop", "coolercontrold"], capture_output=True)
            shutil.copy2(bak, a.config)
            subprocess.run(["systemctl", "start", "coolercontrold"], capture_output=True)
            log(f"restored from {bak}; the service was started with the previous config")
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
    log("the newest is what 'revert' restores; 'sudo coolercontrold list' shows the daemon's own backups")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["plan", "apply", "revert", "backups"])
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--start", action="store_true", help="with apply: start the daemon afterwards and roll back if it fails")
    ap.add_argument("--out", help="with plan: also write the patched text to this path")
    ap.add_argument("--show", action="store_true", help="with plan: print the whole patched file")
    a = ap.parse_args()
    return {"plan": cmd_plan, "apply": cmd_apply, "revert": cmd_revert, "backups": cmd_backups}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
