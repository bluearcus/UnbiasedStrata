# Cooling: the 3090's GDDR6X temperature, and what drives the fans

## Status — applied and live as of 2026-10-20

Every step below says who did it and what proves it.  If you are picking this up later, this section is the truth;
the prose further down is background.

### Done

| # | Step | Who | Proof |
| --- | --- | --- | --- |
| 1 | Card's own fans follow VRAM temperature | assistant, via the LACT daemon socket | `/etc/lact/config.yaml`: `fan_control_enabled: true`, `temperature_key: vram`, curve `40→0.30 … 80→1.00` on `10DE:2204-…-0000:17:00.0` |
| 2 | CoolerControl installed, six channels calibrated | assistant wrote the commands, you ran them | `calibrations.json` holds up/down curves for `fan3…fan6` on device `8b76273c…` |
| 3 | VRAM sensor bridge | assistant wrote it, you installed it | `vram-sensor.service` active; `/run/vramsensor/vram.temp1` mode `0644`, updating |
| 4 | Custom File sensor inside CoolerControl | you, GUI | `[[custom_sensors]] id = "sensor_8f5c0d26"`, path `/run/vramsensor/vram.temp1` |
| 5 | VRAM-keyed profiles applied to `fan5` + `fan6`, stale `GPUs Intake` profile removed | assistant wrote the patch, you ran it with sudo | `config.toml` md5 `2b0f4615…`; `[device-settings.8b76273c…]` with `fan5`/`fan6`; backup `/etc/coolercontrol/config.toml.vrambak-20261020-221247` |
| 6 | UI state confirmed | you, read-only | `fan5` = *3090 Intake (VRAM)*, `fan6` = *Top Rear Exhaust (VRAM)*, `fan3`/`fan4` = *Unmanaged*. **Nothing had to be created in the UI for this step.** |
| 7 | Load test | assistant | 30,074 prompt + 4,000 generated tokens in 57.4 s; VRAM 58 → 68 °C; `pwm5` 110 → 140 (43 → 55 %); `pwm6` 151 → 181 (59 → 70 %); no stalls |

### Open, with owner

