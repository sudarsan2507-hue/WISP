# Handoff - where wisp stands and what's next

Snapshot for whoever picks this up next. Everything below is on `main`.

## TL;DR

The **synthetic-data MVP runs end-to-end today, with zero hardware.** Calibrate a room
profile, run the one-line alert console, and print the Phase 0 gate metrics - all from
`python scripts/*.py`. On the synthetic demo it currently scores **recall 2/2, kinds
correct 2/2, 0 false alarms/week.** What remains is real: the live serial source (after
hardware Milestone 1), an optional CSI-Bench validation adapter, and - the actual point
weeks of real-room measurement.

## Run it

```
pip install -r requirements.txt
python scripts/evaluate.py      # auto-calibrates, prints the gate table (recall / FA-per-week)
python scripts/run_live.py      # prints the one-line alert console on the synthetic demo
python scripts/calibrate.py     # fits + saves a room_profile.pkl explicitly
pytest -q                       # 6 tests: features on known signals + state-machine logic
```

Example output of `run_live.py`:

```
[00:00:50] ALERT - sudden collapse (confidence 0.75, stillness=8.0s)
[00:01:44] ALERT - slow collapse (confidence 1.0, stillness=20.0s)
```

## Status by module

| Module | S | Status |
| --- | --- | --- |
| `wisp/source/base.py` | S1.6 | ✅ `CSISource` interface |
| `wisp/source/synthetic.py` | S1.6 | ✅ labeled fake room (`demo()`, `normal_only()`) |
| `wisp/ingest/parser.py` | S1 | ✅ parses `[I Q ...]` → amplitude *(verify vs a real ESP32 line)* |
| `wisp/ingest/logger.py` | S1.5 | ✅ `RawLogger` CSV writer |
| `wisp/source/replay.py` | S1.6 | ✅ replays a RawLogger CSV |
| `wisp/preprocess/clean.py` | S2 | ✅ mask + Hampel |
| `wisp/features/extract.py` | S3 | ✅ motion, sharpness, `feature_stream` |
| `wisp/detect/model.py` | S5 | ✅ IsolationForest |
| `wisp/detect/rules.py` | S5.2 | ✅ sudden vs slow |
| `wisp/detect/state_machine.py` | S6 | ✅ temporal logic + audit log |
| `wisp/calibrate/profile.py` | S4 | ✅ `RoomProfile.fit/save/load` |
| `wisp/pipeline.py` | - | ✅ shared `run_detection` loop |
| `wisp/evaluate/harness.py` | S9 | ✅ recall / FA-per-week / latency |
| `scripts/{calibrate,run_live,evaluate}.py` | - | ✅ wired, runnable |
| `tests/` | S3.7 | ✅ 6 passing (features + state machine) |
| `wisp/source/csi_bench_source.py` | - | ✅ adapter to replay CSI-Bench `.h5` clips (needs `h5py`) |
| `wisp/source/serial_source.py` | S1.6 | ✅ live ESP32 reader (pyserial, lazy import) - needs a board to *run*, but written |
| `scripts/plot_run.py` | - | ✅ saves `run.png` - motion + sharpness + alerts |
| `tests/` | - | ✅ 20 passing (features, state machine, parser, logger↔replay, model, profile, harness, csi-bench) |

## What's genuinely left

1. **`serial_source.py`** (post-Milestone-1). Read serial → `parser.parse_csi_line` →
   yield `(t, amp)`. Then everything above runs on live data unchanged. Confirm the real
   `CSI_DATA` column layout matches `parser.py` (only the trailing `[...]` block is used).
2. **Record real normal + staged falls**, calibrate on the real normal
   (`scripts/calibrate.py --replay <log.csv>`), and re-run `evaluate.py` on real
   recordings with a labels CSV (`harness.load_events`).
3. **The gate:** weeks of continuous real-room operation, logging every alert for review.
4. *Optional, already wired:* validate on real CSI via CSI-Bench. Download the Fall
   single-task subset (Kaggle: `guozhenjennzhu/csi-bench`), then:
   ```python
   from wisp.source.csi_bench_source import CSIBenchSource
   src = CSIBenchSource("path/to/FallDetection")   # a file or a directory of .h5
   print(src.list_datasets())                       # confirm the in-file layout first
   for t, amp in src.stream():                       # same (t, amp) contract as everything else
       ...
   ```
   Note this is a real-CSI *code* sanity check, not the gate (see the module docstring).
   The supervised S5.4 benchmark (investor credibility number) is still separate - GPU,
   ~sub-hour, NOT the shipping model.

## Tuning knobs (all in one place)

`RoomProfile.fit(...)` percentiles set the thresholds from the room's own stats:
`still_pct=35, occupied_pct=65, sharp_pct=99.5`. Temporal timings
(`confirm_s, slow_confirm_s, recent_activity_s, debounce_s`) travel on the profile and
feed the state machine via `pipeline._state_machine`. If real rooms produce false
alarms, raise `slow_confirm_s` and the percentiles first - that trades a little latency
for the gate number, which is the right trade.

## Design reminders (don't undo these)

- **Everything hides behind `CSISource`.** Synthetic → replay → serial are interchangeable.
- **The state machine is the false-alarm killer.** Never collapse it to single-window
  thresholding - that's what fails the gate.
- **No GPU / no CSI-Bench for the shipping detector.** IsolationForest trains on *this
  room's* normal in seconds. The gate is a *room* number (false alarms/week), not a
  dataset accuracy.
