"""Review a run's recorded frames four to a sheet. Reads images; touches nothing live.

    python tools/contact_sheet.py runs/RUN/screenshots --start 120 --count 8

Prints JSON with the sheet paths, the frames covered, the next index to review and any
frames the recorder reported missing, so a supervised run can be read in order.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def sheets(directory: Path, start: int, count: int, *, per_sheet: int = 4,
           tile: tuple[int, int] = (805, 453)) -> dict:
    from PIL import Image, ImageDraw

    rows = []
    for line in (directory / "manifest.jsonl").read_text().splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            break                                   # a crash tail is not a frame
    selected = [row for row in rows if row["index"] >= start][:count]
    written = []
    for offset in range(0, len(selected), per_sheet):
        group = selected[offset:offset + per_sheet]
        columns = 2
        height = (tile[1] + 27) * ((len(group) + columns - 1) // columns)
        canvas = Image.new("RGB", (tile[0] * columns, height), (20, 20, 20))
        draw = ImageDraw.Draw(canvas)
        for i, row in enumerate(group):
            x, y = (i % columns) * tile[0], (i // columns) * (tile[1] + 27)
            if row.get("status") == "ok":
                with Image.open(directory / row["file"]) as image:
                    image.thumbnail(tile)
                    canvas.paste(image, (x, y + 25))
            draw.text((x + 5, y + 5), f"frame {row['index']}  {row['kind']}  "
                      f"{row.get('label', '')}  {row.get('status')}", fill="white")
        path = directory / f"sheet-{group[0]['index']:06d}-{group[-1]['index']:06d}.png"
        canvas.save(path)
        written.append(str(path))
    return {"total": len(rows), "indices": [row["index"] for row in selected],
            "next": selected[-1]["index"] + 1 if selected else start, "sheets": written,
            "missing": [row["index"] for row in rows if row.get("status") != "ok"],
            "skipped_slots": max((row.get("skipped_slots", 0) for row in rows), default=0)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args(argv)
    print(json.dumps(sheets(args.directory, args.start, args.count)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
