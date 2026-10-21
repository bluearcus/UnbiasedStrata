#!/usr/bin/env python3
"""Give fan1 (the AIO radiator push pair) its own profiles, and stop the VRAM curve dominating at idle.

TWO PROBLEMS THIS FIXES

1. fan1 cannot share a profile with the exhaust fans.
   Its calibration shows the radiator pair never stops: 0% duty = 641 RPM, 40% duty = 622 RPM (a dead band), then
   45% -> 749, 55% -> 958, 70% -> 1248, 100% -> 1771.  CoolerControl therefore reads a profile duty as "that fraction
   of the way between the calibrated minimum and maximum RPM", not as a fraction of the PWM register.  58% duty on
   fan1 means 641 + 0.58*(1771-641) = 1298 RPM, which needs ~72% raw PWM - measured as pwm1=181, 1258 RPM.  On fan3,
   whose calibration starts at 0 RPM, the same 58% duty means 58% raw and 817 RPM.  One profile, two very different
   fans.  fan1 gets curves written in its own units.

2. The VRAM member was winning the mix at idle.
   The 3090's memory rests warm (~48 C just being powered with a model loaded), while the CPU package rests cooler.
   A Max mix that starts ramping VRAM at 35 C therefore lets a component that is merely *warm by nature* drive the
   case fans while nothing is running.  The VRAM member's ramp moves up into the band where GPU work actually shows
   up, so at rest the CPU curve governs and under load the GPU curve does.

WHAT IT WRITES
  profile "CPU (radiator)"      Graph, CPU source, gentle - fan1's duty scale
  profile "GPU (radiator)"      Graph, 3090 VRAM source, flat until 70 C then ramping - the radiator pair does not
                                ramp for the 3090 at all below 70 C, because its fans idle at 641 RPM anyway
  profile "Radiator (CPU or VRAM)"  Mix, Max of the two  -> applied to fan1
  profile "Top Rear Exhaust (VRAM)" retuned: flat at the 32% floor below 50 C and topping out at 75% rather than
                                100% - the exhaust mix's VRAM member
  fan5 (GPU intake) is not touched; its calibration starts at 0 RPM so its duty scale is already honest.

The balance strategy here is deliberate: this case has three exhausts and one unobstructed intake, so matching duty
across them is already negative pressure.  Rather than push the restricted radiator pair harder - which is noisy for
the air it actually moves - the three exhausts are asked to do less.

The CPU temperature source is taken from the profile you created in the GUI - this script reads its temp_source
rather than guessing CoolerControl's internal sensor ids.

It also prints, for each scenario, what duty the profile asks for and what RPM that actually means on that header,
inverted through the calibration curve - so you can see the result before applying it.

USAGE (root, daemon stopped)
  sudo systemctl stop coolercontrold
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/radiator_profiles.py plan
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/radiator_profiles.py apply --start
  sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/radiator_profiles.py revert
"""
import argparse
import datetime as dt
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib

CONFIG = "/etc/coolercontrol/config.toml"
CALIBRATIONS = "/etc/coolercontrol/calibrations.json"
BACKUP_GLOB = "config.toml.radbak-*"

MB_DEVICE = "8b76273cb2328ccea47d925147ddb6ab7acacd5ebee2f4fd1e814716d9470129"
VRAM_SENSOR_DEVICE = "19e098e312e1b1b39163a343ea22b6ea17f18ec1a803ffe0ce44f5bacd6076ee"
VRAM_EXHAUST_UID = "7c1f6a02-9b4e-4f1a-8d30-5e2b0c7a9102"

CPU_RAD_UID = "b7e24c19-5a3d-4f81-9c06-2d1b7e90a341"
GPU_RAD_UID = "b7e24c19-5a3d-4f81-9c06-2d1b7e90a342"
RAD_MIX_UID = "b7e24c19-5a3d-4f81-9c06-2d1b7e90a343"