| # | Item | Owner | Blocked on |
| --- | --- | --- | --- |
| O1 | ~~Confirm the 3090's own fan channel reads Unmanaged~~ — **done, confirmed Unmanaged**, so LACT owns the card and CoolerControl does not | you | closed |
| O2 | Sustained soak, 12 min — **done, results below** | assistant | closed |
| O3 | Steepen the intake curve / the card's own fan curve to buy a few more degrees | you decide | see "Soak result" below |
| O7 | Calibrate `fan1` — **done**: starts spinning at **0% duty**, max **1771 RPM**, so it cannot be parked | you, GUI | closed |
| O8 | `fan1` under automatic control — **done**, but on its own profiles, not the shared mix (see "Radiator pair and exhaust balance"). Mean exhaust speed fell 15-21% for a mean VRAM change of **-0.5 C** | assistant | closed |
| O9 | **4070 as a backplate blower** — **done, it works**: pinned to 80% static (2715 RPM) the 3090's VRAM settles **2.4 C lower** (76.2 → 73.8 C) and peaks 4 C lower, with every case fan and the 3090's own fan *slower*. Detail and caveats below | assistant | closed, see "O9" |
| O11 | **Closed - it is a hardware limit, not a config bug.**  On 30-series the BIOS fan-curve API accepts **only GPU (edge) temperature**; VRAM and per-chip sensors are readable but not selectable as a curve source.  LACT cannot key on VRAM on this card at all, so `temperature_key: vram` is dead configuration and every curve must be written in edge units | answered | closed |
| O13 | **Summer pickup, not built: a proper VRAM-based response.**  Extend the exporter to publish the worst chip per card, then drive fans from what the memory is actually doing - the 3090 from its own worst chip, the 4070 from max(3090, 4070).  One mechanism constraint to design around: **CoolerControl cannot set GPU fan duty** (it drives the NCT6797 motherboards headers only; the card's channel reads Unmanaged), so GPU duty has to go through LACT - ideally its socket API, which the GUI uses to change settings without restarting the daemon | nobody yet | O12 measured |
| O12 | **Summer plan**: rather than the 4070 at 80% (which was unacceptably loud), a steeper curve on the 3090 itself — noted from your listening test, to be settled *after* O11, since the useful curve depends on which sensor is actually driving that fan | you decide | O11 |
| O10 | **The 4070 boost should trigger on D1 Back, not the aggregate VRAM sensor** — full design below.  Not built: writing fan speed needs root, and the speed knee is unmeasured | you decide | design written |
| O3 | If the soak shows VRAM never reaching 75–85 °C, raise the intake curve (`55→40, 62→55, 70→75, 78→90, 85→100`) | assistant | outcome of O2 |
| O4 | Create one **CPU-sourced Graph profile** — **done**: `CPU temp profile`, source `Package id 0` on `coretemp` (verified it is not the NVMe `Composite` sensor, which also exposes `temp1`) | you, GUI | closed |
| O5 | Mix Profiles on `fan3`/`fan4`/`fan6` — **done**: `Case Fans (CPU or VRAM)`, `Max`, members `CPU temp profile` + `Top Rear Exhaust (VRAM)` | assistant | closed |
| O6 | BIOS: `SYS_FAN5`/`SYS_FAN6` to fixed/ignore | you, at the next reboot | optional — no fighting observed yet, the duties track CoolerControl, not the firmware |

### O4 in the GUI, exactly (about two minutes)

Profiles → add profile → type **Graph** → temperature source: the **CPU** sensor (pick the package sensor if the
dropdown lists cores and a package) → name it `CPU Package` → curve:

```
30 → 15    40 → 25    50 → 40    60 → 55    70 → 70    80 → 85    90 → 100
```

Deliberately gentle at the bottom: this profile only ever *lifts* a fan above whatever the VRAM curve asks, because
the mix takes the maximum — so a low floor costs nothing and a high floor would drown the case at idle.

**Do not attach it to a channel. Do not touch `fan5`.** Save it and tell me nothing else — the mix script finds it by
itself and prints what it matched.

### The one thing left to decide

O3, and it is a preference call rather than an engineering problem: VRAM now sits at **76–78 °C sustained** with
`fan5` at 1480 RPM against a 1769 RPM calibrated maximum and the card's own fan at ~50 % of its scale.  We can leave
it, or steepen the curves to buy a few more degrees in exchange for more noise.  The comparison that matters is
against the original baseline of 78–80 °C with the card's fan pinned at 30–33 %.


The number that matters on this rig is **memory temperature on the 3090**, not core temperature.  The card is
power-capped at 300 W, its core sits in the low 60s, and the GDDR6X on the backplate is what runs hot - 86 °C
sustained was seen in a cooler room, and this loft is unconditioned, so summer is a different machine.  The 4070
draws ~19 W in this workload and is left alone.

## Measured baseline

| | value |
| --- | --- |
| 3090 fan under engine load | **30-33 % (1298-1386 RPM)** - the vBIOS curve barely moves |
| 3090 VRAM under load | **78-80 °C**, hottest individual chips 74-76 °C |
| GPU core / hotspot | 62 °C / 74 °C |
| Power | 290-298 W against the 300 W cap (`Throttling: SW_POWER_CAP`) |
| Case fans (SYS_FAN) | ~78-79 %, driven by **CPU temperature** (`pwm1_temp_sel = 7`, PECI) - they react to the 9-worker CPU pool, not to the GPU |
| Board | MSI X299 PRO 10G, Nuvoton **NCT6797** (`nct6775` in-kernel driver, already loaded): `CPU_FAN1`, `PUMP_FAN1`, `SYS_FAN1-4` = 6 PWM outputs, 6 tach inputs |

That is the "not configured sensibly" part: the case fans follow the CPU while the GPU's memory is the hot thing,
and the card's own fans sit at 30 % because its vBIOS target temperature is 83 °C.

## Which header is which

Six headers on the NCT6797 (`CPU_FAN1`, `PUMP_FAN1`, `SYS_FAN1-4`), six tach inputs, and the physical fans are:
CPU AIO (radiator pair + pump), a front intake pair, a GPU intake fan, a rear exhaust, and a top exhaust pair -
so some groups share a header via a splitter.

What the board reports without touching anything (`/sys/class/hwmon/hwmon2`, plus a 25 s CPU-only load of 10
busy loops to see which tach responds):

| header | PWM | BIOS curve source | written PWM | tach | response to a CPU-only load |
| --- | --- | --- | --- | --- | --- |
| 1 | `pwm1` | `temp_sel=7` **PECI (CPU)** | 125 | `fan1` 788 → **1226** | moves strongly |
| 2 | `pwm2` | `temp_sel=7` **PECI (CPU)** | 125 | `fan2` **4166 → 4153** | constant ~4150 |
| 3 | `pwm3` | `temp_sel=7` **PECI (CPU)** | 197 | `fan3` 1045 → **1191** | moves |
| 4 | `pwm4` | `temp_sel=7` **PECI (CPU)** | 199 | `fan4` 1093 → **1212** | moves |
| 5 | `pwm5` | `temp_sel=1` **SYSTIN** | 147 | `fan5` 1225 → 1205 | **does not move** |
| 6 | `pwm6` | `temp_sel=1` **SYSTIN** | 178 | `fan6` 1020 → 1009 | **does not move** |

The curves themselves (from `pwmN_auto_pointN_*`):

- headers 1-2: `40->33, 55->96, 70->160, 85->255` and `27->12, 52->84, 71->163, 85->244` - real CPU curves.
- headers 3-4: `40->127, 55->178, 72->226, 85->255` - real CPU curves, already running fairly high (197/199 of 255).
- **headers 5-6: `25->147, 35->147, 45->147, 55->147, 101->255`** and `25->178, ... 55->178, 101->255` - a flat
  duty (57 % and 70 %) that only changes if something reaches **101 °C**.  That is the "not configured sensibly"
  smoking gun: two of the case fans are pinned and respond to nothing.

Reading the evidence: `fan2` at a constant 4150 is the AIO pump; `fan1` moves most with the CPU, so it is the
AIO radiator pair.  Which of 3-6 was which was not discoverable in software - the board does not label them - so it
was settled by cycling each channel to a fixed duty in CoolerControl and watching which physical fan moved
(2026-10-20).

### The confirmed mapping

| header | tach | duty before any change | **physical fan** |
| --- | --- | --- | --- |
| `pwm1` | `fan1` | 125/255, CPU curve | **CPU AIO radiator fans** |
| `pwm2` | `fan2` | 125/255, CPU curve | **AIO pump** (4153 RPM, constant) |
| `pwm3` | `fan3` | 194/255 (76 %), CPU curve | **rear exhaust** |
| `pwm4` | `fan4` | 196/255 (77 %), CPU curve | **top forward exhaust** |
| `pwm5` | `fan5` | **147/255 (57 %), flat SYSTIN curve** | **front bottom GPU intake** |
| `pwm6` | `fan6` | **178/255 (70 %), flat SYSTIN curve** | **top rear exhaust** |

That is the diagnosis in one line: **the only fan blowing at the 3090's backplate is the one pinned at 57 % by a
curve that does not move until something reaches 101 °C**, while the CPU-driven fans run at 76-77 % because the
9-worker CPU pool is warm.  Airflow path: front-bottom intake -> rear + top exhausts.

### The airflow layout

Front: **two fans push through the AIO radiator** (`pwm1`, restricted - they move air through fin stacks) and
**one unobstructed fan at the front bottom** (`pwm5`, feeding the 3090 directly).  Exhaust: rear (`pwm3`), top
forward (`pwm4`), top rear (`pwm6`).  So nominal intake is three fans, but two of them are fighting a radiator,
which means the *effective* intake is much closer to the exhaust count than the fan count suggests - and the one
free-flowing intake fan is the one aimed at the GPU.

### What each channel should be driven by

| header | drive it with | why |
| --- | --- | --- |
| `pwm5` front bottom GPU intake | **`3090 VRAM`** Graph Profile, the most aggressive curve on the machine | the only unobstructed intake, and the only fan blowing straight at the GDDR6X; currently the least responsive one |
| `pwm1` AIO radiator pair | **Mix Profile, Maximum of (CPU curve, `3090 VRAM` curve)** | it is the bulk of the intake, and a hot case raises the coolant temperature the radiator has to reject - a CPU-only curve ignores the 300 W the GPU is putting into the same air |
| `pwm6` top rear exhaust | **Mix Profile, Maximum of (`3090 VRAM`, CPU)** | top-rear is where the card's own exhaust goes; it must follow both heat sources |
| `pwm3` rear exhaust | Mix Profile, Maximum of (`3090 VRAM`, CPU) | already doing real work at 76 % off CPU heat alone |
| `pwm4` top forward exhaust | Mix Profile, Maximum of (`3090 VRAM`, CPU), gentler curve than 3/6 | helps, furthest from the card |
| `pwm2` AIO pump | **leave alone / fixed full speed** | pumps should not be modulated |

Build it as **Mix Profiles, not Mix Custom Sensors**: make a Graph Profile per temperature source (one on
`3090 VRAM`, one on the CPU sensor), then a Mix Profile that takes the **Maximum** of the two and apply *that* to
the channel.  CoolerControl's own guidance is that a Mix Profile is the right tool "when mixing two temperature
sensors with different ranges and response dynamics" - which is exactly the case here, since CPU load ramps slowly
and generation load ramps fast.  A Mix *sensor* would collapse the two signals before the curve is applied.

Balance: with two of three intakes restricted by the radiator, driving the exhausts harder than the intake would
put the case under negative pressure and pull air in through every gap - worse cooling and noisier.  `pwm5` leads
the ramp, the exhausts follow it, and `pwm1` is the way to add intake airflow without buying a fan.

Calibration (`Cooling -> channel -> Calibrate`) measures each fan's true min/max RPM so the curves are expressed in
real airflow rather than raw duty.  It is slow - a sweep per fan - and only needs doing once; run it while the
engine is idle so the sweep is clean, and expect the applied curves to land at different duties than the raw
percentages above.

## The split

| Layer | Tool | Why |
| --- | --- | --- |
| The 3090's own fans | **LACT** (installed: `/usr/bin/lact`, `lactd.service` enabled, GUI + daemon socket) | its curve can be keyed on `vram`, which nothing else on this machine can read; configured and verified below |
| The case fans | **CoolerControl** (daemon + web UI) | controls the motherboard's hwmon channels, curves/mixes/hysteresis in a GUI, and its **File custom sensor** accepts a temperature exported by another program - which is the only way to get VRAM heat into a curve |
| The bridge | `tools/vram_sensor.py` (30 lines, writes a number, controls nothing) | CoolerControl's NVIDIA backend exposes GPU temperature and hotspot, **not memory temperature** |

An earlier draft of this document proposed hand-rolling the case-fan control loop.  That was the wrong instinct and
the script is deleted: CoolerControl already implements the loop, and does it better - curves, mixes, EMA/time
averaging, functions for hysteresis and response time, per-channel calibration, device health, and a **failsafe of
100 °C for a lost temperature source**, which is the safety property a hand-rolled loop would have had to invent.

## Configured: the card's fans follow VRAM (live, verified)

Applied through the LACT daemon socket (`/run/lactd.sock` is `srw-rw---- root sudo`, the user is in `sudo`, so no
root needed) and persisted in `/etc/lact/config.yaml`.  Equivalent in the GUI: Fan Control → enable → custom curve →
temperature source.

```yaml
gpus:
  10DE:2204-1569:2204-0000:17:00.0:     # the 3090 (Palit, Micron GDDR6X)
    fan_control_enabled: true
    fan_control_settings:
      mode: curve
      temperature_key: vram             # accepted on NVIDIA: the curve follows memory, not edge
      interval_ms: 1000
      curve: { 40: 0.30, 50: 0.40, 60: 0.55, 70: 0.75, 80: 1.0 }
      spindown_delay_ms: 8000           # do not hunt between requests
      change_threshold: 2
      auto_threshold: 0
    power_mizer_mode: Adaptive
    power_cap: 300.0
```

Measured on a 2000-token generation:

| | before | after |
| --- | --- | --- |
| Peak fan | 77 pwm / **1386 RPM** | 127 pwm / **1956 RPM** |
| Peak VRAM | **80 °C** | **76 °C** |
| Decode | 105.3 tok/s | **105.5 tok/s** (unchanged) |

The curve's top point (80 °C → 100 %) was not reached in a cold room; in summer it will be.  The 4070 stays on
automatic (`fan_control_enabled: false`).

## Setup: CoolerControl for the case fans (done, kept for re-install / another machine)

```bash
# 1. install (Debian 13 trixie is supported: >= Bookworm)
sudo apt install curl apt-transport-https
curl -fsSL https://apt.coolercontrol.org/setup.sh | sudo -E TREE=debian sh
sudo apt update
sudo apt install coolercontrol lm-sensors          # coolercontrold comes with it
sudo systemctl enable --now coolercontrold

# 2. the sensor bridge (absolute paths: paste from any directory)
sudo cp /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/vram_sensor.py /usr/local/bin/vram-sensor
sudo chmod 0755 /usr/local/bin/vram-sensor
sudo cp /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/vram-sensor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now vram-sensor.service
cat /run/vramsensor/vram.temp1                     # e.g. 72000 = 72.0 C in millidegrees

# 3. open http://localhost:11987 and check the Hardware Support card on Home first:
#    it must list the NCT6797 channels as controllable.  No DKMS driver is needed here -
#    CoolerControl's nct6687d-dkms is for NCT6687 boards; this one is NCT6797, already handled
#    by the in-kernel nct6775.
```

Then in the interface:

1. **Monitoring → Custom Sensors → new sensor**: metric **Temperature**, type **File**, file
   `/run/vramsensor/vram.temp1`.  Name it `3090 VRAM`.  (A File sensor is exactly for "an external tool exports a
   reading not natively detected"; the value is millidegrees.)
2. Optional smoothing: a **Time Average** or **EMA** sensor on top of it, so the case fans track the trend rather
   than each request.  CoolerControl's *Functions* do hysteresis and response time on the channel instead.
3. **Cooling → Profiles → Graph Profile** `GPU intake`, source `3090 VRAM`, curve e.g.
   `45→25 %, 55→40 %, 65→60 %, 75→80 %, 85→100 %`.
4. Apply it to the **SYS_FAN channels** that sit in front of the 3090 - identify them on each channel's page
   (CoolerControl cannot tell what each fan is for; the Auto-Create Profiles wizard asks rather than guessing, and
   the same care applies here).  Leave `CPU_FAN1` and `PUMP_FAN1` on their own curve for the CPU pool.
5. **Set the 3090's fan channel to `Unmanaged` in CoolerControl** so LACT keeps the card's fans.  CoolerControl's
   own docs warn against two tools controlling one fan; their conflict note is AMD-specific, but the rule is the
   same: whichever owns a channel must be the only one writing it.
6. In **BIOS → Hardware Monitor → Fan Management**, set the SYS_FAN entries handed to CoolerControl to a fixed
   value or Disabled, so the firmware curve is not fighting it.
7. Optional, and the cleanest long-term answer to "ramp intake on GPU heat": a **thermistor on the 3090's
   backplate** plugged into one of the board's `AUXTIN0-3` headers.  Then the reading is a real board sensor, the
   exporter script is unnecessary, and plain `fancontrol` would do it too.  Hardware purchase; the exporter is the
   no-cost equivalent.

## The config patch (`tools/coolercontrol/vram_profiles.py`) — applied 2026-10-20

CoolerControl's configuration lives in `/etc/coolercontrol/config.toml` (TOML, daemon-owned; `overrides.toml` is the
hand-editable name file).  Its API requires authentication for reads as well as writes, so the patch is applied to
the file - under root, with the daemon stopped.  The script exists because TOML is table-scoped: the new
`[[profiles]]` entries must land inside the profiles array and the channel assignments directly under
`[device-settings]`, and a key in the wrong scope parses happily and does nothing.

Safety, all of it exercised on a copy of the live config before first use:

| Path | Result |
| --- | --- |
| `plan` | prints the added lines, validates the result with `tomllib`, writes nothing |
| `apply` | refuses to run while `coolercontrold` is active (it rewrites the config on shutdown) |
| `apply` | re-checks idempotency: a second run reports "already applied" and changes nothing |
| backup | byte-exact copy (`config.toml.vrambak-<stamp>`, mode 0600) parsed and validated before anything is written |
| write | the patched text is re-parsed, and the script verifies `fan5`/`fan6` resolve to the intended profile UIDs - it refuses if an assignment landed in the wrong table |
| `coolercontrold check` | if the daemon's own validator fails, the backup is restored automatically and the script exits non-zero |
| `--start` | if the daemon refuses to start with the new config, it rolls back, restarts the service with the old config |
| `revert` | restores the newest backup byte-for-byte (verified: md5 identical to the original) |

What the patch installs: two Graph profiles on the `3090 VRAM` File sensor, applied to **`fan5` (front bottom GPU
intake)** and **`fan6` (top rear exhaust)** - the two headers pinned by flat BIOS curves.  Curve floors respect the
measured stall duties (`fan5` will not start below 20%, `fan6` not below 30%).

What it removes: the existing **`GPUs Intake`** profile.  Its curve asks for 13% duty at 0 C, below `fan5`'s measured
20% stall duty, and an unused profile with a stall-breaking floor left in the list is one mis-click away from a
stopped intake fan.  Removal is done with a TOML segment splitter (delete the whole `[[profiles]]` block whose uid
matches, nothing else) rather than a regex, it refuses if the uid is not found exactly once, if the block is not the
expected profile, or if any channel still applies it; and it hands trailing comment lines to the following table so
the `# A list of functions ...` header comments survive.  `revert` brings it back byte-for-byte.

What it deliberately does **not** touch: `fan1` (AIO radiator), `fan2` (pump), `fan3` (rear exhaust) and `fan4`
(top forward exhaust).  `fan3`/`fan4` are already at 76% on a CPU curve doing real work; a VRAM-only curve would
drop them to 30-48% whenever the GPU is idle and regress CPU cooling.  They need Mix Profiles, and a Mix Profile
needs a CPU-sourced Graph profile - which cannot be authored blind, because `temp_source.temp_name` is
CoolerControl's internal sensor id and the API that lists devices is authenticated.  So: create that one profile in
the GUI (pick the CPU sensor from the dropdown), note its uid from the config afterwards, and the mixes become
mechanical.

```bash
# Every command below is absolute: paste it from any directory, in any shell.
cd /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata

sudo systemctl stop coolercontrold
sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/vram_profiles.py plan
sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/vram_profiles.py apply --start
sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/vram_profiles.py backups

# undo, if anything looks wrong:
sudo systemctl stop coolercontrold
sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/vram_profiles.py revert
sudo systemctl start coolercontrold
```

### Soak result (12 min, after the mixes) — `tools/cooling_soak.sh 12`

Nine back-to-back completions, ~30k prompt tokens each, 8,000 generated each: **70,777 tokens in 760 s = 93.1 tok/s**
including prefill, GPU pegged at ~299 W (the 300 W cap) and ~100 % utilisation for the whole window.

| | value | old BIOS pin |
| --- | --- | --- |
| 3090 VRAM | **peak 78 °C, mean 75.4 °C, settled 76 °C** | — |
| 3090 core | 57 °C | — |
| card's own fan (LACT) | 48–51 % | — |
| `pwm3` rear | 158–212 duty (61–83 %), 877–1137 RPM | 203 (79 %) — exceeded |
| `pwm4` top forward | 161–217 (63–85 %), 935–1205 RPM | 205 (80 %) — exceeded |
| `pwm5` GPU intake | 130–191 (50–74 %), 1092–1480 RPM | 147 (57 %) — exceeded |
| `pwm6` top rear | 173–219 (67–85 %), 995–1214 RPM | 178 (69 %) — exceeded |

No header dipped near its stall floor at any point, and every header ended up above the flat BIOS duty it used to be
pinned to.  VRAM stopped climbing at 76–78 °C instead of running on to the 78–80 °C of the original baseline, where
the card's own fan sat at 30–33 %.

Remaining headroom: `fan5` reached 1480 RPM against a calibrated maximum of 1769, and the card's own fan only reached
~50 % of its scale.  So there is more cooling available if we want it — that is a noise decision, not a safety one.

### Known gap: the case is probably at negative pressure under GPU load

Measured duties in the 12-minute soak: exhaust `pwm3` 61–83 %, `pwm4` 63–85 %, `pwm6` 67–85 %.  Intake was `pwm5` at
50–74 % plus `pwm1` — **which the harness never sampled at all** (fixed in `446dc5c`: it now samples all six headers
and prints an intake-versus-exhaust balance line).

With the radiator pair at the 67–69 % seen during the CPU test, the duty-weighted balance works out near **0.88
intake-to-exhaust, and the true airflow ratio is worse than that number** because a fan pushing through a radiator
moves less air per percent than a free-air fan.  So the intuition that the case goes negative under GPU load is well
supported.

`fan1` was left on its BIOS curve for exactly one reason: CoolerControl has calibrations for `fan3`–`fan6` only
(start duty 16 / 14 / 20 / 30 %, maxima 1313 / 1333 / 1769 / 1209 RPM), and putting an uncalibrated header under
automatic control means not knowing where it stops spinning.  Calibration is a two-minute job in the GUI, so this is
a gap to close rather than a reason to leave the intake alone.  `fan2` stays unmanaged: it is a pump (~4150 RPM
regardless of duty), and pumps should not be modulated.



### Radiator pair and exhaust balance — the second comparison soak

Putting `fan1` on the shared `Case Fans (CPU or VRAM)` mix made the machine louder, for two reasons documented in
`tools/coolercontrol/radiator_profiles.py`: `fan1`'s calibration starts at 641 RPM so profile duties are rescaled
against its RPM range (58% duty there means 1298 RPM, 72% raw), and the VRAM member won the `Max` at idle because
the 3090's memory rests warm.  The fix: `fan1` gets its own `CPU (radiator)` / `GPU (radiator)` / mix, with the GPU
member **flat until 70 C** — it does not ramp for the 3090 below 70 C at all — and the exhaust member retuned to
flat 32% below 50 C, topping out at 75% instead of 100%.

Matched 6-minute windows, identical payload (30k prompt tokens, 8k generated, back-to-back):

| | before (shared mix) | after (radiator fix) |
| --- | --- | --- |
| 3090 VRAM mean | 74.4 C | **73.9 C** |
| 3090 VRAM min-max | 66 – 76 C | 62 – 78 C |
| 3090 core mean | 57 C | 56 C |
| card's own fan mean | 48 % | 49 % |
| rear exhaust | 76.6 %, 1044 RPM | 58.6 %, **846 RPM (−19 %)** |
| top forward exhaust | 78.1 %, 1100 RPM | 59.4 %, **873 RPM (−21 %)** |
| top rear exhaust | 81.2 %, 1136 RPM | 64.5 %, **961 RPM (−16 %)** |
| GPU intake (`fan5`) | 67.8 %, 1365 RPM | 66.9 %, 1358 RPM |
| radiator pair (`fan1`) | not sampled | 47.2 %, 792 RPM |

**The trade came out almost free: three exhausts roughly 20% slower, mean VRAM 0.5 C better, peak 2 C higher.** The
GPU intake stayed where it was, which is the one that actually feeds the 3090.

Caveat kept on the record: the intake/exhaust balance ratio (0.88 this run) **cannot** be compared against the
earlier run, because the harness at that time did not sample `pwm1` at all — its "intake" was `fan5` alone.  The
ratio is only comparable from now on.

### O9: the 4070 as a backplate blower — it works, and here is what it costs

The 4070 sits half a slot above the 3090 and both cards intake from underneath, so at rest the 4070's fans sit at
their VBIOS 30% (1300 RPM) while 67 W of idle card leaves a warm blanket on the 3090's backplate — the 3090's
hottest, least-cooled surface.  Pinned to 80% static (2715 RPM) through LACT (`tools/lact/fan_pin.py`), that card
becomes a free blower aimed at exactly the right place.

**The mechanism is real, measured directly.**  While the 3090 decoded, the worst back-side memory chip ran **74 C
against 70 C for the worst front-side chip** — the backplate is genuinely the hot side, which is the premise the
whole idea rests on.

Steady-state VRAM on the 3090, matched 6-minute runs, identical payload, no other config change:

| minute | 4070 at VBIOS 30% | 4070 pinned 80% | delta |
| --- | --- | --- | --- |
| 0 | 67.2 C | 53.3 C | −13.8 |
| 1 | 73.0 C | 65.5 C | −7.5 |
| 2 | 75.5 C | 70.0 C | −5.5 |
| 3 | 76.0 C | 72.0 C | −4.0 |
| 4 | 76.0 C | 73.2 C | −2.8 |
| 5 | 76.2 C | 73.8 C | **−2.4** (settled) |

Whole-run peak 78 → **74 C**.  And crucially it was not bought by working the 3090 harder — everything on the 3090's
own cooling ran *slower* because it was cooler:

| | VBIOS 30% | pinned 80% |
| --- | --- | --- |
| 3090's own fan (plateau) | 50 % | 48 % |
| radiator pair `fan1` | 47.4 % | 47.7 % |
| rear exhaust `fan3` | 59.4 % | 58.7 % |
| top forward `fan4` | 60.2 % | 59.2 % |
| GPU intake `fan5` | 71.1 % | **65.0 %** |
| top rear `fan6` | 65.3 % | 64.5 % |

So the 3090 is cooler **and quieter**; the price is paid entirely on the 4070, at 2715 RPM — a card that is
otherwise inaudible.  That is the whole trade, and it is a policy question rather than a technical one.

**Caveats kept on the record, because this was not a perfect A/B:**

* The runs were **sequential, not interleaved**, and started from different temperatures (62 C vs 40 C).  The
  table is a per-minute comparison precisely so the ramp does not flatter the result; the steady-state figure is the
  one to trust, and it is the −2.4 C at minutes 5-6.
* One pair of runs, in one ambient.  Evening-to-midnight ambient drift is unquantified.
* A confirmatory interleaved pair (pin / unpin / pin) is worth an hour if this ever becomes load-bearing rather than
  interesting.

**Why this is not switched on as a permanent setting.**  LACT's `temperature_key` can only select sensors on the card
it is configuring, so the 4070 cannot be told "speed up when the *3090* is hot" from its own config.  Constant 80%
is the only thing its config can express, and constant 2715 RPM for a card that is idle most of the day is a bad
default.  The useful version is conditional — boost the 4070 only while the 3090's VRAM is above ~65 C and the 4070
is idle — which needs a small daemon reading `/run/vramsensor/vram.temp1` and driving the 4070 through LACT.
Recorded as **O10**; not built, because it is a noise-policy decision before it is a coding one.

### O11: is the 3090's own fan curve doing anything?

Asked for a steeper curve on the 3090 as the summer answer to the 4070's noise, the obvious prerequisite is knowing what
those fans are currently following.  They are not following VRAM.  Read off a sustained run, alongside the curve
`{40:0.30, 50:0.40, 60:0.55, 70:0.75, 80:1.0}` evaluated at each of the two candidate sensors:

| VRAM | edge | reported fan | curve(vram) says | curve(edge) says |
| --- | --- | --- | --- | --- |
| 60 C | 51 C | 38 % | 55 % | 42 % |
| 64 C | 53 C | 43 % | 63 % | 44 % |
| 68 C | 55 C | 44 % | 71 % | 48 % |
| 70 C | 55 C | 47 % | 75 % | 48 % |
| 74 C | 57 C | 48 % | **85 %** | **50 %** |

Mean over every sample at or above 65 C VRAM: **fan 47 %, curve(edge) 49 %, curve(vram) 79 %.**  The edge column is a
near-exact match across the whole ramp, which is not something a coincidence does.

Two possible explanations, and they matter differently:

1. **LACT is applying the curve but keying on the wrong sensor** — `temperature_key: vram` is being ignored or falling
   back to edge/default.  Fix: rewrite the curve in edge units, or find the sensor key this LACT build accepts.
2. **LACT is not driving this card's fan at all**, and the two curves above merely resemble each other by luck (a
   Palit 3090 BIOS ramps to roughly 48 % at 57 C edge, which is plausible).  Fix: nothing in LACT will help until that
   is addressed.

