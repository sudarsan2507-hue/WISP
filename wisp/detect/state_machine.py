"""S6 — temporal logic. THE false-alarm killer. DO NOT SKIP.

Single-window thresholding is exactly what produces the false-alarm spam that fails the
gate. A single anomalous window means nothing on its own — a slammed door, a pet, a
glitch all spike one window. What distinguishes a real collapse is a PATTERN OVER TIME:

    someone was active  ->  a disturbance  ->  stillness that PERSISTS >= T seconds

This machine encodes exactly that, for both fall types:

- **sudden**: a sharp/anomalous disturbance, then stillness persists >= confirm_s.
- **slow**  : the room was occupied (recent motion), motion declines, and stillness
              then persists >= slow_confirm_s — with NO sharp transient.

An empty room never fires the slow path, because "was recently occupied" is required.
Debounce/hysteresis stops re-firing until the person moves again. Every state
transition is appended to ``audit_log`` (kept even though severity tiers are deferred).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .rules import classify


@dataclass
class Alert:
    timestamp: float
    kind: str            # "sudden_collapse" | "slow_collapse"
    confidence: float
    stillness_s: float


@dataclass
class DetectionStateMachine:
    """disturbance -> stillness persists >= T -> CONFIRMED, with debounce.

    Parameters (seconds unless noted)
    ---------------------------------
    still_threshold      motion_intensity below this counts as "still"
    occupied_threshold   motion_intensity above this counts as "occupied/active"
    sharp_threshold      transient_sharpness above this counts as a sharp disturbance
    confirm_s            stillness needed to confirm a SUDDEN collapse
    slow_confirm_s       stillness needed to confirm a SLOW collapse
    recent_activity_s    how far back "was recently occupied" looks
    debounce_s           quiet time after an alert before the machine can re-arm
    """

    still_threshold: float
    occupied_threshold: float
    sharp_threshold: float
    confirm_s: float = 8.0
    slow_confirm_s: float = 20.0
    recent_activity_s: float = 10.0
    debounce_s: float = 5.0
    min_active_s: float = 0.0        # a collapse requires this much sustained activity first

    # ---- internal state
    _state: str = "NORMAL"
    _last_motion_t: Optional[float] = None      # last time we saw "occupied" motion
    _active_start: Optional[float] = None        # start of the current sustained-activity burst
    _still_since: Optional[float] = None         # when the current stillness began
    _peak_sharp: float = 0.0                     # peak sharpness during the disturbance
    _last_alert_t: Optional[float] = None
    audit_log: List[tuple] = field(default_factory=list)

    @property
    def state(self) -> str:
        """Current machine state (read-only): NORMAL | DISTURBANCE | STILL | CONFIRMED."""
        return self._state

    def _transition(self, t: float, new: str) -> None:
        if new != self._state:
            self.audit_log.append((t, self._state, new))
            self._state = new

    def update(self, timestamp: float, features: dict, is_anomaly: bool = False) -> Optional[Alert]:
        """Advance one step. Returns an Alert when a collapse is confirmed, else None."""
        motion = features["motion_intensity"]
        sharp = features["transient_sharpness"]

        # --- debounce: stay quiet after an alert until motion clearly resumes
        if self._last_alert_t is not None:
            if motion > self.occupied_threshold and timestamp - self._last_alert_t >= self.debounce_s:
                self._last_alert_t = None
                self._reset_dynamic(timestamp)
                self._transition(timestamp, "NORMAL")
            else:
                if motion > self.occupied_threshold:
                    self._last_motion_t = timestamp
                return None

        moving = motion > self.still_threshold
        occupied = motion > self.occupied_threshold

        if moving:
            # motion (re)appeared: remember it, note any sharp disturbance, clear stillness
            if occupied:
                # start a fresh activity burst if this is the first occupancy or the last one
                # was long ago; otherwise the burst is ongoing and keeps accumulating.
                if self._active_start is None or (
                    self._last_motion_t is not None
                    and (timestamp - self._last_motion_t) > self.recent_activity_s
                ):
                    self._active_start = timestamp
                self._last_motion_t = timestamp
            if is_anomaly or sharp >= self.sharp_threshold:
                self._peak_sharp = max(self._peak_sharp, sharp)
                self._transition(timestamp, "DISTURBANCE")
            self._still_since = None
            return None

        # --- below the stillness floor: accumulate stillness
        if self._still_since is None:
            self._still_since = timestamp
            self._transition(timestamp, "STILL")
        stillness = timestamp - self._still_since

        was_recently_occupied = (
            self._last_motion_t is not None
            and (self._still_since - self._last_motion_t) <= self.recent_activity_s
        )
        had_sharp_disturbance = self._peak_sharp >= self.sharp_threshold
        # a real collapse is preceded by SUSTAINED activity (someone up and moving), not a
        # 1-second twitch or a lone noise spike. This gate is the false-alarm killer on a
        # noisy live signal, where brief blips + the room's normal quiet otherwise look
        # identical to "fell then lay still". Default 0.0 keeps synthetic tests unchanged.
        active_s = (
            (self._last_motion_t - self._active_start)
            if (self._active_start is not None and self._last_motion_t is not None)
            else 0.0
        )
        had_enough_activity = active_s >= self.min_active_s

        # sudden: sustained activity, a sharp disturbance, then stillness >= confirm_s
        if had_enough_activity and had_sharp_disturbance and stillness >= self.confirm_s:
            return self._confirm(timestamp, stillness)
        # slow: sustained recent occupancy, no sharp disturbance, stillness >= slow_confirm_s
        if (had_enough_activity and was_recently_occupied
                and not had_sharp_disturbance and stillness >= self.slow_confirm_s):
            return self._confirm(timestamp, stillness)
        return None

    def _confirm(self, t: float, stillness: float) -> Alert:
        kind = classify(self._peak_sharp, self.sharp_threshold)
        confidence = min(1.0, 0.5 + stillness / (4.0 * self.confirm_s))
        self._transition(t, "CONFIRMED")
        self._last_alert_t = t
        alert = Alert(timestamp=t, kind=kind, confidence=round(confidence, 2), stillness_s=round(stillness, 1))
        self._peak_sharp = 0.0
        self._active_start = None
        return alert

    def _reset_dynamic(self, t: float) -> None:
        self._still_since = None
        self._peak_sharp = 0.0
        self._active_start = None
