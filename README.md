# wisp

**Phase 0 MVP.** A program that watches a room via Wi-Fi Channel State Information
(CSI) — sensed by two ESP32 boards — and prints an alert when someone collapses,
plus an evaluation harness that measures whether those alerts can be *trusted*.

This is not a product. It has no polish, no app, no cloud. Its only job is to answer
one question and produce one number.

---

## The one question

> Can two ESP32s catch staged collapses in one real room **without spamming false alarms?**

Everything in this repo exists to answer that. The deliverable is not software — it is
a **trustworthy false-alarm-per-week number**, alongside proof that staged falls are
caught.

## The gate (pass / fail)

Over weeks of continuous operation in one real, occupied room:

| Metric | Target |
| --- | --- |
| Recall on staged sudden + slow collapses | catch **(nearly) all** |
| **False alarms per week** under real living conditions (cooking, fan, normal movement) | **< ~1** |
| Detection latency | reported, informational |

- **PASS** → the core idea is real; proceed to build the product on top.
- **FAIL** → learned cheaply (~$15, a few weeks) *before* raising money or quitting
  anything. This is a good outcome, not a bad one — it beats any amount of market research.

The second number — false alarms per week — decides everything. A fall detector that
also fires when a fan spins or the cat walks by is worse than useless: people mute it,
and a muted safety device saves no one.

---

## How it works (and what it does NOT need)

The shipping detector is an **Isolation Forest** — an *unsupervised anomaly detector*.
It trains on **one room's own "normal" in seconds, on CPU**. There are no epochs, no GPU,
no massive dataset.

- **No GPU is used for the MVP.** A GTX 1650 (or any card) sits idle the whole time.
  That is the point of the anomaly-detection approach, not a limitation.
- **No public dataset is required to ship.** [CSI-Bench][csi-bench] (461 hours, 35 users,
  26 environments) is a *multi-task benchmark corpus*, not training data for this MVP.
  You never download all of it.
- **The only scenario that touches a dataset or GPU** is the *optional* supervised
  benchmark (S5.4) — training a small 1D-CNN/LSTM on the fall subset to produce a
  "credibility number for investors." Even then: pull only the fall single-task subset
  (~6,700 samples, a few GB — not 461 hours), and a full run is well under an hour on a
  4GB card (batch 32–64; avoid transformers). This is explicitly **not** the shipping
  model.

## The core idea: one interface, hardware as a plug-in

The entire detection "brain" is built against a single interface —
[`CSISource`](wisp/source/base.py) — whose `.stream()` yields
`(timestamp: float, amplitude: np.ndarray)` tuples. Nothing downstream knows or cares
where the data comes from. The same brain runs on:

- **`SyntheticSource`** — a fake room. Build and test everything against this **now**,
  with zero hardware.
- **`ReplaySource`** — recorded log files. Deterministic; powers evaluation and doubles
  as a safe live-demo fallback.
- **`SerialSource`** — the **live** ESP32 serial stream. Written **last**, when the
  hardware is up.

**The payoff:** you build the whole pipeline before the hardware exists. When the ESP32s
finally stream, the only new code is `serial_source.py` (implement `.stream()`) and
`parser.py` (decode the real CSI line). Everything else already works and is already
tested. The hardware is a plug-in, not a dependency.

---

## Layout

Maps 1:1 to the MVP doc's S-sections.

```
WISP/                              ← git repo = project root
├── config/pipeline.yaml          # all params, versioned (S2.7)
├── wisp/                          # the Python package (import wisp)
│   ├── source/                   # S1.6 — the interface everything hides behind
│   │   ├── base.py               #   CSISource: .stream() -> (timestamp, amplitude[])  [CONCRETE]
│   │   ├── synthetic.py          #   fake room — build against this NOW
│   │   ├── replay.py             #   read logged files
│   │   └── serial_source.py      #   LIVE — write this LAST, when hardware streams
│   ├── ingest/                   # S1
│   │   ├── parser.py             #   CSI_DATA line -> amplitude array
│   │   └── logger.py             #   raw logger to disk (S1.5)
│   ├── preprocess/clean.py       # S2 — mask dead subcarriers, Hampel, band-pass, windows
│   ├── features/extract.py       # S3 — motion, sharpness, stillness
│   ├── calibrate/profile.py      # S4 — RoomProfile: mask, thresholds, model
│   ├── detect/
│   │   ├── model.py              # S5 — IsolationForest anomaly model
│   │   ├── rules.py              # S5.2 — sudden vs slow discriminators
│   │   └── state_machine.py      # S6 — temporal logic, THE false-alarm killer
│   └── evaluate/harness.py       # S9 — recall, false-alarms/week, latency  ← the deliverable
├── scripts/
│   ├── calibrate.py              # fit a room profile from a recording
│   ├── run_live.py               # detection loop -> one-line debug console
│   └── evaluate.py               # replay + metrics
└── tests/test_features.py        # S3.7 — features on a known sine/step
```