The discriminator is a static pin, which no curve can imitate — `fan_pin.py pin --gpu 3090 --speed 0.35` and read
`nvidia-smi`.  Not run yet; it needs one root command, and it is worth doing before touching any 3090 curve because it
says which of the two problems we actually have.


### The sensor question, answered by measurement

Same run, all three NVIDIA sensors, versus the aggregate VRAM reading:

| t | edge | hotspot | VRAM | VRAM − edge | VRAM − hotspot |
| --- | --- | --- | --- | --- | --- |
| 0 | 27 | 36 | 40 | +13 | +4 |
| 61 | 53 | 64 | 62 | +9 | −2 |
| 121 | 56 | 68 | 70 | +14 | +2 |
| 161 | 56 | 68 | 74 | **+18** | +6 |
| 201 | 56 | 68 | 74 | **+18** | +6 |

**Edge saturates and stops reporting.**  Between t=141 and t=201 the edge sensor sits flat at 56-57 C while VRAM rises
72 -> 74 C.  Hotspot is not perfect either (it flattens at 68 C) but it tracks VRAM within 0-6 C instead of 12-18 C.

That explains the whole observation without needing a bug: **the curve is being applied, in edge units, and its useful
band lies outside where this card operates.**  The curve reaches 100% at edge 80 C.  Under sustained inference edge
never exceeds 57 C, so the curve is stuck between 39% and 50% forever - which is exactly the 47-48% measured.

