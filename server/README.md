# server - SenseThrough live dashboard (demo layer)

A thin **presentation layer** over the tested `wisp` detection pipeline: a Flask bridge +
a self-contained dashboard that turns the one-line alert console into a live room monitor
with a **fall alert + escalation** UI, suitable for a screen-recorded demo.

It is intentionally **separate from the core**. The core stays console-first (see the main
README); this is the "make it visible for the pitch" layer and does not change any
detection logic - it consumes `wisp.pipeline.detection_telemetry`, the same path the
evaluation harness uses, so the demo can never diverge from what is measured.

## What it shows

- **MONITORING vs FALL ALERT** hero that flips the whole screen red on a confirmed collapse.
- An always-visible **source badge**: green **LIVE · ESP32** when a board is streaming, amber
  **FALLBACK · …** otherwise. The fallback is never silent - it is labelled on screen.
- A live **activity meter + sparkline**, current room state, and detector thresholds.
- A cancellable **escalation countdown** ("contacting emergency contact") that resolves to
  *notified* if nobody cancels, or back to monitoring if you press **I'm OK**.

## The source fallback chain

On startup the engine walks this chain and reports which rung it landed on:

1. **LIVE ESP32** - if a serial port (auto-detected `/dev/ttyUSB*`/`ttyACM*`, or `--serial`)
   actually emits `CSI_DATA` within `--probe-s` seconds. Calibrates on the room's own live
   normal (`--calibrate-s`) if no `room_profile.pkl` exists.
2. **Real-data replay** - `--csi-bench PATH` (real captured CSI, needs the Kaggle subset) or
   `--replay file.csv` (a recorded `RawLogger` log).
3. **Synthetic demo room** - always available, self-contained, correct. The guaranteed floor.

## Run it (inside the WSL venv, from the repo root)

> On this machine the pipeline runs in **WSL Ubuntu**, not Windows Python (Windows Smart App
> Control blocks scipy/sklearn/h5py native DLLs). Open http://localhost:8000 in any browser.

```bash
source ~/wisp-venv/bin/activate

# Guaranteed software demo (no hardware) - amber FALLBACK badge:
python server/app.py --no-serial --room "Washroom 3B"

# Auto: use the ESP32 if it's streaming, else fall back automatically:
python server/app.py --room "Washroom 3B"

# Point at a specific board (still falls back if no CSI arrives):
python server/app.py --serial /dev/ttyUSB0 --room "Washroom 3B"

# Real captured CSI (after downloading the CSI-Bench fall subset, see below):
python server/app.py --no-serial --csi-bench /path/to/FallDetection --room "Washroom 3B"
```

Useful flags: `--speed` (fallback playback speed; live is always real-time),
`--escalate-s` (countdown length), `--no-loop` (play the fallback once), `--port`,
`--contact "Daughter - Priya"`, `--rate`.

## HTTP API (for an external dashboard too)

CORS is enabled, so a dashboard you host elsewhere can poll the same endpoints.

| Method | Path | Purpose |
| --- | --- | --- |
| GET  | `/`        | the dashboard HTML |
| GET  | `/status`  | JSON snapshot (poll ~2 Hz): mode, source label, room state, alert phase, countdown |
| POST | `/cancel`  | "I'm OK" - cancel an in-progress escalation |
| POST | `/reset`   | clear alert/resolved state |
| GET  | `/healthz` | liveness |

## Enabling real CSI-Bench data (optional)

The code path is ready (`--csi-bench`), but the dataset needs your Kaggle credentials:

1. Kaggle → Account → **Create New API Token** → download `kaggle.json`.
2. In WSL: `mkdir -p ~/.kaggle && cp /mnt/c/…/kaggle.json ~/.kaggle/ && chmod 600 ~/.kaggle/kaggle.json`
3. `kaggle datasets download -d guozhenjennzhu/csi-bench -p ~/csi-bench --unzip`
4. Point the server at the fall subset directory: `--csi-bench ~/csi-bench/…/FallDetection`
   (first run `CSIBenchSource(path).list_datasets()` to confirm the in-file layout).

This is a *real-CSI code sanity check*, not the Phase-0 gate (which is a weeks-long
false-alarms/week number in one real room). See `wisp/source/csi_bench_source.py`.
