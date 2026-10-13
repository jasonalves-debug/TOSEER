"""GRIDLOCK feed entry point.

  python scripts/build_gridlock_feed.py live     every 15 min (TOSEER's update workflow)
  python scripts/build_gridlock_feed.py static   weekly (gridlock-static workflow)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from gridlock import live, static  # noqa: E402

if __name__ == "__main__":
    {"live": live.build, "static": static.build}[sys.argv[1] if len(sys.argv) > 1 else "live"]()