| edge | current curve | moderate | aggressive |
| --- | --- | --- | --- |
| 49 C | 39 % | 48 % | 55 % |
| 53 C | 44 % | 60 % | 72 % |
| 56 C | 49 % | 72 % | 88 % |
| 57 C | 50 % | 76 % | 91 % |

```
moderate    40:0.30  45:0.38  50:0.50  53:0.60  56:0.72  60:0.90
aggressive  40:0.30  45:0.42  50:0.58  53:0.72  56:0.88  60:1.00
```

Both keep the 30% floor below 40 C so idling stays silent.  Which one is right cannot be settled by reading curves: the
missing measurement is **fan percent to VRAM degrees**, i.e. step the fan to 50/60/70% under sustained load and read
the VRAM plateau at each.  That sweep is what the summer decision needs, and it is the same measurement that would tell
us the 4070's knee (O10).

**Consequence for the summer plan (O12).**  If explanation 1 holds, the honest headroom is larger than it looks: the
card is sitting at ~48 % fan with VRAM at 74 C, and a curve keyed to VRAM would be asking for 85 % there.  Somewhere
between those is the quiet setting that buys the degrees — and it should be *measured* by stepping the fan and reading
VRAM, not inferred from the curve, because the curve has just been shown not to mean what we thought.

### Why edge is the only option: the 30-series BIOS fan API

