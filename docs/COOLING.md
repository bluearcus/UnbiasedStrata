# Cooling: the 3090's GDDR6X temperature, and what drives the fans

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

## To do: CoolerControl for the case fans (needs root)

```bash
# 1. install (Debian 13 trixie is supported: >= Bookworm)
sudo apt install curl apt-transport-https
curl -fsSL https://apt.coolercontrol.org/setup.sh | sudo -E TREE=debian sh
sudo apt update
sudo apt install coolercontrol lm-sensors          # coolercontrold comes with it
sudo systemctl enable --now coolercontrold

# 2. the sensor bridge
sudo cp tools/vram_sensor.py /usr/local/bin/vram-sensor && sudo chmod 0755 /usr/local/bin/vram-sensor
sudo cp tools/vram-sensor.service /etc/systemd/system/
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

## Verification checklist

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
