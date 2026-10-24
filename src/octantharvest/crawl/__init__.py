"""Collecting from BeatSaver (OctantSpecs constitution.md, principle 4b; the output is contract 4 (OctantHarvest crawl to OctantVector)).

The two scripts in this folder do the work and run as separate processes, as they did before they were brought in:
  beatsaver_catalogue_capture.py  a complete metadata catalogue (catalogue.jsonl.gz), one capture per run: a walk of
                                  the whole catalogue, or a diff (the previous capture's catalogue updated with what
                                  changed since)
  beatsaver_bulk_download.py      every version's zip in the catalogue, once, into the archive
This module runs them with the numbers set in the settings (the rest keep the scripts' defaults) and finds their
results.
artists.py (in process) looks the map artists up on MusicBrainz and fetches their ListenBrainz similar artists.
"""
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import artists  # noqa: F401  (crawl.artists)
from .beatsaver_catalogue_capture import base_record, full_walk_at

HERE = Path(__file__).parent
CAPTURE = HERE / "beatsaver_catalogue_capture.py"
DOWNLOAD = HERE / "beatsaver_bulk_download.py"


def _flags(values, flags):
    """The command-line flags for the settings that are set; an unset one keeps the script's default."""
    return [x for key, flag in flags.items() if values[key] is not None for x in (flag, values[key])]


def _run(cmd, log):
    print("$ " + " ".join(map(str, cmd)), file=log, flush=True)
    return subprocess.run([str(c) for c in cmd]).returncode


def captures(root):
    """Complete captures under `root`, oldest first (a capture is complete once it has manifest.json)."""
    out = []
    for d in sorted(Path(root).glob("*/")):
        m = d / "manifest.json"
        if m.exists() and json.loads(m.read_text()).get("complete") is True:
            out.append(d)
    return out


def latest_catalogue(s):
    done = captures(s["paths"]["catalogues"])
    if not done:
        raise SystemExit(f"no complete catalogue capture under {s['paths']['catalogues']}; run `octantharvest crawl`")
    return done[-1] / "catalogue.jsonl.gz"


def _base(s, out, now, log):
    """The capture `out` starts from: the newest complete one, unless a walk of the whole catalogue is due (the last
    one it builds on began crawl.fullWalkDays days ago or more). None: walk the whole catalogue. A damaged newest
    capture stops the crawl before today's capture is begun."""
    done = [d for d in captures(s["paths"]["catalogues"]) if d != out]
    if not done:
        return None
    days = int(s["crawl"]["fullWalkDays"])
    walked = datetime.fromisoformat(full_walk_at(json.loads((done[-1] / "manifest.json").read_text())))
    if days > 0 and now - walked >= timedelta(days=days):
        print(f"catalogue: the last full walk began {walked:%Y-%m-%d}, {days} days or more ago", file=log)
        return None
    try:
        base_record(done[-1])
    except (RuntimeError, OSError) as e:
        raise SystemExit(f"catalogue: {e}. Remove {done[-1]} to start from the capture before it, or walk the whole "
                         "catalogue with `octantharvest crawl --full`") from None
    return done[-1]


def catalogue(s, today=None, log=sys.stderr, run=_run, full=False):
    """Capture today's catalogue (resuming today's capture if one was interrupted) -> catalogue.jsonl.gz. It starts
    from the newest complete capture and fetches only what changed since; it walks the whole catalogue when there is
    none, when a full walk is due (_base) or when `full` is set (repair)."""
    c = s["crawl"]["catalogue"]
    now = today or datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    out = Path(s["paths"]["catalogues"]) / day
    cmd = [sys.executable, CAPTURE, "capture", "--output", out] + _flags(c, {
        "minDelay": "--min-delay", "timeout": "--timeout", "maxRetries": "--max-retries", "userAgent": "--user-agent"})
    if full and out.exists():
        raise SystemExit(f"catalogue {day} is already started or complete; `octantharvest crawl` resumes it as it "
                         f"began. To walk the whole catalogue instead, remove {out} first")
    if (out / "manifest.json").exists():
        print(f"catalogue {day}: already complete", file=log)
    else:
        if (out / "capture-state.json").exists():  # a capture that is resumed goes on as it began
            base = None
            print(f"catalogue {day}: resuming it as it began", file=log)
        else:
            base = None if full else _base(s, out, now, log)
            print(f"catalogue {day}: " + (f"what changed since {base.name}" if base else "the whole catalogue"),
                  file=log)
        code = run(cmd + (["--base", base] if base else []), log)
        if code != 0:
            raise SystemExit(f"catalogue capture stopped (exit {code}); running again resumes it")
    _prune(s, log)
    return out / "catalogue.jsonl.gz"


def _prune(s, log):
    """Delete all but the newest crawl.keepCaptures complete captures. The newest, which the next run starts from,
    is always kept."""
    keep = int(s["crawl"]["keepCaptures"])
    done = captures(s["paths"]["catalogues"])
    for d in done[:-keep] if keep > 0 else []:
        print(f"removing old capture {d}", file=log)
        shutil.rmtree(d)


def download(s, catalogue_path, log=sys.stderr, run=_run):
    """Fetch every zip of the catalogue not yet in the archive. -> "complete" or "incomplete" (some versions are
    blocked or still deferred; what was verified is usable and a later run continues)."""
    d = s["crawl"]["download"]
    cmd = [sys.executable, DOWNLOAD, catalogue_path, s["paths"]["archive"]] + _flags(d, {
        "concurrency": "--concurrency", "rps": "--rps", "timeout": "--timeout", "retriesPerCycle": "--retries-per-cycle",
        "maxRetryCycles": "--max-retry-cycles", "giveUpAfterRuns": "--give-up-after-runs",
        "recheckGivenUpDays": "--recheck-given-up-days", "maxFileMib": "--max-file-mib", "minFreeGib": "--min-free-gib",
        "stagingGib": "--staging-gib", "userAgent": "--user-agent"})
    code = run(cmd, log)
    if code == 0:
        return "complete"
    if code == 1:
        return "incomplete"
    raise SystemExit(f"archive download stopped (exit {code}); running again resumes it")