Only `source/base.py` is implemented (the abstract `CSISource`). Every other module is
a documented stub raising `NotImplementedError`, filled in one at a time.

---

## Module reference

| Module | S-section | What it does |
| --- | --- | --- |
| `source/base.py` | S1.6 | Abstract `CSISource.stream()` → `(timestamp, amplitude[])`. The one contract everything hides behind. **Done.** |
| `source/synthetic.py` | S1.6 | Fake room. Emits **labeled** sequences: empty / walking / sudden collapse / slow collapse / optional periodic fan. |
| `source/replay.py` | S1.6 | Replays a recorded log through the identical interface. Deterministic. |
| `source/serial_source.py` | S1.6 | Live CSI from the RX ESP32 over pyserial. The only file that touches hardware. Written last. |
| `ingest/parser.py` | S1 | Parse a `CSI_DATA` serial line → per-subcarrier amplitude array (`sqrt(i²+q²)`). Pure, unit-testable. |
| `ingest/logger.py` | S1.5 | Raw CSI logger to disk, continuous. **Do not skip** — every hour logged early is irreplaceable data and your demo safety net. |
| `preprocess/clean.py` | S2 | Drop null/guard + dead subcarriers, Hampel outlier rejection, band-pass. Amplitude only (phase skipped for MVP). Rolling short (~1s) + long (~3–5s) windows. |
| `features/extract.py` | S3 | Three features: **motion intensity** (cross-subcarrier variance), **transient sharpness** (max first-difference), **stillness duration** (counter). |
| `calibrate/profile.py` | S4 | `RoomProfile` fit from hours of one room's "normal": subcarrier mask, percentile thresholds, fan/HVAC notch, trained model. One room, one profile. |
| `detect/model.py` | S5 | IsolationForest over the feature vectors (fastest anomaly detector to get running; CPU, seconds). |
| `detect/rules.py` | S5.2 | Discriminators: transient→stillness = sudden fall; gradual decline→prolonged stillness = slow collapse. Notches periodic sources. |
| `detect/state_machine.py` | S6 | Temporal logic: requires a *pattern over time*, not one anomalous window. `disturbance → stillness ≥ T → CONFIRMED`, with debounce/hysteresis. Keeps an audit log of transitions. **The false-alarm killer — do not skip.** |
| `evaluate/harness.py` | S9 | Replays labeled recordings through the exact live pipeline; computes recall, false-alarms/week, latency. **The actual deliverable.** |

---

## Build order (all of 1–6 need zero hardware)

```
synthetic.py  ──►  parser/logger (S1)  ──►  clean (S2)  ──►  extract (S3)
                                                                  │
                                          ┌───────────────────────┘
                                          ▼
                          profile (S4) ──► model + rules + state_machine (S5, S6)
                                                                  │
                                                                  ▼
                                                         harness (S9) ← the deliverable
```

1. `source/base.py` + `source/synthetic.py` — data to work with immediately.
2. `ingest/parser.py` + `logger.py`.
3. `preprocess` + `features` + the unit tests (turn the 3 `xfail`s green — first real milestone).
4. `calibrate`.
5. `detect` (model → rules → state machine).
6. `evaluate/harness.py`.
7. **Last, when hardware is up:** `source/serial_source.py`.

### Independent tracks (what can be parallelized)

Each depends only on a *data shape*, not another module's internals, so all can proceed
at once given people to do them:

1. **Source/data** — synthetic, replay, logger
2. **Preprocess** — clean (testable on hand-made arrays)
3. **Features** — extract (testable on a pure sine / step, no source needed)
4. **Detection** — model, rules, state machine (feed it fake feature dicts)
5. **Evaluation** — harness (stub the detector)
6. **Hardware** — firmware (H3) → serial_source, parser (the long pole)

The three worth parallelizing early: **source/synthetic**, **features**, and **hardware**.

---

## Setup

```
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate on Unix
pip install -r requirements.txt
```

