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
amount. From the bottom stop, 400 pixels of upward drag is level — measured live at
Dermot Johns' stall in Northshire, where the same frame went from bare ground to three
merchants under an awning.

This deliberately does not read the frame to decide. A horizon detector would be a second
thing to be wrong, and the clamp already makes the result repeatable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

# Far enough to reach the stop from any starting pitch, with room to spare. Overshooting
# into a clamp costs nothing - that is the entire point of using one as a reference.
TO_THE_STOP_PX = 900

# From the bottom stop, how far up is level. Measured, not derived: it is the client's
# pitch range times whatever the operating system did to the deltas on the way.
#
# **This number is only meaningful with the pointer pinned** - see `tools/env_lock.py`.
# It was first measured at Windows pointer speed 11, which scales every relative delta by
# 1.25, and the machine has since been pinned to the neutral 10. Whether that makes the
# true value 500 depends on whether the client reads the scaled cursor or the raw device,
# and that is not something to reason about: `tools/calibrate_camera.py` drags a given
# number of pixels and shows the result, which settles it in one run.
LEVEL_PX = 400

# Mouse-look has to be held across the movement, and the client needs a moment either side
# to notice the button before the deltas start arriving.
GRAB_S = 0.15
SETTLE_S = 0.6


@dataclass
class Camera:
    hid: object
    window_origin: tuple[int, int] = (0, 0)
    window_size: tuple[int, int] = (1600, 900)

    def level(self) -> bool:
        """Put the camera back on the horizon. Safe to call when it is already there.

        Cheap enough to do before looking rather than only after failing to see: about a
        second, no keystrokes, and it cannot make a good camera worse because the stop is
        where it measures from either way.
        """
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
            self.hid.move_by(0, TO_THE_STOP_PX)     # into the bottom stop
            self.hid.move_by(0, -LEVEL_PX)          # back up to level
            time.sleep(GRAB_S)
        finally:
            self.hid.button(False, right=True)
        time.sleep(SETTLE_S)
        return True