# Written in fan1's units: duty is a fraction of its 641..1771 RPM range, so these stay low.
# THE CPU HALF MUST BE ABLE TO MAX THE PAIR.  A previous version of this curve topped out at 55% at 90 C, which on
# fan1's calibration (641 RPM start, so duty is a fraction of its RPM range) is only ~958 of 1771 RPM: a CPU at 95 C
# could not spin the fans that dump its heat past half.  That is a safety defect, not a taste question, and the guard
# in check_cpu_ceiling() now refuses a curve that reintroduces it.  This one is at 100% by 75 C.
# The radiator pair's CPU curve, in two shapes.  Both reach 100% by 75 C - that part is not negotiable, and
# check_cpu_ceiling() enforces it.
#
#   steep   what shipped after the 55%-ceiling defect was fixed.  Its slope in the 50-60 C band is ~3 points/degree,
#           which was only ever needed because the input was a sensor bouncing 13 C per second.
#   gentle  a higher floor and an earlier start, so the idle mean sits on a flatter part of the curve.  ~0.9 points/
#           degree at 40-48 C.  Use it once the input is smoothed; against the raw sensor its lower slope just means
#           less amplitude on the same 3 s oscillation.
CPU_CURVES = {
    "steep":  [[40.0, 2], [45.0, 10], [50.0, 22], [55.0, 38], [60.0, 55], [65.0, 72], [70.0, 90],
               [75.0, 100], [95.0, 100]],
    "gentle": [[35.0, 15], [40.0, 15], [48.0, 22], [55.0, 34], [62.0, 52], [68.0, 72], [72.0, 88],
               [75.0, 100], [95.0, 100]],
}
CPU_RAD_PRESET = "steep"
CPU_RAD_CURVE = CPU_CURVES[CPU_RAD_PRESET]
# The radiator pair does not ramp for the 3090 at all until 70 C.  Its fans idle at 641 RPM whatever the duty, so a
# flat 2% below 70 C costs nothing and stops the memory's resting temperature spinning them.
GPU_RAD_CURVE = [[45.0, 2], [70.0, 2], [78.0, 12], [85.0, 25], [92.0, 40]]

# Exhaust member, held back rather than overegged: flat at the 32% floor (just above fan6's 30% stall duty) to 50 C,
# then a gentle climb that tops out at 75% instead of the original 100%.  Balance is achieved by asking less of the
# three exhausts, not by pushing the intake pair harder - the case has three exhausts and one unobstructed intake, so
# matching duty would already be negative pressure.
EXHAUST_CURVE_NEW = [[45.0, 32], [50.0, 32], [60.0, 45], [70.0, 56], [80.0, 66], [90.0, 75]]

CPU_RAD_NAME = "CPU (radiator)"
GPU_RAD_NAME = "GPU (radiator)"
RAD_MIX_NAME = "Radiator (CPU or VRAM)"

SCENARIOS = [("idle now", None, None), ("resting (typical)", 40.0, 48.0),
             ("GPU decoding 76C", 57.0, 76.0), ("GPU hot 84C", 60.0, 84.0), ("CPU-only pool", 72.0, 45.0)]

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


def curve_at(curve, t):
    if not curve:
        return 0
    if t <= curve[0][0]:
        return curve[0][1]
    if t >= curve[-1][0]:
        return curve[-1][1]
    for (t0, d0), (t1, d1) in zip(curve, curve[1:]):
        if t0 <= t <= t1:
            return d0 + (t - t0) * (d1 - d0) / (t1 - t0)
    return curve[-1][1]


def calibration(channel):
    try:
        data = json.load(open(CALIBRATIONS, encoding="utf-8"))
    except OSError:
        return None
    for e in data.get("calibrations", []):
        if e.get("device_uid") == MB_DEVICE and e.get("channel_name") == channel:
            up = [(p["duty"], p["rpm"]) for p in e.get("calibration", {}).get("up_curve", [])
                  if isinstance(p.get("duty"), (int, float)) and isinstance(p.get("rpm"), (int, float))]
            if not up:
                return None
            rpms = [r for _, r in up]
            return {"up": up, "min": min(rpms), "max": max(rpms)}
    return None


