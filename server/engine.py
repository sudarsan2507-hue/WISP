"""server/engine.py — the live monitoring engine behind the dashboard.

Responsibilities (framework-agnostic; no Flask here, so it stays unit-testable):

1. **Source selection with a fallback chain** — the heart of the demo's honesty:
       LIVE ESP32 (CSI actually streaming)  ->  real-data replay (CSI-Bench / recording)
       ->  synthetic demo room
   Whichever is chosen is reported as ``mode`` = LIVE | FALLBACK plus a human label, so the
   UI can ALWAYS show which one is running. A fallback is never silent.

2. **One detection path** — runs ``wisp.pipeline.detection_telemetry`` (same code the gate
   harness uses) in a background thread and keeps a thread-safe snapshot of room state.

3. **Escalation** — on a confirmed collapse, a cancellable countdown runs; if nobody
   cancels it, it "notifies the emergency contact". All timing is computed from wall-clock
   in ``snapshot`` so any number of dashboard viewers agree.

The web layer (``server/app.py``) is a thin shell over this.
"""

from __future__ import annotations

import glob
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import List, Optional

from wisp.calibrate.profile import RoomProfile
from wisp.ingest.parser import parse_csi_line
from wisp.pipeline import detection_telemetry
from wisp.source.base import CSISource
from wisp.source.replay import ReplaySource
from wisp.source.serial_source import SerialSource
from wisp.source.synthetic import SyntheticSource

# ------------------------------------------------------------------ config

_KIND_LABEL = {"sudden_collapse": "sudden collapse", "slow_collapse": "slow collapse"}
_STATE_LABEL = {
    "NORMAL": "normal",
    "DISTURBANCE": "disturbance",
    "STILL": "still",
    "CONFIRMED": "collapse confirmed",
}
_HISTORY = 160          # motion-history samples kept for the sparkline
_STALE_AFTER_S = 3.0    # no telemetry update for this long => feed considered stale
_ESCALATION_HOLD_S = 6.0  # keep "notified" on screen this long before re-arming (looping demo)


@dataclass
class EngineOptions:
    # source chain
    serial_port: Optional[str] = None     # explicit port; None => autodetect (unless probe_serial False)
    probe_serial: bool = True             # try live ESP32 first
    baud: int = 921600
    probe_s: float = 6.0                  # how long to wait for a CSI line before giving up
    csi_bench: Optional[str] = None       # path to CSI-Bench .h5 file/dir (real-data fallback)
    replay: Optional[str] = None          # path to a recorded RawLogger CSV (real-data fallback)
    companion_port: Optional[str] = None  # 2nd ESP32 (TX) port to hold open so the link stays up
    # calibration
    profile_path: str = "room_profile.pkl"
    calibrate_s: float = 20.0             # seconds of live "normal" to fit a live profile
    rate_hz: float = 50.0                 # synthetic sample rate / nominal live rate
    smooth_windows: int = 9               # live: median-filter features over N windows (kills noise-driven false alarms)
    # absolute threshold overrides (live): pin the still/occupied/sharp lines to measured
    # values instead of calibration percentiles. Needed when the resting noise floor sits
    # ABOVE the percentile-derived still line, which otherwise latches "disturbance" forever.
    still_abs: Optional[float] = None
    occupied_abs: Optional[float] = None
    sharp_abs: Optional[float] = None
    confirm_s: float = 8.0                 # live: stillness needed to confirm a sudden collapse
    min_active_s: float = 3.0              # live: sustained activity required before a collapse counts (false-alarm killer)
    # playback + demo flavour
    speed: float = 3.0                    # fallback playback speed multiplier (live is real-time)
    loop: bool = True                     # loop finite fallback streams (unattended demo)
    escalate_s: float = 30.0              # countdown before auto-notifying the contact
    room: str = "Room 1"
    contact: str = "Emergency contact"


@dataclass
class _SourceChoice:
    source: CSISource
    mode: str        # "LIVE" | "FALLBACK"
    live: bool
    kind: str        # serial | csi_bench | replay | synthetic
    label: str       # human label for the badge
    note: str        # why this source / extra context
    sample_rate_hz: float


# ------------------------------------------------------------------ probing / selection

def _autodetect_ports() -> List[str]:
    """Linux/WSL serial devices the ESP32 typically shows up as."""
    return sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyACM*"))