LACT drives these fans through the card's own BIOS fan-control interface, and on 30-series that interface accepts **only
the GPU (edge) temperature** as a curve source.  VRAM and per-chip VRAM sensors are readable - the 24-chip table above
comes straight out of NVAPI - but they cannot be *selected* to drive a curve.  This is a property of the firmware
interface, not of LACT, and no configuration can change it.

That explains every observation without a bug:

* `temperature_key: vram` parses, is accepted, and is ignored.  The key is unrepresentable, so the daemon falls back to
  edge.  This is why `which_sensor.py` scores edge at ~6 % error against hotspot and aggregate VRAM at ~19-20 %.
* The curve's useful band (up to 80 C) is outside where this workload operates (edge 49-57 C), so it self-caps near
  50 %: the 47-48 % we measured, for as long as we have been measuring it.

**Three consequences, all of which change the plan:**

1. **Every curve on the 3090 must be written in edge units, permanently.**  The moderate/aggressive curves above are the
   only route.  `temperature_key` should be set to `edge` for honesty - it changes no behaviour, it just stops the file
   from claiming something the hardware cannot do.
2. **An edge-keyed curve is a proxy, and a loose one.**  The edge-to-VRAM offset measured between +9 and +18 C, and it
   drifts upward at plateau (edge sits flat at 56-57 while VRAM goes 72 -> 74).  An edge curve therefore carries roughly
   **9 C of uncertainty about the memory temperature it is actually guarding**, and the offset is workload-dependent: a
   workload with different memory-traffic characteristics will shift it.  Tuning a curve for this engine's offset is
   fitting to one workload, not solving the problem.