def duty_to_rpm(channel, duty):
    """What a profile duty actually means on this header, given its calibration."""
    c = calibration(channel)
    if not c:
        return duty, None
    rpm = c["min"] + (duty / 100.0) * (c["max"] - c["min"])
    raw = duty  # what register percentage CoolerControl would write if it did not rescale
    up = c["up"]
    for (d0, r0), (d1, r1) in zip(up, up[1:]):
        if r0 <= rpm <= r1 and d1 != d0:
            raw = d0 + (rpm - r0) * (d1 - d0) / (r1 - r0)
            break
    return raw, rpm


def find_cpu_source(profiles):
    for p in profiles.values():
        if p.get("p_type") != "Graph":
            continue
        ts = p.get("temp_source") or {}
        if ts.get("device_uid") and ts["device_uid"] != VRAM_SENSOR_DEVICE:
            return ts, p
    sys.exit("no CPU-sourced Graph profile found - create it in the GUI first")


def build_patch(text, parsed):
    profiles = {p.get("uid"): p for p in parsed.get("profiles", [])}
    if RAD_MIX_UID in profiles:
        sys.exit("already applied (the radiator mix uid is present).  Use 'revert' to undo.")
    cpu_source, cpu_profile = find_cpu_source(profiles)
    log(f"CPU source taken from {cpu_profile.get('name')!r}: {cpu_source}")
    if VRAM_EXHAUST_UID not in profiles:
        sys.exit(f"the VRAM exhaust profile {VRAM_EXHAUST_UID} is missing - run vram_profiles.py first")
    exhaust = profiles[VRAM_EXHAUST_UID]

    segs = split_segments(text)
    # retune the exhaust member's curve in place
    retuned = False
    for i, (h, body) in enumerate(segs):
        if h == "[[profiles]]" and any(l.strip() == f'uid = "{VRAM_EXHAUST_UID}"' for l in body):
            for j, l in enumerate(body):
                if l.startswith("speed_profile = "):
                    body[j] = f"speed_profile = {EXHAUST_CURVE_NEW}"
                    body.insert(j + 1, "# retuned: the 3090's memory rests warm, so this member must not start ramping at 35 C")
                    retuned = True
                    break
    if not retuned:
        sys.exit(f"could not find the speed_profile line of {VRAM_EXHAUST_UID} to retune")

    new_profiles = f'''[[profiles]]
uid = "{CPU_RAD_UID}"
name = "{CPU_RAD_NAME}"
p_type = "Graph"
# fan1's duty scale: 0% already means 641 RPM and 40% is still 622 RPM, so these values look tiny next to the
# exhaust profile.  They are not - see the predicted RPMs the plan prints.
speed_profile = {CPU_RAD_CURVE}
temp_source = {{ temp_name = "{cpu_source['temp_name']}", device_uid = "{cpu_source['device_uid']}" }}
temp_min = 0.0
temp_max = 100.0

[[profiles]]
uid = "{GPU_RAD_UID}"
name = "{GPU_RAD_NAME}"
p_type = "Graph"
# Ramps from 50 C so the memory's resting temperature does not spin the radiator fans for no reason.
speed_profile = {GPU_RAD_CURVE}
temp_source = {{ temp_name = "{exhaust['temp_source']['temp_name']}", device_uid = "{exhaust['temp_source']['device_uid']}" }}
temp_min = 0.0
temp_max = 100.0

[[profiles]]
uid = "{RAD_MIX_UID}"
name = "{RAD_MIX_NAME}"
p_type = "Mix"
member_profile_uids = ["{CPU_RAD_UID}", "{GPU_RAD_UID}"]
mix_function_type = "Max"
function_uid = "0"
'''
    prof_idx = [i for i, (h, _) in enumerate(segs) if h == "[[profiles]]"]
    segs.insert(prof_idx[-1] + 1, ["[[profiles]]", new_profiles.split("\n")[1:]])

    idx = next((i for i, (h, _) in enumerate(segs) if h == DEV_HEADER), None)
    if idx is None:
        sys.exit(f"no {DEV_HEADER} table in the config")
    block = segs[idx][1]
    keys = [i for i, l in enumerate(block) if re.match(r"^[A-Za-z0-9_]+ = ", l)]
    line = f'fan1 = {{ profile_uid = "{RAD_MIX_UID}" }}   # AIO radiator push pair, its own CPU-or-VRAM mix'
    existing = [i for i in keys if block[i].startswith("fan1 = ")]
    if existing:
        log(f"replacing fan1: {block[existing[0]].split('#')[0].strip()}")
        block[existing[0]] = line
    else:
        at = (keys[-1] + 1) if keys else 0
        block.insert(at, line)
        log("adding fan1 assignment")
    return join_segments(segs)


