"""Software end-stops for pan/tilt cameras that haven't been calibrated.

An uncalibrated camera (get_all_pos answers -5) doesn't know where its
mechanical ends are, so it grinds if driven into them.  We estimate position
by timing every move (units: seconds of motor travel, since the motor speed is
unknown) and let the user mark the limits they want it to stop at.

The estimate drifts a little over time; re-mark the limits if it does.
"""
from __future__ import annotations

from typing import Any

# direction -> (axis, sign); pan grows to the right, tilt grows upwards
DIRECTIONS = {"left": ("pan", -1), "right": ("pan", 1), "up": ("tilt", 1), "down": ("tilt", -1)}


class SoftLimits:
    """Position estimate and user-marked limits, stored in a plain dict."""

    def __init__(self, state: dict[str, Any]) -> None:
        # ``state`` lives in cameras.json, so changes persist when saved.
        self.state = state
        state.setdefault("pan", 0.0)
        state.setdefault("tilt", 0.0)
        state.setdefault("limits", {"left": None, "right": None, "up": None, "down": None})

    def position(self, moving: str | None = None, elapsed: float = 0.0) -> dict[str, float]:
        """Current estimate, including a move in progress."""
        pos = {"pan": float(self.state["pan"]), "tilt": float(self.state["tilt"])}
        if moving in DIRECTIONS:
            axis, sign = DIRECTIONS[moving]
            pos[axis] += sign * elapsed
        return pos

    def remaining(self, direction: str, pos: dict[str, float]) -> float | None:
        """Seconds of travel left before the limit in ``direction`` (None = no limit)."""
        limit = self.state["limits"].get(direction)
        if limit is None:
            return None
        axis, sign = DIRECTIONS[direction]
        return max(0.0, sign * (limit - pos[axis]))

    def commit(self, direction: str, elapsed: float) -> None:
        """Add a finished move to the estimate."""
        axis, sign = DIRECTIONS[direction]
        new = self.state[axis] + sign * max(0.0, elapsed)
        # A limit is a hard stop: the estimate can't go past it.
        low = self.state["limits"]["left" if axis == "pan" else "down"]
        high = self.state["limits"]["right" if axis == "pan" else "up"]
        if low is not None:
            new = max(new, low)
        if high is not None:
            new = min(new, high)
        self.state[axis] = round(new, 3)

    def mark(self, edge: str) -> None:
        """Make the current position the limit for ``edge``."""
        axis, _ = DIRECTIONS[edge]
        self.state["limits"][edge] = self.state[axis]

    def clear(self) -> None:
        """Forget all limits and treat the current position as the centre."""
        self.state.update(pan=0.0, tilt=0.0)
        self.state["limits"] = {"left": None, "right": None, "up": None, "down": None}

    def summary(self) -> dict[str, Any]:
        return {
            "pan": self.state["pan"],
            "tilt": self.state["tilt"],
            "limits": dict(self.state["limits"]),
        }