3. **Anything genuinely VRAM- or D1-driven must be an external agent** - read the sensor through LACT/NVAPI, then set fan
   speed through the same BIOS interface.  That is exactly O10's daemon, so it is no longer an optional refinement: it
   is the only way to drive these fans from memory temperature at all.

### The edge curve, measured: +17 points of fan bought 2 C

The moderate curve was applied to the active profile and soaked for 5 minutes (30k prompt tokens, ~30k generated,
93.0 tok/s - throughput unchanged, so nothing here costs tokens).

| edge | hotspot | VRAM | fan | curve asks |
| --- | --- | --- | --- | --- |
| 49 | 61 | 58 | 44 % | 48 % |
| 52 | 64 | 64 | 58 % | 58 % |
| 53 | 65 | 70 | 58 % | 62 % |
| 53 | 66 | **72** | **64 %** | 62 % |

**The fan responds now** - 63 % actual against 65 % predicted at edge >= 54 C, so the curve is genuinely in effect and
edge-keyed, unlike the base-entry edit that did nothing.

**Plateau: VRAM 74 -> 72 C. Cost: fan mean 42 -> 54 %, peak 64 % (about 2500 RPM territory), against 47-48 % before.**

That is roughly **0.12 C per point of fan**. Reaching 68 C would need about 97 % fan. The conclusion is not a tuning
problem: **the 3090 is airflow-limited, not fan-limited.** There is no curve that fixes it, which is why the 4070 blowing
across the backplate bought 2.4 C for free while 17 extra points of the card's own fans bought 2 C at real noise.

Two things this run also showed, both worth keeping:

* **The curve settles lower than predicted (64 % not 78 %).**  Edge plateaued at 53-54 C instead of the 56-57 C seen
  earlier, because the stronger fan cools the core - so the sensor the curve keys on moves *in response to the curve
  itself*.  Negative feedback through the actuator.  It is a third reason edge cannot stand in for VRAM: the proxy is
  coupled to the thing it is controlling.
* **The edge-to-VRAM offset widened to ~19 C** (72 vs 53) from ~18 C (74 vs 56), so it did not behave as a constant.