def check_cpu_ceiling(curve):
    """Refuse a CPU curve that cannot reach 100%, or that is not already there by a temperature a loaded 10900X reaches.

    This exists because the curve that shipped at first capped out at 55% and nothing stopped it - the fans that carry
    the CPU's heat were unable to run at full speed at any CPU temperature.
    """
    top = max(v for _, v in curve)
    if top < 100.0:
        sys.exit(f"REFUSED: the CPU radiator curve peaks at {top:.0f}%, so the pair can never run at full speed. "
                 f"It must reach 100%.")
    at75 = curve_at(curve, 75.0)
    if at75 < 100.0:
        sys.exit(f"REFUSED: the CPU curve is only {at75:.0f}% at 75 C; it must be at 100% by then (a loaded "
                 f"i9-10900X on this AIO sits above that, and the radiator fans are what move the heat out).")
    log(f"CPU ceiling check: 100% by 75 C ({curve_at(curve, 75.0):.0f}% at 75 C, {top:.0f}% maximum)")


def build_retune(text, parsed, a_preset="steep"):
    """Rewrite the member curves of an already-applied radiator mix, touching those two lines and nothing else.

    The apply path refuses to run twice, and reverting first would put fan1 back on the shared case-fan mix for the
    duration - the noisy state this design removed.

    Deliberately NOT built on split_segments(): that splitter hands trailing blank and comment lines to the *next*
    table (so a comment stays with what it describes), which means re-joining it reflows those blank lines.  Byte-exact
    elsewhere is worth more here than reusing the helper, so this finds each table by its uid and rewrites one line.
    """
    cpu_curve = CPU_CURVES[a_preset]
    check_cpu_ceiling(cpu_curve)
    profiles = {p.get("uid"): p for p in parsed.get("profiles", [])}
    if RAD_MIX_UID not in profiles:
        sys.exit("not applied yet: the radiator mix is absent.  Run 'apply' instead of 'retune'.")
    want = {CPU_RAD_UID: cpu_curve, GPU_RAD_UID: GPU_RAD_CURVE}

    lines = text.split("\n")
    done = {}
    table_start = None
    table_uid = None
    for i, line in enumerate(lines):
        if line.startswith("["):
            table_start, table_uid = i, None
        elif table_start is not None and line.startswith("uid = "):
            table_uid = line.split("=", 1)[1].strip().strip('"')
        if table_uid in want and table_start is not None and line.startswith("speed_profile = "):
            old = line
            lines[i] = f"speed_profile = {want[table_uid]}"
            done[table_uid] = (old.strip(), lines[i])
            table_uid = None if table_uid not in done else table_uid
    if set(done) != set(want):
        missing = [profiles[u].get("name") for u in set(want) - set(done)]
        sys.exit(f"could not find the speed_profile line for {missing}")
    for uid, (before, after) in done.items():
        log(f"  {profiles[uid].get('name')}: {before.split('=', 1)[1].strip()}")
        log(f"      -> {after.split('=', 1)[1].strip()}")
    return "\n".join(lines)