def probe_serial(port: str, baud: int, probe_s: float, prefix: str = "CSI_DATA") -> bool:
    """Return True iff a ``CSI_DATA`` line arrives on ``port`` within ``probe_s`` seconds.

    Safe on machines with no hardware / no pyserial: any failure => False (=> fallback).
    """
    try:
        import serial  # pyserial, lazy
    except Exception:
        return False
    try:
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = baud
        ser.timeout = 0.5
        ser.dtr = False   # don't reset the ESP32 on open (RTS->EN, DTR->GPIO0)
        ser.rts = False
        ser.open()
        ser.dtr = False
        ser.rts = False
        try:
            deadline = time.time() + probe_s
            while time.time() < deadline:
                raw = ser.readline().decode("ascii", errors="ignore").strip()
                if raw.startswith(prefix):
                    return True
        finally:
            ser.close()
    except Exception:
        return False
    return False


def _open_runmode(port: str, baud: int, timeout: float = 0.1):
    """Open a serial port WITHOUT knocking the ESP32 into download mode: DTR low keeps GPIO0
    high (normal boot) and RTS low keeps EN high (not held in reset). The board still resets
    once on open, then runs its firmware."""
    import serial
    s = serial.Serial()
    s.port = port
    s.baudrate = baud
    s.timeout = timeout
    s.dtr = False
    s.rts = False
    s.open()
    s.dtr = False
    s.rts = False
    return s


class _ListSource(CSISource):
    """Re-streams an already-collected list of (t, amp) records (used for live calibration)."""

    def __init__(self, records) -> None:
        self._records = records

    def stream(self):
        yield from self._records


class _GenSource(CSISource):
    """Wraps an already-open generator so detection continues the SAME serial read (one open)."""

    def __init__(self, gen) -> None:
        self._gen = gen

    def stream(self):
        yield from self._gen


class _LiveTwoBoard(CSISource):
    """Live 2-board reader. Opens the RECEIVER port and, if given, also HOLDS the TRANSMITTER
    (companion) port open — both in run-mode — so the two ESP32s boot in sync and the Wi-Fi
    link stays up. Opening either port resets that board once; holding both open is what keeps
    the CSI rate stable (opening only one, or at different times, drops the link). Yields
    (t, amp) from the receiver, skipping boot/log chatter and malformed lines.
    """

    def __init__(self, reader_port: str, companion_port, baud: int, prefix: str = "CSI_DATA") -> None:
        self.reader_port = reader_port
        self.companion_port = companion_port
        self.baud = baud
        self.prefix = prefix

    def stream(self):
        companion = _open_runmode(self.companion_port, self.baud) if self.companion_port else None
        ser = _open_runmode(self.reader_port, self.baud)
        t0 = time.time()
        # ESP32 CSI packets arrive with different subcarrier counts (HT20/HT40, LLTF vs
        # HT-LTF). Lock onto the DOMINANT width from the first batch and skip the rest, so the
        # fixed-width room mask always matches — otherwise preprocessing IndexErrors and the
        # whole detection loop dies mid-stream.
        buf = []
        width = None
        baseline = None  # slow EMA of the packet mean -> removes AGC drift, keeps fast motion
        try:
            while True:
                raw = ser.readline().decode("ascii", errors="ignore").strip()
                if not raw or not raw.startswith(self.prefix):
                    continue
                try:
                    amp = parse_csi_line(raw)
                except ValueError:
                    continue
                # High-pass AGC removal: divide by a SLOW baseline (not the packet's own mean),
                # so slow gain drift cancels but a body moving (fast scale change) survives.
                pm = float(amp.mean())
                if pm > 1e-6:
                    baseline = pm if baseline is None else (0.97 * baseline + 0.03 * pm)
                    amp = amp / baseline
                rec = (time.time() - t0, amp)
                if width is None:                       # still learning the dominant width
                    buf.append(rec)
                    if len(buf) >= 40:
                        width = Counter(a.size for _, a in buf).most_common(1)[0][0]
                        for r in buf:
                            if r[1].size == width:
                                yield r
                        buf = []
                    continue
                if amp.size != width:                   # skip odd-width packets
                    continue
                yield rec
        finally:
            ser.close()
            if companion is not None:
                companion.close()