For summer, this makes the ranking explicit: added airflow first (O10's D1-driven 4070 boost), a marginal extra curve
step second, and expecting 68 C from motherboard fans not at all.

### O13: the summer pickup, and why the edge curve cannot be tuned out of this

Settled as the place we stop for now, with the design written down rather than half-remembered.

**Why the current setup is self-limiting, precisely.**  The card's shroud fans pressurise the cooler and cool the *core* far
more effectively than the memory on the reverse of the board.  So when VRAM heat drives the fan up through an edge-keyed
curve, edge falls faster than VRAM does, the curve backs off, and the mechanism neutralises itself.  That is not a bad
curve - it is a curve measuring a sensor that its own action moves.  A VRAM-driven response cannot be approximated from
edge at all; it needs the memory sensors as the input.

**The summer pickup (O13), if it is needed:**

1. Extend `tools/vram_sensor.py` to publish the **worst chip per card** - the 3090's hottest of the 24, and the 4070's own
   reading - instead of only the aggregate.
2. Drive fans from those: **the 3090 from its own worst chip, the 4070 from max(3090 worst, 4070 worst)** - the 4070 existing
   to cool the 3090's backplate, so it should follow the 3090's need as well as its own.
3. Keep the existing CoolerControl work untouched: it owns the motherboard headers and the CPU/AIO side, and it does that well.

**One constraint the plan must respect.**  CoolerControl **cannot** set GPU fan duty - it drives the NCT6797 headers, and the
3090's fan channel appears there as Unmanaged (O1).  So the GPU side has to go through LACT.  Editing `/etc/lact/config.yaml`
and restarting `lactd` per change (what `fan_pin.py` does) is fine for a one-off but wrong for a control loop; the right
actuator is LACT's socket API, which the GUI already uses to change fan settings live with no restart.  Half an hour of
protocol discovery is the first step of O13, not an afterthought.

**Both cards need the agent.  Neither half collapses into configuration:**

* the **3090** cannot be keyed on its own worst chip, because 30-series accepts only edge (established above), and
* the **4070** cannot be keyed on `max(3090 worst, 4070 worst)` **even if 40-series accepts a VRAM source**, because a
  per-card curve sees only that card's own sensors.  `max()` across two cards is not expressible by LACT's curve and not by
  the BIOS fan table either - it is not a matter of finding the right key name.

So the external agent is mandatory on both sides, and O13 is one piece of work, not one piece plus a config change.
Worth still testing, cheaply and separately, whether 40-series accepts VRAM at all - it decides whether the 4070's own
contribution can be a *local* curve with the cross-card boost layered on top, or whether both temperatures have to be
computed in the agent.

### What moving the ceiling actually bought (and what it could not)

Two identical 2-minute `matrixprod` runs, before and after moving the CPU curve's 100 % point from 75 C to 80 C:

| | CPU peak / mean | clock | fan1 mean | peak RPM |
| --- | --- | --- | --- | --- |
| 100 % by 75 C | 81 / 78 C | 4201-4300 MHz | 98 % | 1744 |
| 100 % by 80 C | 80 / 78 C | 4215-4300 MHz | 97 % | 1744 |

**They converge under a solid synthetic load**, and the reason is structural: the CPU settles at 78-80 C, which *is* the
new ceiling.  A ceiling at or below the settling temperature means 100 % duty at steady state whatever the curve above it
looks like.  Flattening the top only helps at loads lighter than the soak:

| CPU | 100 % @ 75 C | 100 % @ 80 C |
| --- | --- | --- |
| 72 C | 94 % / ~1660 RPM | 84 % / 1587 RPM |
| 75 C | 100 % / 1744 | 90 % / 1656 |
| 78 C | 100 % | 96 % / 1725 |

That is the intended trade: **quieter at real working loads, unchanged at worst case.**  Real inference traffic on this
box does not hold twenty threads in AVX-512 for minutes; it sits nearer 70-76 C, where the 6-16 saved duty points are
audible.  The one clock dip in the second run (4256 and 4281 MHz against 4300) is turbo oscillation, not throttling.

### The fan calibration is optimistic under the filter

`fan1` reads **1700-1744 RPM at 100 % duty**, against the calibration's 1771 maximum.  The calibration was measured with
the case front and filter off, so it grants the fan a few hundred RPM it cannot actually reach with the intake
restricted.  Practically: "100 % duty" is ~98 % of real speed, and any projection that quotes 1771 RPM at full duty is
about 2 % optimistic.  Not worth recalibrating - redo it if the front is ever off for another reason, so the number
reflects the machine as it runs.

### Curve shapes, and the decision to allow an 80 C ceiling

`CC`'s CPU curve exists in three shapes; the ceiling is the safety-relevant parameter and is checked, not assumed.

| preset | shape | ceiling | note |
| --- | --- | --- | --- |
| `steep` | 2/10/22/38/55/72/90/100 at 40..75 | **75 C** | what shipped after the 55 %-ceiling defect |
| `gentle` | 15/15/22/34/52/72/88/100 | 75 C | higher floor, half the slope in the idle band; steeper than `steep` at 68-75 because the endpoint did not move |
| `top80` | 2/10/22/38/55/70/80/90/100 at 40..80 | **80 C** | the top end eased: 80 % at 70 C instead of 90 %, full speed at 80 C |

`check_cpu_ceiling()` refuses any curve that does not reach 100 %, and refuses a ceiling other than 75 C or an explicitly
accepted 80 C.  Accepting 80 C rests on measurement rather than taste: two minutes of all-core `matrixprod` held
**77-81 C at 4300 MHz** with the fan already at 100 %, so there is roughly 5 C of headroom above the observed peak
before throttling, and the curve still reaches full speed at the temperature the CPU actually reaches under load.  The
default stays 75 C, and a preset cannot raise its own ceiling silently - `PRESET_CEILING` ties each shape to the ceiling
its guard checks.

### The radiator pair oscillated, and the fix was filtering the sensor

Found by accident while checking the CPU half of the curve.  At **no load**, sampled once a second, the CPU package
sensor swung 13 C (47..60) and reversed direction 26 times in 45 s - a ~3 s oscillation.  With a curve carrying 12-17
duty points per degree, that jitter became visible fan hunting:

| | raw sensor | 20 s EMA |
| --- | --- | --- |
| pwm turning points in 45 s | 24 | **9** |
| mean swing between turns | 10.1 duty points | **1.6** |
| largest single swing | 20 points | **3** |

Nothing was damping it: `[functions]` held only the `Identity` default on every channel.  CoolerControl's own answer is a
custom sensor of `cs_type ExponentialMovingAvg` (or `TimeAverage`), window 1-300 s; the smoothed CPU package temperature
was created in the GUI and `CPU (radiator)` was repointed at it (`ccrad smooth --sensor ...`).  The GPU member and the
case-fan mix still read their raw sources.

**Measured settling, correcting the daemon's own tooltip.**  The UI claims an EMA "takes roughly 3x the window length to
fully follow a sustained change".  A step load (100 s of `matrixprod`) with the smoothed value recovered by inverting the
curve gave: 50% of the rise at **+6 s**, 90% at **+16 s**, for a 20 s window - roughly 1x, not 3x.  Two caveats: the
estimate is coarse (inverting the curve gives about 0.5 C) and it **clips at 100% duty / 75 C**, so 16 s is a floor on
the true 90% time.  Either way it is not 60 s, and 20 s does not need shortening.

The same run re-checked the safety property the earlier 55%-ceiling defect was fixed for:

| t | raw CPU | duty |
| --- | --- | --- |
| +21 s | 63 C | 67% |
| +33 s | 75 C | 90% |
| +41 s | 75 C | **100% (1771 RPM)** |
| +57 s | 80 C | 100% |

`stress-ng --cpu 20 --cpu-method matrixprod`, 100 s, peak 80 C.  The pair reaches full speed ~18 s after the load starts
and holds the CPU at 75-80 C.  Before the ceiling fix it could not exceed 55% duty at any temperature.

Note: `stress-ng --cpu-method matrix` is **not** a valid method on 0.19 (use `matrixprod` or `all`); the first attempt
silently loaded nothing, which is how the oscillation was spotted in the first place.

### Which memory chip runs hottest

Per-chip VRAM sensors are exposed through NVAPI (`lact cli stats`), 24 of them: 12 chips x Front/Back.  Steady-state
values from the two matched runs, with only the 4070's fan differing:

| chip | 4070 @ 30 % | 4070 @ 80 % | change |
| --- | --- | --- | --- |
| **D1 Back** | **76.0 C** | **72.8 C** | -3.2 |
| B0 Back | 76.0 C | 71.6 C | -4.4 |
| C1 Back | 74.4 C | 72.4 C | -2.0 |
| E0 Back | 76.0 C | 71.2 C | -4.8 |
| C0 Back | 74.0 C | 70.8 C | -3.2 |
| hottest *front* chip (C0/E0) | 70.0 C | 68.4 C | -1.6 |

**D1's back sensor is hottest in both runs**, so it is not a single-run artifact; averaged over the pair the worst three
are D1 Back (74.4 C), B0 Back (73.8), E0 Back (73.6).  No front-side chip reaches the top ten.

Aggregated by side, which is the number that matters for the backplate argument:

```
all Back  chips mean:  73.5 -> 69.8 C   (-3.7)
all Front chips mean:  67.9 -> 65.9 C   (-2.0)
```

The 4070 improved the back side **1.9x as much as the front**, which is the direct confirmation that its fans are
working on the back of the board and not merely stirring the case.  The problem being solved is entirely a back-side
problem; front-side temperatures are almost bystanders.

Caveat: the letters are NVAPI identifiers.  NVIDIA does not document which physical package each maps to, so "D1" is
reproducible but not locatable - it cannot be tied to a position on the PCB without a board diagram.

### O10, revised: trigger on D1 Back, because the aggregate sensor gives up

The original O10 sketch watched the aggregate VRAM temperature.  The per-chip data kills that idea: the aggregate sensor
flattens at 74 C while individual back-side chips are still climbing, so a trigger built on it goes quiet exactly when
it is needed.  D1 Back is hottest in both matched runs (see "Which memory chip runs hottest") and keeps moving last.

**Trigger design:**

| element | choice | why |
| --- | --- | --- |
| primary sensor | **D1 Back** | hottest in both runs; the last sensor still rising at plateau |
| fallback if D1 is missing/glitched | mean of the four hottest back chips (D1, B0, C1, E0) | one bad sensor should not disable cooling |
| on threshold | D1 Back >= 70 C | below the 72.8 C plateau, so it engages before the plateau is reached |
| off threshold | D1 Back <= 64 C | 6 K deadband, comfortably more than sensor noise |
| minimum on-time | 90 s | stops hunting when the load oscillates between requests |
| suppression | skip while the 4070 draws > 40 W | when it is busy its own fans already spin; overriding driver fan management there is how you get instability |
| speed | 55-65 % ***unmeasured*** | 80 % was audibly unacceptable; the knee has never been found (see O12's sweep) |
| fail-safe | restore the 4070's previous settings on exit, including signal/power loss | a daemon that dies while boosting must not leave the card loud forever |

**Why it is not built yet.**  Two real blockers, neither of them coding effort:

1. **How the daemon should write fan speed is unsettled.**  Two paths exist.  (a) Edit `/etc/lact/config.yaml` and
   restart `lactd` - what `tools/lact/fan_pin.py` does, with backup, verification and rollback.  It works, but a daemon
   bouncing the GPU daemon around inference is a poor shape for something that may run hourly.  (b) Drive LACT's own
   socket API, which is how the GUI changes fan settings live with no restart.  The socket is `root:sudo` and speaks a
   JSON command protocol on a Unix socket; the protocol is undocumented here and would need discovery, but if (b) works
   it removes the restart problem entirely and makes the agent a plain user-service.  Worth half an hour before
   committing to (a).
2. **The speed knee is unmeasured.**  Choosing 80 % by hand was a mistake we have already paid for once.

**Verification, and why it matters here.**  `temperature_key` cannot be trusted on this machine: the 3090's config has
said `vram` all along while the fan demonstrably follows `edge`.  So whenever a curve is changed, confirm what it is
actually keyed on rather than what the YAML says:

```
python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/lact/which_sensor.py --gpu 3090 --seconds 120
```

It samples the fan against edge, hotspot, aggregate VRAM and all 24 per-chip sensors, evaluates the configured curve at
each, and reports which candidate's prediction matches the fan.  It needs real load - it says so instead of pretending
when nothing moves.  First result, on the unmodified config: **fan tracks `edge`; config claims `vram`** (best-fit error
edge ~6 %, against hotspot ~20 % and aggregate VRAM ~19 %).

### The mixes (`tools/coolercontrol/mix_profiles.py`) — applied 2026-10-20

A VRAM-only curve is wrong for a **shared** case fan: at CPU-only work the GPU is cool, so a VRAM-only curve lets an
exhaust fan drop to ~50 % while nine CPU workers generate heat — `fan6`'s old BIOS pin was 70 %.  A Mix Profile
computes both curves from their own sources and takes the higher duty, so no fan has to decide which component it
serves.

`fan3`, `fan4`, `fan6` → `Case Fans (CPU or VRAM)` = max of the CPU profile and the VRAM exhaust curve.
`fan5` stays VRAM-only: its job is the GDDR6X and it is the only unobstructed intake.  `fan1` stays on its BIOS
curve — it was never calibrated, and stalling two AIO radiator fans on an unknown duty floor is not a risk worth
taking for a marginal gain; calibrate it first if the soak says we need it.

The CPU profile is **discovered, not hard-coded**: the script looks for the single Graph profile whose temperature
source is not the VRAM File sensor, and refuses if there is none — or lists what it saw and refuses if there is more
than one.  Tested on a copy of the live config: refuses before O4 exists, discovers it after, replaces `fan6`'s
existing assignment instead of duplicating the key (a duplicate key is invalid TOML and would take the config
offline), rejects an ambiguous second Graph profile, backup-first, rollback on `coolercontrold check` failure,
revert byte-for-byte.

```bash
sudo systemctl stop coolercontrold
sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/mix_profiles.py plan
sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/mix_profiles.py apply --start
# undo:
sudo systemctl stop coolercontrold
sudo python3 /home/mike/Sync/Projects/StrataUnbiasedDualGPU/UnbiasedStrata/tools/coolercontrol/mix_profiles.py revert
sudo systemctl start coolercontrold
```

## Verification checklist (1–4 done: exporter, sensor graph, UI channel states, load test and soak)

1. `journalctl -u vram-sensor -n 20` → `... <- 72.0 C`, and `/run/vramsensor/vram.temp1` tracks it.
2. CoolerControl Home: the `3090 VRAM` sensor charts, and matches LACT's `VRAM` reading.
3. Run a 2000-token generation and watch: the intake fans climb within ~30 s, LACT shows **VRAM under 80 °C**, and
   `sensors` shows the CPU fans unaffected.
4. Idle for 5 minutes: VRAM ~55-60 °C and the case fans drop toward the floor.  If they stay high, the BIOS curve is
   still in charge (step 6).
5. Kill the exporter (`sudo systemctl stop vram-sensor`) for a minute: CoolerControl must show the sensor as
   failed and push those channels to full - that is the failsafe working, and it is the behaviour you want from a
   dead sensor.
6. Summer acceptance test: at ambient ~30 °C the target is **VRAM under 88 °C sustained** with the fans allowed to
   reach 100 %.  If it cannot hold that, use the levers below before buying anything.

## Levers, in order of preference

1. **Case airflow aimed at the backplate** (above) - free, and the memory chips are on the back of the PCB.
2. **`power_cap`**: the card draws 290-298 W against a 300 W cap (`cap_default` 350, max 365).  275 W removes ~8 %
   of the heat for a small throughput cost - measure it in summer rather than guessing now.
3. **The card's own curve** (already VRAM-keyed; top point 100 % at 80 °C).
4. **A board thermistor** (`AUXTIN`) - the simplest control loop, and it removes the exporter.
5. **Physical**: a fan aimed directly at the backplate beats another fan elsewhere in the case.

## Limits and traps

- Driver-reported core limits on this card: slowdown 95 °C, shutdown 98 °C, max operating 93 °C.  GDDR6X is fine to
  ~95 °C and thermal-throttles above that; 86 °C sustained is acceptable, 100 °C is not the moment to experiment.
- `pwmconfig` (from `lm-sensors`) stops each fan for ~5 s to learn the mapping.  Do not run it while the engine is
  loading a 300 W card.  CoolerControl's per-channel pages and `sensors` output identify channels without stopping
  anything; CoolerControl also has real fan **Calibration** if the curves need to match the fans' true range.
- `nvidia-smi -q -d TEMPERATURE` reports `Memory Current Temp: N/A` on this card/driver - that is why the GPU
  temperature tools that rely on that query cannot key on VRAM, and why LACT (NvAPI) is the sensor source here.
- Two controllers on one header is how fans end up hunting.  One owner per channel.
