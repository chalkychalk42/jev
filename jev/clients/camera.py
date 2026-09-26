"""Point the camera at the world. Everything that looks depends on this and nothing set it.

A pitched-down camera is invisible in every log the bot writes. The strip still decodes,
the position is still right, nameplates still draw at the top of the screen — and the unit
they belong to is not on screen at all, because the camera is looking at the character's
feet from directly above. `Interact` reports `not_visible`, `Fight` reports "selected, but
no ring and nameplate to click", and both are telling the truth about a screen that has
nothing on it.

That is what an evening of `not_visible` in Echo Ridge was. The camera had drifted to
near-vertical and nothing in the codebase could see that, let alone fix it: the bot could
walk to a merchant, stand 4.6 yards away with him targeted and his nameplate on screen,
and still fail to click him.

Why pitch can be set at all
---------------------------
Mouse-look is relative, so there is no "set pitch to X". But it **clamps**: drag down far
enough and the camera stops looking straight down, however it started. A clamp is an
absolute reference, so the sequence is drag hard into the stop, then come back a measured
amount. From the bottom stop, 500 pixels of upward drag is level at the pinned Windows
pointer speed 10 — measured live at Dermot Johns' stall in Northshire, where the same
frame went from bare ground to three merchants under an awning.

This deliberately does not read the frame to decide. A horizon detector would be a second
thing to be wrong, and the clamp already makes the result repeatable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from jev.run.evidence import event, traced

# Far enough to reach the stop from any starting pitch, with room to spare. Overshooting
# into a clamp costs nothing - that is the entire point of using one as a reference.
#
# And it has to be generous, because getting this wrong does not look like a short drag:
# it looks like `level()` not being absolute any more. 900 was measured alongside
# `LEVEL_PX` at pointer speed 11; pinning the pointer to 10 cut every delta by a fifth,
# 900 stopped reaching the clamp, and the resulting pitch depended on wherever the camera
# already was. Two calls in a row put the same nameplate at y=441 and then y=211.
TO_THE_STOP_PX = 2000

# From the bottom stop, how far up is level. Measured, not derived: it is the client's
# pitch range times whatever the operating system did to the deltas on the way.
#
# **This number is only meaningful with the pointer pinned** - see `tools/env_lock.py`.
# It was first measured as 400 at Windows pointer speed 11, which scales every relative
# delta by 1.25; the machine is now pinned to the neutral 10. Re-measured there with
# `tools/calibrate_camera.py` at Dermot Johns' stall: 400 still looks at the ground, 600
# looks into the canopy, 500 is level. That the arithmetic (400 x 1.25) and the
# measurement agree is the answer to whether the client reads the scaled cursor or the
# raw device - it reads the scaled one, so pointer speed matters and is pinned. Both tools
# went with V226; `git show 845f880:tools/env_lock.py` still checks and pins the pointer.
LEVEL_PX = 500

# Mouse-look has to be held across the movement, and the client needs a moment either side
# to notice the button before the deltas start arriving.
GRAB_S = 0.15
SETTLE_S = 0.6

# Enough movement that releasing the button ends a drag, not a right-click on whatever is
# under the pointer; small enough to leave the heading where it was.
NUDGE_PX = 8


@dataclass
class Camera:
    hid: object
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)
    _calibrated_geometry: tuple | None = field(default=None, init=False, repr=False)
    _generation: int = field(default=0, init=False, repr=False)

    def invalidate(self, reason: str = "camera control changed") -> None:
        """Forget calibration when ownership or session continuity is lost."""
        if self._calibrated_geometry is not None:
            event("camera.invalidated", detail=reason)
        self._generation += 1
        self._calibrated_geometry = None

    @property
    def calibrated(self) -> bool:
        return self._calibrated_geometry == (self.window_origin, self.window_size, self._generation)

    def ensure_level(self) -> bool:
        """Reuse this session's completed calibration until explicitly invalidated.

        Movement, target changes and missing nameplates do not change camera pitch.
        The runtime invalidates on observed focus loss and before reconnecting; callers
        that deliberately change the camera must invalidate it too.
        """
        if self.calibrated:
            event("camera.ready", code="retained")
            return True
        return self.level()

    @traced("camera.face")
    def face(self) -> bool:
        """Turn the character to where the camera looks: hold mouse-look and nudge.

        Mouse-look turns the character to the camera's heading as soon as it starts, and
        leaves the pitch as it was. Half a second, where `level()` - a drag into the pitch
        clamp and back - took 5.4 s in the middle of a fight at 40% health, the rotation
        stopped through it, and the character died at its end (run 20260924T121445-3d2ab1).
        """
        ox, oy = self.window_origin
        w, h = self.window_size
        if not self.hid.move_to(ox + w // 2, oy + h // 2):
            return False
        time.sleep(GRAB_S)
        if not self.hid.button(True, right=True):
            return False
        try:
            time.sleep(GRAB_S)
            if not self.hid.move_by(NUDGE_PX, 0) or not self.hid.move_by(-NUDGE_PX, 0):
                return False
            time.sleep(GRAB_S)
        finally:
            released = self.hid.button(False, right=True)
        return bool(released)

    @traced("camera.calibrate")
    def level(self) -> bool:
        """Force the measured stop-and-return sequence; diagnostics use this explicitly."""
        self.invalidate("calibration started")
        generation = self._generation
        ox, oy = self.window_origin
        w, h = self.window_size
        if not self.hid.move_to(ox + w // 2, oy + h // 2):
            return False
        time.sleep(GRAB_S)

        # The cursor is captured from here, so nothing underneath it is clicked: a right
        # button that moves is mouse-look, not a click on the merchant we are stood in
        # front of.
        if not self.hid.button(True, right=True):
            return False
        try:
            time.sleep(GRAB_S)
            if not self.hid.move_by(0, TO_THE_STOP_PX):
                return False                     # no confirmed stop to measure from
            if not self.hid.move_by(0, -LEVEL_PX):
                return False                     # the return drag was not delivered
            time.sleep(GRAB_S)
        finally:
            released = self.hid.button(False, right=True)
        time.sleep(SETTLE_S)
        if released and self._generation == generation:
            self._calibrated_geometry = (self.window_origin, self.window_size, generation)
            return True
        return False
