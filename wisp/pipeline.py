"""The shared detection loop: CSISource + RoomProfile -> stream of Alerts.

One code path so that live detection (scripts/run_live.py), the evaluation harness (S9),
and the dashboard all run the *exact same* pipeline — features -> anomaly model -> temporal
state machine. Keeping it here means "what you demo" and "what you measure" can never
silently diverge.

``detection_telemetry`` is that single path: it yields per-window telemetry AND any
confirmed alert. ``run_detection`` is a thin alerts-only filter over it (unchanged
behaviour), and richer consumers like the dashboard read the full telemetry.
"""

from __future__ import annotations

from collections import deque
from statistics import median
from typing import Iterator, Optional, Tuple

from .calibrate.profile import RoomProfile
from .detect.state_machine import Alert, DetectionStateMachine
from .features.extract import feature_stream
from .source.base import CSISource


def _state_machine(profile: RoomProfile) -> DetectionStateMachine:
    return DetectionStateMachine(
        still_threshold=profile.still_threshold,
        occupied_threshold=profile.occupied_threshold,
        sharp_threshold=profile.sharp_threshold,
        confirm_s=profile.confirm_s,
        slow_confirm_s=profile.slow_confirm_s,
        recent_activity_s=profile.recent_activity_s,
        debounce_s=profile.debounce_s,
        min_active_s=profile.min_active_s,
    )


def detection_telemetry(
    source: CSISource, profile: RoomProfile, smooth_windows: int = 1
) -> Iterator[Tuple[float, dict, str, Optional[Alert]]]:
    """The one detection path. Yields ``(timestamp, features, state, alert_or_None)`` for
    EVERY window in the source stream.

    - ``features``  : ``{motion_intensity, transient_sharpness}`` for the window.
    - ``state``     : the state-machine state after this window (NORMAL/DISTURBANCE/STILL/CONFIRMED).
    - ``alert``     : an ``Alert`` on the window that confirms a collapse, else ``None``.

    ``smooth_windows`` > 1 median-filters the two features over that many recent windows
    BEFORE the state machine sees them. On a noisy live signal a single spurious packet can
    spike sharpness or dip motion and trip a false collapse; a median rejects those isolated
    outliers while a real, sustained pattern survives. Default 1 = no smoothing (so the
    synthetic tests and the gate harness are unchanged).

    Consumers that only care about confirmed collapses use ``run_detection``; consumers
    that need to render live room state (the dashboard) read the full telemetry — without
    re-implementing the loop and risking divergence from what the harness measures.
    """
    sm = _state_machine(profile)
    mbuf: deque = deque(maxlen=max(1, smooth_windows))
    sbuf: deque = deque(maxlen=max(1, smooth_windows))
    for t, feat in feature_stream(source, profile.mask, profile.win_samples, profile.hop_samples):
        if smooth_windows > 1:
            mbuf.append(feat["motion_intensity"])
            sbuf.append(feat["transient_sharpness"])
            feat = {"motion_intensity": median(mbuf), "transient_sharpness": median(sbuf)}
        alert = sm.update(t, feat, is_anomaly=profile.model.is_anomaly(feat))
        yield t, feat, sm.state, alert


def run_detection(source: CSISource, profile: RoomProfile) -> Iterator[Tuple[float, Alert]]:
    """Yield (timestamp, Alert) for every confirmed collapse in the source stream."""
    for t, _feat, _state, alert in detection_telemetry(source, profile):
        if alert is not None:
            yield t, alert
