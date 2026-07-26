# SenseThrough — Live System Documentation

**Through-wall-agnostic fall detection over Wi-Fi CSI, using two ESP32 boards.**

This document covers the **live, running system** end to end: how it works, how the two
ESP32s are brought up, how detection is calibrated and tuned, where to place the boards,
and how to recover when the radio link misbehaves. It complements — not replaces — the
top-level [`README.md`](../README.md) (software architecture / the "trust the alerts"
gate) and [`server/README.md`](../server/README.md) (the dashboard demo layer).

> **TL;DR** Two ESP32s form a Wi-Fi link across a room. A person moving in that link
> perturbs the Channel State Information (CSI). We turn that into a `motion` signal, learn
> the room's "still" and "active" levels, and fire a **fall alert** when a burst of
> sustained activity is followed by persistent stillness. A Flask dashboard shows it live.

---

## 1. What it is and how it works

### 1.1 The physics in one paragraph

Wi-Fi packets travel from a **transmitter (TX)** ESP32 to a **receiver (RX)** ESP32. The
RX measures **Channel State Information** — the amplitude/phase of each OFDM subcarrier,
i.e. how the radio channel distorted the signal on the way across. Anything that moves in
the space between the boards changes the multipath and therefore changes the CSI. A person
walking wobbles it a lot; a still room barely changes it. **The two boards are the sensor's
frame of reference** — the measurement is entirely relative to their fixed positions.

### 1.2 From radio to alert (the signal chain)

```
 TX ESP32 ──Wi-Fi──▶ RX ESP32 ──USB serial──▶ parser ──▶ preprocess ──▶ features
 (active_sta)        (active_ap)               CSI_DATA    clean/mask     motion, sharpness
                                                                              │
                                                                              ▼
                            dashboard ◀── engine ◀── state machine ◀── RoomProfile thresholds
                            (Flask)      snapshot     NORMAL→DISTURBANCE      (calibrated)
                                                      →STILL→CONFIRMED
```

Two instantaneous features are extracted from each short (~1 s) window
([`wisp/features/extract.py`](../wisp/features/extract.py)):

- **motion intensity** = mean over subcarriers of the *temporal variance* within the
  window. High while someone moves, near-zero when the room is still. **This is the
  workhorse signal.**
- **transient sharpness** = the largest single-step change between consecutive packets.
  Spikes on a sudden event (the "thud" of an impact).

A **fall** is not a single big number — it is a *pattern over time*:

```
   someone active  →  a disturbance  →  stillness that PERSISTS ≥ T seconds
```

That temporal logic lives in the state machine (§5) and is what keeps false alarms down.

---

## 2. Hardware

| Role | Firmware (ESP32-CSI-Tool) | Serial node (WSL) | What it does |
| --- | --- | --- | --- |
| **RX / AP** | `active_ap` | `/dev/ttyUSB0` | Receives packets, **emits `CSI_DATA` lines** we read |
| **TX / STA** | `active_sta` | `/dev/ttyUSB1` | Associates to the AP and blasts packets (no CSI out; just logs) |