Run it end-to-end on the synthetic room (zero hardware):

```
python scripts/calibrate.py     # learn this room's normal -> room_profile.pkl
python scripts/run_live.py      # the one-line alert console
python scripts/evaluate.py      # the Phase-0 gate numbers (recall / false-alarms per week)
python scripts/plot_run.py      # SEE it: saves run.png (motion + sharpness + alerts)
pytest -q                       # 20 tests
```

### Live dashboard (optional demo layer)

For a screen-recordable pitch, `server/` adds a Flask bridge + a self-contained dashboard
(fall-alert + escalation UI) over the same pipeline. It is **additive and isolated** — the
core stays console-first (dashboard/UI is otherwise deferred, see the bottom of this file).
It auto-picks a source and always shows which one: **LIVE ESP32 → real-data replay →
synthetic demo**.

```
python server/app.py --no-serial     # guaranteed software demo -> http://localhost:8000
python server/app.py                 # use the ESP32 if it's streaming, else fall back
```

See [`server/README.md`](server/README.md) for the fallback chain, flags, and HTTP API.

> **Running it live on the two ESP32s?** The full live-system guide —
> hardware/USB bring-up, the 2-board reader, calibration & detection tuning, board
> placement, and troubleshooting the radio link — is in
> [`docs/SENSETHROUGH.md`](docs/SENSETHROUGH.md).

### Live hardware (after Milestone 1)

Once the RX ESP32 streams CSI to serial, swap the source — nothing downstream changes:

```python
from wisp.source.serial_source import SerialSource
source = SerialSource(port="COM5", baud=921600)   # or /dev/ttyUSB0 on Linux
```

## The MVP interface

A one-line printout is the entire UI until the gate passes:

```
[10:15:22] ALERT — sudden collapse (confidence 0.91, stillness=24s)
[11:33:01] ALERT — slow collapse   (confidence 0.87, stillness=180s)
```

...plus the logged event file. Build no more UI than that.

---

## Evaluation protocol (S9)

- **Replay engine:** recorded CSI → the same pipeline. Deterministic; also the demo fallback.
- **Labeling:** a simple CSV convention (not a UI): `staged fall / walk / empty / sit / pet-or-visitor`.
- **Staged collapses:** documented, safe protocol — crash mat, healthy volunteer, consent
  even for self-testing.
- **The run:** weeks of continuous unsupervised operation in the real occupied room,
  auto-logging every alert for human review.

Two things not to cut, ever, even in MVP:

1. **`logger.py` raw logging** — trivial to build, irreplaceable proprietary data, and your
   live-demo safety net.
2. **`state_machine.py` temporal logic** — single-window thresholding is exactly what
   produces the false-alarm spam that fails the gate. This is where the metric is won.

---

## Hardware (Phase 0)

- 2 ESP32 boards in hand (RX = WROOM-32, TX = ESP-32S) + 1 spare, 2 data USB cables.
- Compute node = a laptop. No Raspberry Pi yet.
- Firmware: ESP-IDF, `active_ap` → RX, `active_sta` → TX (matching SSID/channel/baud).
  **Milestone 1** = CSI streams to serial and reacts to a hand-wave.
- Fixed rig: both boards taped/bracketed so they cannot move for the whole test.
  Geometry (TX–RX distance, heights, orientation, room sketch, photo) documented once.
- The firmware/IDF version fight is the highest-risk, most time-consuming hardware task.

## References

- [CSI-Bench][csi-bench] — real Wi-Fi sensing benchmark. Source of the *optional* fall
  single-task subset for the supervised credibility number (S5.4). Not used to ship.
- [ESP-Fi-HAR][esp-fi-har] — ESP32 CSI human-activity-recognition reference. Useful when
  writing the firmware, the real `CSI_DATA` line format for `parser.py`, and `serial_source.py`.

[csi-bench]: https://github.com/guozhen-jenn-zhu/CSI-Bench-Real-WiFi-Sensing-Benchmark
[esp-fi-har]: https://github.com/AutoSmartGroup/ESP-Fi-HAR

---

## Explicitly deferred (NOT in this MVP)

Alerting service, dashboard/UI (a bare debug console is enough), data & ops tooling,
deployment hardening, autoencoder, supervised models, breathing detection, multi-room,
drift / recalibration automation, cloud, escalation, consent pipeline, data flywheel.

All of it is post-gate. The coding here is days; the *measuring* is weeks.
