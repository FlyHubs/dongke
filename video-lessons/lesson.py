#!/usr/bin/env python3
"""Independent entry point for dongke's frame-by-frame question videos (MIT)."""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parent.parent
LESSONS = {
    "poem-coordinates": ("古诗里的位置：有序数对与平移", REPO / "lessons/video/poem-coordinates"),
}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["list", "validate", "preview", "frame", "render", "tts"])
    parser.add_argument("lesson", nargs="?", choices=sorted(LESSONS))
    args, extra = parser.parse_known_args(argv)
    if args.command == "list":
        if args.lesson or extra:
            parser.error("list does not accept lesson options")
        for slug, (title, _) in LESSONS.items():
            print(f"{slug}: {title}")
        return 0
    if not args.lesson:
        parser.error("choose a lesson; run 'list' to see available lessons")
    _, directory = LESSONS[args.lesson]
    script = directory / ("generate_tts.py" if args.command == "tts" else "render_lesson.py")
    switches = {"validate": ["--validate"], "preview": ["--preview"], "frame": ["--frame"],
                "render": [], "tts": []}
    return subprocess.run([sys.executable, str(script), *switches[args.command], *extra], check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