def predict(parsed):
    profiles = {p.get("uid"): p for p in parsed.get("profiles", [])}
    log("\nwhat the profiles actually mean on fan1 (calibration-inverted):")
    log(f"   {'scenario':16s} {'CPU':>6s} {'VRAM':>6s} {'CPU duty':>9s} {'GPU duty':>9s} {'mix':>6s} {'raw pwm':>8s} {'RPM':>7s}")
    live_cpu = live_vram = None
    for h in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(h + "/name").read().strip() == "coretemp":
                live_cpu = int(open(h + "/temp1_input").read()) / 1000
        except OSError:
            pass
    try:
        live_vram = int(open("/run/vramsensor/vram.temp1").read()) / 1000
    except OSError:
        pass
    for name, ct, vt in SCENARIOS:
        if ct is None:
            ct, vt = live_cpu or 0, live_vram or 0
            if not live_cpu and not live_vram:
                continue
        cd = curve_at(profiles[CPU_RAD_UID]["speed_profile"], ct)
        gd = curve_at(profiles[GPU_RAD_UID]["speed_profile"], vt)
        mix = max(cd, gd)
        raw, rpm = duty_to_rpm("fan1", mix)
        log(f"   {name:16s} {ct:6.1f} {vt:6.1f} {cd:9.1f} {gd:9.1f} {mix:6.1f} {raw:8.1f} {rpm or 0:7.0f}")
    for ch in ("fan3", "fan4", "fan6"):
        c = calibration(ch)
        if not c:
            continue
        old = [[35.0, 32], [50.0, 40], [60.0, 55], [70.0, 72], [80.0, 90], [90.0, 100]]
        new = profiles[VRAM_EXHAUST_UID]["speed_profile"]
        cells = "  ".join(f"{t:.0f}C: {curve_at(new,t):5.1f}% (was {curve_at(old,t):5.1f}%)" for t in (48, 62, 76, 84))
        log(f"   {ch} exhaust member: {cells}")


def validate(text):
    try:
        parsed = tomllib.loads(text)
    except Exception as e:
        sys.exit(f"REFUSED: the result is not valid TOML ({e})")
    profiles = {p.get("uid"): p for p in parsed.get("profiles", [])}
    counts = {}
    for p in parsed.get("profiles", []):
        counts[p.get("uid")] = counts.get(p.get("uid"), 0) + 1
    dupes = {u: n for u, n in counts.items() if n > 1}
    if dupes:
        sys.exit(f"REFUSED: duplicate profile uids {dupes}")
    for uid, name, ptype in ((CPU_RAD_UID, CPU_RAD_NAME, "Graph"), (GPU_RAD_UID, GPU_RAD_NAME, "Graph"),
                             (RAD_MIX_UID, RAD_MIX_NAME, "Mix")):
        p = profiles.get(uid)
        if not p or p.get("p_type") != ptype or p.get("name") != name:
            sys.exit(f"REFUSED: profile {uid} missing or wrong shape: {p}")
    mix = profiles[RAD_MIX_UID]
    if mix.get("mix_function_type") != "Max" or sorted(mix.get("member_profile_uids") or []) != sorted([CPU_RAD_UID, GPU_RAD_UID]):
        sys.exit(f"REFUSED: radiator mix malformed: {mix}")
    srcs = {profiles[m]["temp_source"]["device_uid"] for m in mix["member_profile_uids"]}
    if VRAM_SENSOR_DEVICE not in srcs or len(srcs) != 2:
        sys.exit(f"REFUSED: radiator mix members do not cover exactly CPU and VRAM: {srcs}")
    if profiles[VRAM_EXHAUST_UID].get("speed_profile") != EXHAUST_CURVE_NEW:
        sys.exit(f"REFUSED: the exhaust member was not retuned: {profiles[VRAM_EXHAUST_UID].get('speed_profile')}")
    dev = parsed.get("device-settings", {}).get(MB_DEVICE)
    if not isinstance(dev, dict):
        sys.exit("REFUSED: [device-settings.<device uid>] missing")
    if dev.get("fan1", {}).get("profile_uid") != RAD_MIX_UID:
        sys.exit(f"REFUSED: fan1 -> {dev.get('fan1')!r}, expected the radiator mix")
    log("validated: TOML parses, no duplicate uids, both radiator curves present, mix covers CPU and VRAM,")
    log(f"           exhaust member retuned, fan1 -> {RAD_MIX_NAME}")
    for ch in sorted(dev):
        if ch != "fan1":
            log(f"           {ch} -> {profiles.get(dev[ch].get('profile_uid'), {}).get('name', dev[ch])}")
    predict(parsed)
    return parsed


