"""
arm/motion.py — gesture library and the 50 Hz motion engine.

The arm is a 4-DOF acrylic kit: base (turn), shoulder (lean), elbow (lift),
claw (open/close). Angles are in degrees; 90 is the assembled centre for the
three joints. The claw runs 0 (open) .. 60 (closed).

Decision: keyframes + easing, not a planner
  Every gesture is a short list of target poses with durations. The engine
  eases between them (cosine), so all joints start and stop together — that
  is what makes motion look deliberate rather than servo-jerky.

Decision: a new command blends from wherever the arm is now
  Interrupting a wave with "stop" must not snap back to a start pose first.
  Each timeline begins at the current pose, so any change is continuous.

Decision: velocity is capped per tick, on top of the easing
  A cheap SG90 under load can't follow a 1000 °/s step; the cap keeps fast
  gestures inside what the hardware can actually do.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

JOINTS = ("base", "shoulder", "elbow", "claw")
LIMITS = {"base": (10, 170), "shoulder": (40, 150), "elbow": (40, 160), "claw": (0, 60)}
HOME = {"base": 90.0, "shoulder": 90.0, "elbow": 90.0, "claw": 15.0}
TICK_HZ = 50
MAX_DEG_PER_S = 360.0

Pose = dict[str, float]
# A gesture is a list of (duration_ms, partial pose). Joints left out hold still.
Keyframes = list[tuple[int, Pose]]


def _repeat(frames: Keyframes, n: int) -> Keyframes:
    return [f for _ in range(n) for f in frames]


GESTURES: dict[str, Keyframes] = {
    "rest": [(700, HOME)],
    "wave": [
        (500, {"shoulder": 70, "elbow": 45, "claw": 0, "base": 90}),
        *_repeat([(320, {"base": 62, "elbow": 38}), (320, {"base": 118, "elbow": 52})], 3),
        (350, {"base": 90}),
        (600, HOME),
    ],
    "point": [
        (550, {"shoulder": 112, "elbow": 72, "claw": 55}),   # upper arm forward, forearm level
        (250, {"shoulder": 118, "elbow": 66}),               # small jab
        (900, {}),                                   # hold the point
        (600, HOME),
    ],
    "nod": [
        (350, {"shoulder": 95, "elbow": 80}),
        *_repeat([(260, {"elbow": 115}), (260, {"elbow": 80})], 3),
        (450, HOME),
    ],
    "shake_no": [
        (300, {"shoulder": 85, "elbow": 75}),
        *_repeat([(220, {"base": 72}), (220, {"base": 108})], 3),
        (300, {"base": 90}),
        (450, HOME),
    ],
    "bow": [
        (900, {"shoulder": 140, "elbow": 135, "claw": 10}),
        (900, {}),                                   # hold the bow
        (900, HOME),
    ],
    "grab": [
        (500, {"claw": 0}),
        (800, {"shoulder": 132, "elbow": 140}),      # reach down
        (450, {"claw": 58}),                         # close
        (300, {}),
        (800, {"shoulder": 95, "elbow": 95}),        # lift, keep holding
    ],
    "release": [
        (800, {"shoulder": 130, "elbow": 138}),      # lower
        (450, {"claw": 0}),                          # open
        (300, {}),
        (800, HOME),
    ],
    "dance": [
        (400, {"shoulder": 75, "elbow": 60}),
        *_repeat([
            (280, {"base": 65, "shoulder": 70, "elbow": 50, "claw": 50}),
            (280, {"base": 115, "shoulder": 95, "elbow": 75, "claw": 5}),
        ], 4),
        (300, {"base": 90, "shoulder": 60, "elbow": 40}),
        (250, {"claw": 55}), (250, {"claw": 0}),
        (600, HOME),
    ],
}
GESTURE_NAMES = sorted(GESTURES) + ["random"]


def clamp_pose(p: Pose) -> Pose:
    return {j: max(LIMITS[j][0], min(LIMITS[j][1], float(v))) for j, v in p.items() if j in LIMITS}


def speed_factor(speed_score: Optional[float]) -> float:
    """Laya's speed score (0 slow .. 2 fast) → duration multiplier."""
    if speed_score is None:
        return 1.0
    s = max(0.0, min(2.0, speed_score))
    return 1.7 - 0.6 * s if s <= 1.0 else 1.1 - 0.5 * (s - 1.0)   # 0→1.7, 1→1.1, 2→0.6


def _ease(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * t)


@dataclass
class _Segment:
    start: Pose
    end: Pose
    duration_s: float


@dataclass
class _Timeline:
    name: str
    segments: list[_Segment]
    started_at: float = field(default_factory=time.monotonic)

    @property
    def total_s(self) -> float:
        return sum(s.duration_s for s in self.segments)


class MotionEngine:
    """Owns the arm pose; turns gestures into a smooth 50 Hz stream of poses."""

    def __init__(self, write: Callable[[Pose], None], on_pose: Optional[Callable[[Pose, Optional[str]], Awaitable[None]]] = None):
        self._write = write
        self._on_pose = on_pose
        self.pose: Pose = dict(HOME)
        self._timeline: Optional[_Timeline] = None
        self._task: Optional[asyncio.Task] = None
        self._tick = 0

    @property
    def playing(self) -> Optional[str]:
        return self._timeline.name if self._timeline else None

    def start(self) -> None:
        self._write(self.pose)
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def play(self, gesture: str, speed_score: Optional[float] = None) -> tuple[str, float]:
        """Start a gesture from the current pose. Returns (resolved name, seconds it will take)."""
        if gesture == "random":
            gesture = random.choice([g for g in GESTURES if g != "rest"])
        frames = GESTURES[gesture]
        k = speed_factor(speed_score)
        segments, cur = [], dict(self.pose)
        for dur_ms, target in frames:
            nxt = clamp_pose({**cur, **target})
            segments.append(_Segment(dict(cur), nxt, dur_ms * k / 1000))
            cur = nxt
        self._timeline = _Timeline(gesture, segments)
        return gesture, self._timeline.total_s

    def _sample(self, now: float) -> Optional[Pose]:
        tl = self._timeline
        if not tl:
            return None
        t = now - tl.started_at
        for seg in tl.segments:
            if t <= seg.duration_s:
                a = _ease(t / seg.duration_s) if seg.duration_s > 0 else 1.0
                return {j: seg.start[j] + (seg.end[j] - seg.start[j]) * a for j in JOINTS}
            t -= seg.duration_s
        self._timeline = None
        return dict(tl.segments[-1].end)

    async def _loop(self) -> None:
        period = 1.0 / TICK_HZ
        max_step = MAX_DEG_PER_S * period
        next_t = time.monotonic()
        while True:
            target = self._sample(time.monotonic())
            if target is not None:
                for j in JOINTS:
                    d = target[j] - self.pose[j]
                    self.pose[j] += max(-max_step, min(max_step, d))
                try:
                    self._write(self.pose)
                except Exception as e:  # a flaky I2C bus must not kill the loop
                    print(f"[arm] driver write failed: {e}")
            self._tick += 1
            if self._on_pose and (target is not None or self._tick % TICK_HZ == 0) and self._tick % 2 == 0:
                await self._on_pose(dict(self.pose), self.playing)   # ~25 Hz to screens
            next_t += period
            await asyncio.sleep(max(0.0, next_t - time.monotonic()))
