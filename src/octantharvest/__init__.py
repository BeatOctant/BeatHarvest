"""OctantHarvest: the host that builds the distributed DB (OctantSpecs constitution.md, principle 4b).

  crawl/       collecting from BeatSaver (the output is contract 4 (OctantHarvest crawl to OctantVector))
  releases.py  the daily run, approval and publishing, calling OctantVector's generation functions (contract 1 (OctantHarvest to OctantVector))
  settings.py  the settings file; its defaults are in defaults.json, bundled with the package
"""
from datetime import datetime, timezone


def now():
    """UTC now to the second, as written in every record (state.json, daily.jsonl, models.json, the artist caches)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