def backups(path):
    d = os.path.dirname(path) or "."
    return sorted(glob.glob(os.path.join(d, BACKUP_GLOB)))


def make_backup(path):
    bak = f"{path}.radbak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, bak)
    os.chmod(bak, 0o600)
    tomllib.loads(open(bak, encoding="utf-8").read())
    log(f"backup written and validated: {bak}")
    return bak


def restore(path):
    baks = backups(path)
    if not baks:
        sys.exit("no backups made by this script; use 'sudo coolercontrold restore' for the daemon's own")
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
    patched = build_patch(text, parsed)
    validate(patched)
    log("\nnothing written.")
    if a.show:
        log("--- full patched text ---")
        print(patched, end="")


def cmd_apply(a):
    check_cpu_ceiling(CPU_RAD_CURVE)
    if daemon_active():
        sys.exit("coolercontrold is RUNNING - stop it first: sudo systemctl stop coolercontrold")
    text = read_config(a.config)
    try:
        parsed = tomllib.loads(text)
    except Exception as e:
        sys.exit(f"the CURRENT config does not parse ({e}) - look before editing anything")
    patched = build_patch(text, parsed)
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
        log("coolercontrold check FAILED - rolling back")
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
    log("  sudo systemctl stop coolercontrold")
    log(f"  sudo python3 {os.path.abspath(__file__)} revert")
    log("  sudo systemctl start coolercontrold")
    return 0


def cmd_retune(a):
    """Rewrite the radiator curves on an already-applied config, with the same contract as apply."""
    live = os.path.abspath(a.config) == os.path.abspath(CONFIG)
    if live and daemon_active():
        sys.exit("coolercontrold is RUNNING - stop it first: sudo systemctl stop coolercontrold")
    text = read_config(a.config)
    try:
        parsed = tomllib.loads(text)
    except Exception as e:
        sys.exit(f"the CURRENT config does not parse ({e}) - look before editing anything")
    patched = build_retune(text, parsed, a.preset)
    print_curves(patched)
    if a.dry_run:
        log("dry run: nothing written")
        return 0
    if live and daemon_active():
        sys.exit("coolercontrold started while this was running - stop it and retry")
    bak = make_backup(a.config)
    try:
        open(a.config, "w", encoding="utf-8").write(patched)
        after = tomllib.loads(open(a.config, encoding="utf-8").read())
    except Exception as e:
        log(f"write failed ({e}); rolling back")
        shutil.copy2(bak, a.config)
        return 1
    got = {p.get("uid"): p.get("speed_profile") for p in after.get("profiles", [])}
    if got.get(CPU_RAD_UID) != CPU_CURVES[a.preset] or got.get(GPU_RAD_UID) != GPU_RAD_CURVE:
        log("the written curves do not read back as intended - rolling back")
        shutil.copy2(bak, a.config)
        return 1
    if not daemon_check():
        log("coolercontrold check FAILED - rolling back")
        shutil.copy2(bak, a.config)
        return 1
    if a.start:
        subprocess.run(["systemctl", "start", "coolercontrold"], capture_output=True)
        if not daemon_active():
            log("daemon refused to start with the new curves - rolling back and restarting on the old ones")
            subprocess.run(["systemctl", "stop", "coolercontrold"], capture_output=True)
            shutil.copy2(bak, a.config)
            subprocess.run(["systemctl", "start", "coolercontrold"], capture_output=True)
            return 1
        log("coolercontrold started with the new curves")
    log("\nretuned.  To undo:")
    log("  sudo systemctl stop coolercontrold")
    log(f"  sudo python3 {os.path.abspath(__file__)} revert")
    log("  sudo systemctl start coolercontrold")
    return 0


