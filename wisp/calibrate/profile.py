"""S4 — one-room calibration. Thresholds come from the room's OWN percentile stats.

`RoomProfile.fit` learns everything the detector needs from a recording of one room's
"normal" (no falls):

- the **subcarrier mask** (which carriers carry signal),
- the trained **IsolationForest** anomaly model,
- and the **thresholds** — stillness floor, occupied level, and the sharpness ceiling —
  all read from the room's own feature percentiles, not global constants.

One room, one profile, pickled to disk. Recalibration UI / drift detection / multi-room
are all post-MVP.
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Tuple

import numpy as np

from ..detect.model import AnomalyModel
from ..features.extract import feature_stream
from ..preprocess.clean import subcarrier_mask
from ..source.base import CSISource


class _ListSource(CSISource):
    """Wraps an already-materialized list of (t, amp) so it can be re-streamed."""

    def __init__(self, records: List[Tuple[float, np.ndarray]]) -> None:
        self._records = records

    def stream(self) -> Iterator[Tuple[float, np.ndarray]]:
        yield from self._records


@dataclass
class RoomProfile:
    """Mask + thresholds + model, all fit from one room's normal recording."""

    mask: np.ndarray
    model: AnomalyModel
    sample_rate_hz: float
    win_samples: int
    hop_samples: int
    still_threshold: float
    occupied_threshold: float
    sharp_threshold: float
    # temporal-logic timings carried alongside the profile
    confirm_s: float = 8.0
    slow_confirm_s: float = 20.0
    recent_activity_s: float = 15.0
    debounce_s: float = 5.0
    min_active_s: float = 0.0

    @classmethod
    def fit(
        cls,
        source: CSISource,
        sample_rate_hz: float,
        win_s: float = 1.0,
        hop_s: float = 0.2,
        still_pct: float = 35.0,
        occupied_pct: float = 65.0,
        sharp_pct: float = 99.5,
        still_abs: Optional[float] = None,
        occupied_abs: Optional[float] = None,
        sharp_abs: Optional[float] = None,
        model_kw: Optional[dict] = None,
        **timings,
    ) -> "RoomProfile":
        """Fit a profile from a normal-room recording (a CSISource).

        Thresholds default to the room's own motion/sharpness percentiles. But a percentile
        of a *moving* calibration can land the stillness floor BELOW the room's true resting
        noise floor — then "quiet" reads as "still moving" and no collapse can ever confirm.
        When the real floor is known from live measurement, pass ``still_abs`` /
        ``occupied_abs`` / ``sharp_abs`` to pin a threshold to an absolute value and override
        the percentile. The mask and anomaly model are always learned from the data.
        """
        # Materialize ONCE — a live/synthetic generator is one-shot, and we need two
        # passes (mask, then features) over identical data.
        records = list(source.stream())
        amps = np.array([a for _, a in records])
        mask = subcarrier_mask(amps)

        win_samples = max(2, int(round(win_s * sample_rate_hz)))
        hop_samples = max(1, int(round(hop_s * sample_rate_hz)))

        motions: List[float] = []
        sharps: List[float] = []
        vecs: List[np.ndarray] = []
        for _, feat in feature_stream(_ListSource(records), mask, win_samples, hop_samples):
            motions.append(feat["motion_intensity"])
            sharps.append(feat["transient_sharpness"])
            vecs.append(AnomalyModel.to_vector(feat))

        model = AnomalyModel(**(model_kw or {}))
        model.fit(np.array(vecs))

        return cls(
            mask=mask,
            model=model,
            sample_rate_hz=float(sample_rate_hz),
            win_samples=win_samples,
            hop_samples=hop_samples,
            still_threshold=float(still_abs if still_abs is not None else np.percentile(motions, still_pct)),
            occupied_threshold=float(occupied_abs if occupied_abs is not None else np.percentile(motions, occupied_pct)),
            sharp_threshold=float(sharp_abs if sharp_abs is not None else np.percentile(sharps, sharp_pct)),
            **timings,
        )

    def save(self, path: str) -> None:
        with open(path, "wb") as fh:
            pickle.dump(self, fh)

    @classmethod
    def load(cls, path: str) -> "RoomProfile":
        with open(path, "rb") as fh:
            return pickle.load(fh)

    def summary(self) -> str:
        return (
            f"RoomProfile(subcarriers_kept={int(self.mask.sum())}/{self.mask.size}, "
            f"still<{self.still_threshold:.2f}, occupied>{self.occupied_threshold:.2f}, "
            f"sharp>{self.sharp_threshold:.2f}, win={self.win_samples}, hop={self.hop_samples})"
        )