- Two ESP32 dev boards with **CP2102 USB-UART** bridges, two data USB cables, a laptop.
- Firmware: [ESP32-CSI-Tool](https://github.com/StevenMHernandez/ESP32-CSI-Tool) built with
  **ESP-IDF v4.3**. The critical sdkconfig flags:
  - `CONFIG_ESP32_WIFI_CSI_ENABLED=y` — **without this the board reboot-loops** on
    `esp_wifi_set_csi` (this was the first hardware bug we hit).
  - `CONFIG_PACKET_RATE=100` — target packet rate.
- Flash both boards (over USB/IP into WSL) at a **conservative baud** — 460800 timed out;
  `-b 115200` is reliable:
  ```bash
  idf.py -p /dev/ttyUSB0 -b 115200 flash    # active_ap
  idf.py -p /dev/ttyUSB1 -b 115200 flash    # active_sta
  ```

### 2.1 Getting the boards into WSL (usbipd)

The Python stack runs in **WSL Ubuntu** (see §7.1), so the Windows-side USB devices must be
forwarded in with [usbipd-win](https://github.com/dorssel/usbipd-win):

```powershell
usbipd list                              # find the two CP2102 busids (e.g. 1-2, 1-3)
usbipd bind   --busid 1-2                # once, needs admin/UAC (persists across reboots)
usbipd bind   --busid 1-3
usbipd attach --wsl --busid 1-2          # after every boot/sleep (does NOT persist)
usbipd attach --wsl --busid 1-3
```

Then in WSL they appear as `/dev/ttyUSB0` and `/dev/ttyUSB1`. **The attach is lost whenever
the laptop sleeps or reboots** — re-attach is the #1 cause of "it stopped working" (§7.3).

---

## 3. The live 2-board reader

Implemented as `_LiveTwoBoard` in [`server/engine.py`](../server/engine.py). Three details
matter, all learned the hard way:

1. **Hold BOTH ports open, in run-mode.** Opening a serial port toggles the CP2102's
   DTR/RTS lines, which resets the ESP32. If the two boards reset at *different* times the
   Wi-Fi link desyncs and the CSI rate collapses to ~1 Hz. Opening **both** in run-mode
   (`DTR=False, RTS=False` before and after `open()`) keeps them booted together and the
   rate stable (~12 Hz when the link is healthy). The RX is the one we read; the TX
   ("companion") is just **held open** to keep the link alive — hence `--companion`.
2. **One open, shared by calibration and detection.** Calibration and the detection loop
   read from the *same* already-open stream (via `_ListSource`/`_GenSource`) so we never
   re-open (and thus never re-reset) mid-run.
3. **Lock the subcarrier width, and high-pass the amplitude.** ESP32 CSI packets arrive
   with varying subcarrier counts (HT20/HT40, LLTF vs HT-LTF). We lock onto the **dominant
   width** from the first 40 packets and skip odd-width ones (otherwise the fixed-width room
   mask `IndexError`s and the loop dies). Each packet's amplitude is divided by a **slow EMA
   baseline** (`baseline = 0.97·baseline + 0.03·mean`), a high-pass that cancels the radio's
   automatic-gain drift while preserving the fast changes a moving body causes.

---

## 4. Calibration — learning the room

Thresholds are **not** global constants; they are learned from the room's own signal
([`wisp/calibrate/profile.py`](../wisp/calibrate/profile.py), `RoomProfile.fit`). On a live
start the engine collects ~`--calibrate-s` seconds of normal activity and derives:

- **still** — motion below this = "still"
- **occupied** — motion above this = "active / someone present"
- **sharp** — sharpness above this = "an impact"
- plus the subcarrier **mask** and an **IsolationForest** anomaly model.

Two ways to set the three lines:

| Mode | How | When to use |
| --- | --- | --- |
| **Adaptive** (default) | Percentiles of the calibration signal: still **p25**, occupied **p80**, sharp **p97** | Normal case — **re-learns each placement automatically**. Move around normally during calibration so it sees both stillness and motion. |
| **Absolute overrides** | `--still X --occupied Y --sharp Z` bypass the percentiles | When you've *measured* the floor and want to pin exact values (e.g. a very noisy spot). |

> **Why the percentiles are p25/p80, not p10/p90.** An early bug set still at the 10th
> percentile of a *moving* calibration, which landed the still-line **below** the room's
> resting noise floor. The meter then read "still moving" even when quiet, the machine
> latched in DISTURBANCE forever, and no fall could ever confirm. Raising the still
> percentile puts the line safely above the noise floor. If you ever see it stuck at
> "disturbance / never still", that's this failure — recalibrate or raise `--still`.

**A healthy calibration shows clear separation**, e.g. `still 0.0045 → occupied 0.0404`
(≈ 9× — an excellent spot) versus `still 0.013 → occupied 0.047` (≈ 3.4× — usable but
noisy). Rule of thumb: **occupied ÷ still ≥ ~2×** is the minimum for reliable detection.

---

## 5. Detection — the state machine

[`wisp/detect/state_machine.py`](../wisp/detect/state_machine.py). States cycle
`NORMAL → DISTURBANCE → STILL → CONFIRMED`. Two collapse paths:

- **Sudden collapse** — sustained activity, then a **sharp** disturbance, then stillness
  that persists ≥ `confirm_s`.
- **Slow collapse** — the room was recently occupied, motion declines with **no** sharp
  transient, and stillness persists ≥ `slow_confirm_s`.

Guards that keep it honest:

| Guard | Param | Purpose |
| --- | --- | --- |
| **Sustained-activity gate** | `min_active_s` | A collapse requires this much *real* activity first. A 1-second twitch or a lone noise spike followed by the room's normal quiet **cannot** confirm a fall. This is the primary false-alarm killer on a noisy live signal (verified: 0 false alerts over 75 s idle, where it previously fired continuously). |
| **Median smoothing** | `--smooth N` | Features are median-filtered over N windows before the machine sees them, rejecting isolated noisy packets. Default 9 live. |
| **Debounce / hysteresis** | `debounce_s` | After an alert, stays quiet until motion clearly resumes — no re-fire spam. |

On a confirmed collapse the engine starts a **cancellable escalation countdown**
(`--escalate-s`, default 30 s). If nobody presses **I'm OK** it resolves to "notified".

---

## 6. Running it live (the full playbook)

From the repo root, inside the WSL venv:

```bash
source ~/wisp-venv/bin/activate

python server/app.py \
  --serial /dev/ttyUSB0 --companion /dev/ttyUSB1 --baud 115200 \
  --rate 12 --calibrate-s 15 --smooth 9 \
  --confirm-s 8 --min-active-s 2 \
  --room "Washroom 3B" --escalate-s 30
```

1. Both ESP32s reset when the ports open; the link re-forms in ~15 s.
2. **Move around** between the boards during the `--calibrate-s` window so it learns both
   still and active levels.
3. Open the dashboard: `http://localhost:8000` (or the WSL IP, e.g.
   `http://172.18.105.193:8000`, if Windows owns port 8000 — see §7.2).
4. **Test a fall:** move clearly/vigorously ~3–4 s → drop → lie still ~8 s. Watch
   `DISTURBANCE → STILL → CONFIRMED`, then the escalation countdown.

### 6.1 CLI reference (live-relevant)

| Flag | Default | Meaning |
| --- | --- | --- |
| `--serial` | autodetect | RX board port (the one emitting `CSI_DATA`) |
| `--companion` | none | TX board port to **hold open** so the link stays up |
| `--baud` | 921600 | serial baud (use **115200** for these boards over USB/IP) |
| `--rate` | 50 | nominal sample rate (use ~**12** for the live link) |
| `--calibrate-s` | 20 | seconds of live normal to fit the profile |
| `--still / --occupied / --sharp` | none | absolute threshold overrides (skip percentiles) |
| `--confirm-s` | 8 | stillness needed to confirm a **sudden** collapse |
| `--min-active-s` | 3 | sustained activity required before any collapse counts |
| `--smooth` | 9 | median-filter features over N windows (1 = off) |
| `--escalate-s` | 30 | escalation countdown length |
| `--room` / `--contact` | — | labels shown on the dashboard |
| `--no-serial` | — | skip the ESP32, force the software fallback |
| `--csi-bench / --replay` | — | real-data fallback sources |

### 6.2 The activity meter (display)

The dashboard "activity level" spreads across the **whole movement range** (still → clearly
active), not just the narrow still→occupied band, so it reads *gradually* 0–100 % instead
of snapping between the two. Concretely the bar normalizes motion over
`still … still + 5·(occupied − still)`. This is **display only** — it does not affect
detection thresholds.

---

## 7. Operations & troubleshooting

### 7.1 Why everything runs in WSL, not Windows Python

Windows **Smart App Control** blocks the native DLLs behind scipy / scikit-learn / h5py
("Application Control policy has blocked this file"). The whole pipeline therefore runs in
**WSL Ubuntu** (venv at `~/wisp-venv`). The boards are forwarded in with usbipd (§2.1).

### 7.2 "Dashboard won't open / 404 on localhost:8000"

If a separate Windows process owns port 8000, `localhost:8000` from Windows may 404. Use the
**WSL IP** instead (`ip addr` in WSL, or the address the server prints, e.g.
`http://172.18.105.193:8000`).

### 7.3 "It stopped working" — boards detached

USB/IP forwarding drops on sleep/reboot. Symptom: no `/dev/ttyUSB*` in WSL, dashboard frozen.
Fix: re-attach (§2.1). If `usbipd list` shows them **Shared** but WSL sees 0 devices, just
re-run the two `usbipd attach --wsl` commands.

### 7.4 "Stuck calibrating, 0 packets" — flaky re-link

Sometimes after a port reset the two boards don't re-form the link (calibration hangs with
`packets=0`, no error). A soft restart may not fix it. The reliable **hard reset**:

```bash
# 1) free the serial ports in WSL
fuser -k /dev/ttyUSB0 /dev/ttyUSB1
```
```powershell
# 2) power-cycle the USB forwarding
usbipd detach --busid 1-2 ; usbipd detach --busid 1-3
# 3) if a stale ttyUSB node lingers, fully clear WSL's USB state:
wsl --shutdown
# 4) re-attach both
usbipd attach --wsl --busid 1-2 ; usbipd attach --wsl --busid 1-3
```

Then restart the server. Physically unplugging/replugging both boards also works. A direct
probe (open both ports, wait ~18 s, count `CSI_DATA` lines) confirms whether CSI is flowing
before you bother restarting the whole server.

### 7.5 "Detecting falls randomly" (false alarms)

Cause: brief blips + the room's normal quiet look like activity→stillness. Fixes, in order:
raise `--min-active-s` (require more genuine activity first), raise `--smooth`, and make sure
the still-line sits above the noise floor (§4). Verified effect: from "firing continuously"
to **0 false alerts in 75 s** idle.

### 7.6 "Stuck at 100 % / never goes still" (can't detect)

The still-line is **below** the resting noise floor, so the meter never reads "still" and no
fall can confirm. Measure the true resting floor (stand still ~15 s, watch the motion value)
and set `--still` just above its peak — or recalibrate with the p25 still percentile.

---

## 8. Board placement (this decides everything)

No amount of threshold tuning helps if the geometry gives no signal. **Moving must read
clearly higher than still.**

```
        2–3 m apart, facing each other, clear line between them
   [ESP-TX] · · · · · · · · person on THIS line · · · · · · · · [ESP-RX]
     ~1 m high (waist/chest)                                ~1 m high, same height
```

- **2–3 m apart**, facing each other, antennas vertical, nothing touching the boards.
- **Waist–chest height, both equal** — maximizes the standing→fallen difference.
- **The person must be on the line between the boards.** Movement off to the side or behind
  a board barely registers. A fall *across* the link gives the biggest signal.
- Away from fans, spinning metal, and other constant movers.
- **Never move or touch the boards during monitoring.** Moving a board changes the whole
  channel — a bigger signal than any person — and *setting it down* looks exactly like
  "activity → stillness", i.e. a false fall. If you reposition, **recalibrate**.

Use the dashboard as a live **signal meter** while placing: a good spot shows near-0 % when
still and a big swing when you cross the line. Measured separations: ≈ **9×** (excellent),
≈ **3×** (usable), ≈ **1×** (no usable signal — reposition).

---

## 9. The fallback chain & dashboard API

The same server powers the live demo and a guaranteed software fallback. On startup the
engine walks a chain and **always shows which rung it landed on** (LIVE badge vs FALLBACK):

1. **LIVE ESP32** — a serial port actually emitting `CSI_DATA`.
2. **Real-data replay** — `--csi-bench PATH` or `--replay file.csv`.
3. **Synthetic demo room** — always available; the guaranteed floor.

HTTP API (CORS enabled): `GET /` (dashboard), `GET /status` (JSON snapshot, poll ~2 Hz),
`POST /cancel` ("I'm OK"), `POST /reset`, `GET /healthz`. See
[`server/README.md`](../server/README.md) for details.

---

## 10. Honest limitations

- **Single-person sensor.** One TX→RX link measures the *sum* of all motion in the channel.
  It cannot separate one faller from a crowd — with several people moving, the room never
  goes quiet, so a collapse can't confirm. Design assumption: one monitored person.
- **"Fell" vs "walked away" ambiguity.** Both are activity → stillness. The `min_active_s`
  gate and the sharp-impact requirement cut this down a lot, and the **I'm OK** button
  cancels the rest, but it is inherent to a single link.
- **Placement- and noise-sensitive.** A high resting-noise spot gives a marginal signal.
  Reliable detection needs a clean placement with clear still/active separation.
- **Metrics on real hardware are still preliminary.** The formal "trust the alerts" gate
  (weeks of continuous operation, < ~1 false alarm/week) is defined in the main README and
  is the real bar; this document is the live-bring-up and tuning reference.

---

## 11. Repo map (live-relevant files)

```
server/
  app.py            Flask shell — routes + CLI flags
  engine.py         MonitorEngine, _LiveTwoBoard (2-board reader), fallback chain, escalation
  dashboard.html    self-contained UI (activity meter, alert/escalation, LIVE/FALLBACK badge)
wisp/
  features/extract.py     motion intensity + transient sharpness
  calibrate/profile.py    RoomProfile.fit — thresholds + mask + model (adaptive or absolute)
  detect/state_machine.py NORMAL→DISTURBANCE→STILL→CONFIRMED + min_active_s / debounce
  pipeline.py             detection_telemetry — the ONE detection loop (dashboard + harness)
  source/serial_source.py DTR/RTS-safe live serial reader
scripts/serial_check.py   DTR-safe serial diagnostic (is CSI streaming? parser-compatible?)
docs/SENSETHROUGH.md      this document
```

Run the test suite (20 tests, in WSL venv): `python -m pytest -q`.