def resolve_profile(parsed, sel):
    """Find a profile by exact name or full/prefix uid.  Ambiguity is an error, not a coin toss."""
    profs = parsed.get("profiles", [])
    hits = [p for p in profs if p.get("name") == sel or p.get("uid") == sel
            or (len(sel) >= 8 and (p.get("uid") or "").startswith(sel))]
    if not hits:
        names = ", ".join(sorted({p.get("name", "?") for p in profs}))
        sys.exit(f"REFUSED: no profile matching {sel!r}.  Profiles: {names}")
    if len(hits) > 1:
        sys.exit(f"REFUSED: {sel!r} matches {len(hits)} profiles: {[p.get('name') for p in hits]}")
    return hits[0]


def build_smooth(text, parsed, sensor_id, profile_sel):
    """Point the CPU member of the radiator mix at a smoothed custom sensor instead of the raw package temperature.

    Why: the raw Package id 0 sensor bounces ~13 C per second at idle as cores park and boost, and a graph curve turns
    that into 24 points of duty - measured, at no load, oscillating with a ~3 s period.  The loop's thermal mass is tens
    of seconds, so the sensor is what has to be filtered, not the fan.  CoolerControl does this with a custom sensor of
    cs_type TimeAverage or ExponentialMovingAvg; this only repoints the profile at it.

    Deliberately narrow: exactly one temp_source line changes, and the sensor is looked up and type-checked first so a
    typo cannot silently leave the profile on the raw sensor.
    """
    sensors = {s.get("id"): s for s in (parsed.get("custom_sensors") or [])}
    sens = sensors.get(sensor_id)
    if sens is None:
        avail = ", ".join(f"{k} ({v.get('cs_type')})" for k, v in sensors.items()) or "none"
        sys.exit(f"REFUSED: no custom sensor {sensor_id!r}.  Available: {avail}")
    if sens.get("cs_type") not in ("TimeAverage", "ExponentialMovingAvg"):
        sys.exit(f"REFUSED: {sensor_id} is cs_type={sens.get('cs_type')!r}; this only points at a smoothing sensor")
    window = sens.get("time_window_seconds")
    profiles = {p.get("uid"): p for p in parsed.get("profiles", [])}
    target = resolve_profile(parsed, profile_sel)
    target_uid = target.get("uid")
    cur = target.get("temp_source") or {}
    if cur.get("temp_name") == sensor_id:
        log(f"  {target.get('name')!r} already reads {sensor_id} - nothing to do")
        return text
    dev = next((d for d in parsed.get("devices", {}) if d.startswith("19e0")), None)
    if dev is None:
        sys.exit("REFUSED: cannot identify the custom-sensors device uid")
    want = f'temp_source = {{ temp_name = "{sensor_id}", device_uid = "{dev}" }}'

    lines = text.split("\n")
    start = uid = None
    changed = False
    for i, line in enumerate(lines):
        if line.startswith("["):
            start, uid = i, None
        elif start is not None and line.startswith("uid = "):
            uid = line.split("=", 1)[1].strip().strip('"')
        if uid == target_uid and line.startswith("temp_source = "):
            log(f"  {target.get('name')} temp_source: {line.strip()}")
            log(f"      -> {want}")
            lines[i] = want
            changed = True
            uid = None
    if not changed:
        sys.exit(f"could not find the temp_source line of {target_uid}")
    log(f"  smoothing sensor {sensor_id}: cs_type={sens.get('cs_type')} window={window}s "
        f"(measured on this box: 90% of a step's rise in ~16s at a 20s window)")
    return "\n".join(lines)


