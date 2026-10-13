"""GRIDLOCK data feed: turns public Toronto data into small Scarborough-only JSON files for the GRIDLOCK traffic sim.

live.py    runs every 15 minutes inside TOSEER's update workflow
static.py  runs weekly in its own workflow (big downloads: GTFS, permits, buildings)
"""