def choose_source(opts: EngineOptions) -> _SourceChoice:
    """Walk the fallback chain and return the first source that is actually available."""
    # 1) LIVE ESP32.
    if opts.probe_serial:
        # An explicit --serial port is TRUSTED (no probe): probing means an extra open, and
        # each open resets the board and disrupts the 2-board link. Autodetected ports are
        # still probed, to pick the one actually streaming CSI.
        if opts.serial_port:
            tx = f" (+ TX held on {opts.companion_port})" if opts.companion_port else ""
            return _SourceChoice(
                source=SerialSource(opts.serial_port, opts.baud),
                mode="LIVE", live=True, kind="serial",
                label=f"ESP32 · {opts.serial_port}",
                note=f"live CSI from the ESP32 on {opts.serial_port}{tx}",
                sample_rate_hz=opts.rate_hz,
            )
        for port in _autodetect_ports():
            if probe_serial(port, opts.baud, opts.probe_s):
                return _SourceChoice(
                    source=SerialSource(port, opts.baud),
                    mode="LIVE", live=True, kind="serial",
                    label=f"ESP32 · {port}",
                    note=f"live CSI streaming from the ESP32 on {port}",
                    sample_rate_hz=opts.rate_hz,
                )
        tried = ", ".join(_autodetect_ports()) or "no serial ports found"
        fallback_note = f"no live ESP32 CSI ({tried}) — running on fallback data"
    else:
        fallback_note = "live probe disabled — running on fallback data"

    # 2) real-data replay — CSI-Bench (real captured CSI) or a recorded CSV
    if opts.csi_bench:
        try:
            from wisp.source.csi_bench_source import CSIBenchSource
            return _SourceChoice(
                source=CSIBenchSource(opts.csi_bench, sample_rate_hz=opts.rate_hz),
                mode="FALLBACK", live=False, kind="csi_bench",
                label="CSI-Bench · real captured CSI",
                note=f"{fallback_note}; replaying CSI-Bench clips: {opts.csi_bench}",
                sample_rate_hz=opts.rate_hz,
            )
        except Exception as exc:  # pragma: no cover - depends on optional h5py/data
            fallback_note += f"; CSI-Bench unavailable ({exc})"
    if opts.replay:
        return _SourceChoice(
            source=ReplaySource(opts.replay),
            mode="FALLBACK", live=False, kind="replay",
            label="Recording · replayed CSI log",
            note=f"{fallback_note}; replaying recording: {opts.replay}",
            sample_rate_hz=opts.rate_hz,
        )

    # 3) synthetic demo room — always available, correct, self-contained
    return _SourceChoice(
        source=SyntheticSource.demo(sample_rate_hz=opts.rate_hz),
        mode="FALLBACK", live=False, kind="synthetic",
        label="Synthetic demo room",
        note=f"{fallback_note}; using the built-in synthetic room",
        sample_rate_hz=opts.rate_hz,
    )


# ------------------------------------------------------------------ calibration

def build_profile(opts: EngineOptions, choice: _SourceChoice) -> RoomProfile:
    """Load a saved profile if present, else fit one appropriate to the chosen source."""
    import os

    if os.path.exists(opts.profile_path):
        return RoomProfile.load(opts.profile_path)

    if choice.live:
        # Calibrate on the room's OWN live normal (room must be behaving normally now).
        n = max(200, int(opts.calibrate_s * opts.rate_hz))
        cal = SerialSource(choice.source.port, choice.source.baud, max_packets=n)  # type: ignore[attr-defined]
        profile = RoomProfile.fit(cal, sample_rate_hz=opts.rate_hz)
        profile.save(opts.profile_path)
        return profile

    # Fallback sources: calibrate on synthetic normal at the same rate (matches the
    # synthetic demo exactly; a reasonable default for replay code-path demos too).
    profile = RoomProfile.fit(
        SyntheticSource.normal_only(minutes=3.0, sample_rate_hz=opts.rate_hz),
        sample_rate_hz=opts.rate_hz,
    )
    return profile


# ------------------------------------------------------------------ the engine