def cmd_smooth(a):
    live = os.path.abspath(a.config) == os.path.abspath(CONFIG)
    if live and daemon_active():
        sys.exit("coolercontrold is RUNNING - stop it first: sudo systemctl stop coolercontrold")
    text = read_config(a.config)
    parsed = tomllib.loads(text)
    patched = build_smooth(text, parsed, a.sensor, a.profile)
    after = tomllib.loads(patched)
    sel = resolve_profile(after, a.profile)
    got = sel["temp_source"]
    if got.get("temp_name") != a.sensor:
        sys.exit(f"REFUSED: the patched config does not read back with {a.sensor} as the CPU source: {got}")
    if a.dry_run:
        log("dry run: nothing written")
        return 0
    bak = make_backup(a.config)
    try:
        open(a.config, "w", encoding="utf-8").write(patched)
        tomllib.loads(open(a.config, encoding="utf-8").read())
    except Exception as e:
        log(f"write failed ({e}); rolling back")
        shutil.copy2(bak, a.config)
        return 1
    if not daemon_check():
        log("coolercontrold check FAILED - rolling back")
        shutil.copy2(bak, a.config)
        return 1
    if a.start:
        subprocess.run(["systemctl", "start", "coolercontrold"], capture_output=True)
        if not daemon_active():
            log("daemon refused to start - rolling back")
            subprocess.run(["systemctl", "stop", "coolercontrold"], capture_output=True)
            shutil.copy2(bak, a.config)
            subprocess.run(["systemctl", "start", "coolercontrold"], capture_output=True)
            return 1
        log("coolercontrold started, CPU radiator curve now reads the smoothed sensor")
    return 0


def print_curves(text, preset="steep"):
    """What the two radiator curves mean at the temperatures this CPU actually reaches."""
    parsed = tomllib.loads(text)
    profiles = {p.get("uid"): p for p in parsed.get("profiles", [])}
    log("\nfan1 duty the CPU curve now asks for (and the RPM that means on this fan):")
    for t in (40, 50, 60, 66, 72, 78, 84, 90):
        d = curve_at(profiles[CPU_RAD_UID]["speed_profile"], t)
        raw, rpm = duty_to_rpm("fan1", d)
        log(f"   CPU {t:3d}C -> {d:5.1f}% duty -> {rpm or 0:5.0f} RPM")
    top = max(p[1] for p in profiles[CPU_RAD_UID]["speed_profile"])
    log(f"   CPU curve maximum: {top:.0f}% duty ({duty_to_rpm('fan1', top)[1] or 0:.0f} RPM)")
    log("   GPU curve maximum: %.0f%% duty (unchanged: the 3090 must not push this pair into the 70s)"
        % max(p[1] for p in profiles[GPU_RAD_UID]["speed_profile"]))


def cmd_revert(a):
    if daemon_active():
        sys.exit("coolercontrold is RUNNING - stop it first.")
    restore(a.config)


def cmd_backups(a):
    for b in backups(a.config):
        log(f"  {b}  ({os.path.getsize(b)} bytes)")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["plan", "apply", "retune", "smooth", "revert", "backups"])
    ap.add_argument("--config", default=CONFIG)
    ap.add_argument("--preset", default="steep", choices=sorted(CPU_CURVES),
                    help="with retune: which CPU curve shape to install")
    ap.add_argument("--sensor", default="", help="with smooth: the custom sensor id to read")
    ap.add_argument("--profile", default=CPU_RAD_NAME, help="with smooth: profile name or uid to repoint (default the radiator CPU member)")
    ap.add_argument("--dry-run", action="store_true", help="with retune: print the change, write nothing")
    ap.add_argument("--start", action="store_true")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()
    return {"plan": cmd_plan, "apply": cmd_apply, "retune": cmd_retune, "smooth": cmd_smooth, "revert": cmd_revert,
            "backups": cmd_backups}[a.command](a)


if __name__ == "__main__":
    sys.exit(main())
