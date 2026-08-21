"""The signal a simulated heart rate sensor produces.

Separated from the transport on purpose: a beat sequence is pure arithmetic
over a random source this class owns, so it is reproducible from a seed and
testable without a Bluetooth adapter anywhere near it. `hr_simulator.py` is
what puts these beats on the air.

Heart rate variability is the whole reason a scenario reaches for this tool, so
the beat interval - not the pulse - is the primary quantity here. The pulse is
derived from it, the way a real sensor derives it.
"""

from __future__ import annotations

import logging
import math
import random
from typing import List, Optional, Sequence, Tuple

from .profiles.heart_rate import rr_from_ms

LOGGER = logging.getLogger(__name__)

DEFAULT_BPM = 60.0
DEFAULT_JITTER_MS = 25.0

# A pulse outside this range is not a signal any central should be asked to
# believe, so it is rejected rather than clamped silently.
MIN_BPM = 20.0
MAX_BPM = 250.0

# Bounds the jittered interval is clamped to, derived from the pulse limits.
# Clamping rather than rejecting is right here: jitter is random, and one
# unlucky sample must not abort a run that is otherwise configured sanely.
MIN_RR_MS = 60_000.0 / MAX_BPM
MAX_RR_MS = 60_000.0 / MIN_BPM


def rmssd(intervals_ms: Sequence[float]) -> float:
    """Root mean square of successive differences, the standard HRV metric.

    Returns 0.0 for fewer than two intervals, where it is undefined - a caller
    logging this wants a number, not an exception, for the first notification.
    """
    if len(intervals_ms) < 2:
        return 0.0
    differences = [
        later - earlier for earlier, later in zip(intervals_ms, intervals_ms[1:])
    ]
    return math.sqrt(sum(difference**2 for difference in differences) / len(differences))


class HrvSignal:
    """A beat generator: turns elapsed time into the beats that fell inside it.

    Usage:

        signal = HrvSignal(bpm=60, jitter_ms=25, seed=1)
        pulse, intervals = signal.advance(1.0)   # ~1 beat at 60 bpm

    `advance` is the only thing that moves time forward, so the caller decides
    the notification cadence and the signal stays free of timers.

    `seed` makes a run reproducible. The random source is per-instance, so two
    simulators in one process never perturb each other's sequence.
    """

    def __init__(
        self,
        bpm: float = DEFAULT_BPM,
        jitter_ms: float = DEFAULT_JITTER_MS,
        drift_bpm_per_min: float = 0.0,
        seed: Optional[int] = None,
    ):
        _check_bpm(bpm)
        if jitter_ms < 0:
            raise ValueError(f"jitter_ms cannot be negative, got {jitter_ms}")

        self.jitter_ms = jitter_ms
        self.drift_bpm_per_min = drift_bpm_per_min
        self._bpm = float(bpm)
        self._random = random.Random(seed)
        # Time carried over from the last `advance` that was not long enough to
        # complete another beat. Without it, a cadence that is not a whole
        # number of beats would lose a fraction of a beat every notification
        # and drift away from the configured rate.
        self._carry_s = 0.0
        self._last_interval_ms: Optional[float] = None

    @property
    def bpm(self) -> float:
        """The current pulse, after any drift applied so far."""
        return self._bpm

    def set_bpm(self, bpm: float) -> None:
        """Jump straight to a new pulse, as a scenario step does."""
        _check_bpm(bpm)
        LOGGER.info("HRV signal pulse %.0f -> %.0f bpm", self._bpm, bpm)
        self._bpm = float(bpm)

    def advance(self, seconds: float) -> Tuple[int, List[int]]:
        """Advance by `seconds`; return the pulse and the beats that completed.

        Intervals come back in the characteristic's 1/1024 s units, ready to
        encode. The list is empty when the window was shorter than the next
        beat, which is normal at a low pulse and a fast cadence.
        """
        if seconds < 0:
            raise ValueError(f"cannot advance by a negative time, got {seconds}")

        self._apply_drift(seconds)
        self._carry_s += seconds

        intervals: List[int] = []
        while True:
            interval_ms = self._next_interval_ms()
            if self._carry_s < interval_ms / 1000.0:
                break
            self._carry_s -= interval_ms / 1000.0
            self._last_interval_ms = interval_ms
            intervals.append(rr_from_ms(interval_ms))

        return round(self._bpm), intervals

    def _next_interval_ms(self) -> float:
        """One beat interval: the mean for the current pulse, plus jitter."""
        mean_ms = 60_000.0 / self._bpm
        if self.jitter_ms == 0:
            return mean_ms
        jittered = self._random.gauss(mean_ms, self.jitter_ms)
        return min(max(jittered, MIN_RR_MS), MAX_RR_MS)

    def _apply_drift(self, seconds: float) -> None:
        """Move the pulse along by the configured drift, staying in range."""
        if not self.drift_bpm_per_min:
            return
        drifted = self._bpm + self.drift_bpm_per_min * seconds / 60.0
        self._bpm = min(max(drifted, MIN_BPM), MAX_BPM)


def _check_bpm(bpm: float) -> None:
    """Reject a pulse outside the range this signal will generate."""
    if not MIN_BPM <= bpm <= MAX_BPM:
        raise ValueError(f"bpm must be between {MIN_BPM:.0f} and {MAX_BPM:.0f}, got {bpm}")