class MonitorEngine:
    """Runs detection in a background thread and exposes a thread-safe snapshot."""

    def __init__(self, opts: Optional[EngineOptions] = None) -> None:
        self.opts = opts or EngineOptions()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.choice: Optional[_SourceChoice] = None
        self.profile: Optional[RoomProfile] = None
        self._calibrating = True

        self._started_at = 0.0
        self._last_update = 0.0
        self._packets = 0
        self._cur = {"t": 0.0, "state": "NORMAL", "motion": 0.0, "sharp": 0.0, "motion_norm": 0.0}
        self._history: deque = deque(maxlen=_HISTORY)
        self._stream_ended = False
        self._error: Optional[str] = None

        # alert / escalation state
        self._alert: Optional[dict] = None   # {kind_raw, kind, confidence, stillness_s, at_t, deadline}
        self._escalated_at: Optional[float] = None
        self._resolution: Optional[str] = None   # "cancelled" | "notified"
        self._last_resolved_at = 0.0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> "MonitorEngine":
        # choose_source probes for a live board (fast once CSI is flowing); calibration is
        # deferred into the worker thread so the web server can come up immediately.
        self.choice = choose_source(self.opts)
        self._started_at = time.time()
        self._last_update = time.time()
        self._thread = threading.Thread(target=self._run, name="wisp-monitor", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        assert self.choice is not None
        try:
            if self.choice.live:
                self._run_live()
            else:
                self._run_fallback()
        except Exception as exc:  # keep the server alive; surface the error to the UI
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
                self._stream_ended = True

    def _run_live(self) -> None:
        """ONE continuous serial read, shared by calibration + detection (a single board
        reset), with the transmitter held open so the 2-board link stays up. First CSI
        arrives ~15s after open while the boards boot and associate."""
        reader_port = self.choice.source.port  # type: ignore[attr-defined]
        gen = _LiveTwoBoard(reader_port, self.opts.companion_port, self.opts.baud).stream()

        # calibrate on the room's own live normal (keep the room normal during this window)
        n = max(120, int(self.opts.calibrate_s * self.opts.rate_hz))
        cal = []
        for rec in gen:
            if self._stop.is_set():
                return
            cal.append(rec)
            with self._lock:
                self._last_update = time.time()
            if len(cal) >= n:
                break
        # Conservative thresholds + timings for a noisy live signal: a wide dead-band (only
        # the bottom 10% of motion counts as "still", only the top 10% as "occupied", only
        # extreme spikes as an impact) plus long sustained-stillness confirmation, so ordinary
        # radio noise can't walk the state machine into a false collapse.
        self.profile = RoomProfile.fit(
            _ListSource(cal), sample_rate_hz=self.opts.rate_hz,
            still_pct=25.0, occupied_pct=80.0, sharp_pct=97.0,
            still_abs=self.opts.still_abs, occupied_abs=self.opts.occupied_abs,
            sharp_abs=self.opts.sharp_abs,
            confirm_s=self.opts.confirm_s, slow_confirm_s=25.0,
            recent_activity_s=12.0, debounce_s=8.0,
            min_active_s=self.opts.min_active_s,
        )
        self.profile.save(self.opts.profile_path)
        with self._lock:
            self._calibrating = False
            self._last_update = time.time()

        # detection continues the SAME open stream — no re-open, no extra reset. Median
        # smoothing rejects isolated noisy packets before they reach the state machine.
        for t, feat, state, alert in detection_telemetry(
                _GenSource(gen), self.profile, smooth_windows=self.opts.smooth_windows):
            if self._stop.is_set():
                break
            self._ingest(t, feat, state, alert)
        with self._lock:
            self._stream_ended = True

    def _run_fallback(self) -> None:
        self.profile = build_profile(self.opts, self.choice)
        with self._lock:
            self._calibrating = False
            self._last_update = time.time()
        first_pass = True
        while not self._stop.is_set() and (first_pass or self.opts.loop):
            first_pass = False
            source = self._fresh_source()  # a fresh source each pass (generators are one-shot)
            wall0 = time.time()
            for t, feat, state, alert in detection_telemetry(source, self.profile):
                if self._stop.is_set():
                    return
                if self.opts.speed > 0:
                    target = wall0 + t / self.opts.speed
                    gap = target - time.time()
                    if gap > 0:
                        time.sleep(min(gap, 0.25))  # cap so stop stays responsive
                self._ingest(t, feat, state, alert)
        with self._lock:
            self._stream_ended = True

    def _fresh_source(self) -> CSISource:
        """Re-create the chosen fallback source for another playback loop."""
        assert self.choice is not None
        c = self.choice
        if c.kind == "synthetic":
            return SyntheticSource.demo(sample_rate_hz=c.sample_rate_hz)
        if c.kind == "replay":
            return ReplaySource(self.opts.replay)  # type: ignore[arg-type]
        if c.kind == "csi_bench":
            from wisp.source.csi_bench_source import CSIBenchSource
            return CSIBenchSource(self.opts.csi_bench, sample_rate_hz=c.sample_rate_hz)  # type: ignore[arg-type]
        return c.source

    # -- ingest one window -------------------------------------------------
    def _ingest(self, t: float, feat: dict, state: str, alert) -> None:
        occ = self.profile.occupied_threshold or 1e-9
        still = self.profile.still_threshold
        # Display ceiling: spread the bar across the whole movement range (still -> clearly
        # active), NOT just the narrow still->occupied detection band. When that band is small
        # (noisy placement), normalizing by it alone snaps the meter 0<->100 with nothing in
        # between; scaling to ~5x the band above still puts the "occupied" line near a fifth of
        # the bar and lets real movement climb gradually toward full. The dashboard maps
        # motion_norm (0..1.6) onto 0..100%, so ceiling -> 1.6 = full bar.
        ceiling = still + max(occ - still, 1e-9) * 5.0
        span = max(ceiling - still, 1e-9)
        motion = float(feat["motion_intensity"])
        with self._lock:
            self._last_update = time.time()
            self._packets += 1
            self._cur = {
                "t": round(t, 2),
                "state": state,
                "motion": motion,
                "sharp": float(feat["transient_sharpness"]),
                "motion_norm": max(0.0, min(1.6 * (motion - still) / span, 1.6)),
            }
            self._history.append(round(self._cur["motion_norm"], 4))

            if alert is not None and self._alert is None and self._resolution is None:
                self._alert = {
                    "kind_raw": alert.kind,
                    "kind": _KIND_LABEL.get(alert.kind, alert.kind.replace("_", " ")),
                    "confidence": alert.confidence,
                    "stillness_s": alert.stillness_s,
                    "at_t": round(alert.timestamp, 1),
                    "deadline": time.time() + self.opts.escalate_s,
                }
                self._escalated_at = None

    # -- external commands -------------------------------------------------
    def cancel(self) -> bool:
        """Dashboard 'I'm OK' — cancel an in-progress alert/escalation."""
        with self._lock:
            if self._alert is not None:
                self._alert = None
                self._escalated_at = None
                self._resolution = "cancelled"
                self._last_resolved_at = time.time()
                return True
            return False

    def reset(self) -> None:
        """Clear any resolved/alert state so the monitor returns to a clean baseline."""
        with self._lock:
            self._alert = None
            self._escalated_at = None
            self._resolution = None

    # -- snapshot ----------------------------------------------------------
    def _advance_escalation(self) -> None:
        """Compute escalation phase from wall clock; auto-clear after the hold window."""
        now = time.time()
        if self._alert is not None:
            if self._escalated_at is None and now >= self._alert["deadline"]:
                self._escalated_at = now
                self._resolution = "notified"
        # auto-clear a resolved alert after the hold, so a looping demo can re-fire
        if self._resolution is not None:
            ref = self._escalated_at or self._last_resolved_at
            if self._resolution == "notified" and self._escalated_at is not None and now - self._escalated_at >= _ESCALATION_HOLD_S:
                self._alert = None
                self._escalated_at = None
                self._resolution = None
            elif self._resolution == "cancelled" and now - self._last_resolved_at >= 2.0:
                self._resolution = None

    def snapshot(self) -> dict:
        now = time.time()
        with self._lock:
            self._advance_escalation()
            c = self.choice
            stale = (c is not None and c.live and not self._calibrating
                     and (now - self._last_update > _STALE_AFTER_S))

            if self._alert is not None:
                phase = "escalated" if self._escalated_at is not None else "active"
                countdown = max(0.0, self._alert["deadline"] - now)
                alert = {
                    "phase": phase,
                    "kind": self._alert["kind"],
                    "kind_raw": self._alert["kind_raw"],
                    "confidence": self._alert["confidence"],
                    "stillness_s": self._alert["stillness_s"],
                    "at_t": self._alert["at_t"],
                    "countdown_s": round(countdown, 1),
                    "resolution": self._resolution,
                }
            elif self._resolution == "cancelled":
                alert = {"phase": "cancelled", "resolution": "cancelled"}
            else:
                alert = {"phase": "none"}

            state = self._cur["state"]
            status_label = _STATE_LABEL.get(state, state.lower())

            return {
                "mode": None if c is None else c.mode,
                "live": bool(c and c.live),
                "source_kind": None if c is None else c.kind,
                "source_label": None if c is None else c.label,
                "note": None if c is None else c.note,
                "sample_rate_hz": None if c is None else c.sample_rate_hz,
                "room": self.opts.room,
                "contact": self.opts.contact,
                "escalate_s": self.opts.escalate_s,
                "running": self._thread is not None and self._thread.is_alive(),
                "calibrating": self._calibrating,
                "stream_ended": self._stream_ended,
                "stale": stale,
                "error": self._error,
                "uptime_s": round(now - self._started_at, 1) if self._started_at else 0.0,
                "packets": self._packets,
                "thresholds": None if self.profile is None else {
                    "still": round(self.profile.still_threshold, 4),
                    "occupied": round(self.profile.occupied_threshold, 4),
                    "sharp": round(self.profile.sharp_threshold, 4),
                },
                "profile_summary": None if self.profile is None else self.profile.summary(),
                "monitor": {
                    "t": self._cur["t"],
                    "state": state,
                    "status_label": status_label,
                    "motion": round(self._cur["motion"], 4),
                    "motion_norm": round(self._cur["motion_norm"], 4),
                    "sharp": round(self._cur["sharp"], 4),
                    "history": list(self._history),
                },
                "alert": alert,
            }
