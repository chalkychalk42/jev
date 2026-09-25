#!/usr/bin/env python3
"""The interface's red button plates in a kept frame, as fractions of the frame: to measure
a glue screen's buttons (`jev.clients.session`) from `tools/character.py measure`'s PNG.

    .venv/bin/python tools/find_plates.py captures/glue/<frame>.png

Read-only. A plate is a connected run of the red `_is_red_button` samples (red above 70 and
45 above green and blue), wider than it is tall, of a button's size.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage

RED_MIN, RED_MARGIN = 70, 45


def plates(frame: np.ndarray) -> list[tuple[float, float, int, int]]:
    rgb = frame[:, :, :3].astype(np.int16)
    r, g, b = rgb[:, :, 0], rgb[:, :, 1], rgb[:, :, 2]
    mask = (r > RED_MIN) & (r - g > RED_MARGIN) & (r - b > RED_MARGIN)
    labels, _ = ndimage.label(mask)
    h, w = mask.shape
    out = []
    for region in ndimage.find_objects(labels):
        ys, xs = region
        height, width = ys.stop - ys.start, xs.stop - xs.start
        if width >= 0.04 * w and 0.012 * h <= height <= 0.08 * h and width > 2 * height:
            out.append(((xs.start + xs.stop) / 2 / w, (ys.start + ys.stop) / 2 / h, width, height))
    return sorted(out, key=lambda p: (round(p[1], 2), p[0]))


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    frame = np.asarray(Image.open(Path(args[0])).convert("RGB"))
    for x, y, width, height in plates(frame):
        print(f"({x:.4f}, {y:.4f})  {width}x{height} px")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
