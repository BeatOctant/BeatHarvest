"""OctantHarvest's settings: one JSON file and its keys (OctantSpecs octantharvest/config.md describes them).

The defaults are in defaults.json next to this file, bundled with the package; that file is the one place they are
written. The settings file is $OCTANTHARVEST_SETTINGS or ./octantharvest.json and holds only what its writer set: keys it
leaves out keep the defaults, and an unknown key is an error. Relative paths are relative to the file's folder. The
crawl scripts' numbers default to null: they are passed only when set, so the scripts' own defaults (which also serve
running them by hand) are the one place those values are.

  paths.catalogues          one folder per capture, named by its UTC date (beatsaver_catalogue_capture); each starts
                            from the newest complete one and fetches only what changed since (crawl.fullWalkDays)
  paths.archive             maps/<hash[:2]>/<hash>.zip and download.journal.jsonl (beatsaver_bulk_download)
  paths.store               OctantVector's working store and ledger
  paths.releases            versions written by OctantVector's release, state.json and daily.jsonl
  paths.publish             what to upload: the split files and index.json (contract 5 (OctantHarvest to OctantCompass))
  paths.artists             MusicBrainz IDs of the map artists and their ListenBrainz similar artists (crawl/artists.py)
  paths.models              the model files OctantVector reads (models_dir): hf/<repo>/<revision>/ and models.json
  crawl.catalogue, crawl.download
                            the scripts' numbers; null: the script's default (beatsaver_catalogue_capture.py /
                            beatsaver_bulk_download.py --help). download.maxRetryCycles: extra passes over the
                            deferred zips in one run (0: until they pass); giveUpAfterRuns: a zip still deferred at
                            the end of this many runs is given up; recheckGivenUpDays: a given-up zip is tried again
                            after this many days (0: never)
  crawl.artists             `octantharvest artists`. inflight: requests waiting for an answer at once per service
                            (starts stay minDelay apart); userAgent: required, MusicBrainz asks for a contact in it;
                            budget: requests to each service in one run, most-mapped artists first (0: no limit);
                            minMaps: names on fewer maps are not looked up
  crawl.keepCaptures        complete catalogue captures to keep; older ones are deleted (0: keep all). The newest,
                            which the next capture starts from, is always kept
  crawl.fullWalkDays        a capture walks the whole catalogue again when the last full walk it builds on began
                            this many days ago (0: only with `crawl --full` or without a complete capture); stats
                            other than votes, and changes that do not move a map's updatedAt, are refreshed then
  views                     OctantVector views (id@version) to compute and release, and relation views OctantVector builds
                            at release from the catalogue (beatsaver_mappers); required by daily and first-release
                            (there is no default list)
  octantvector                params for OctantVector's generation functions (OctantSpecs octantvector/config.md)
  presets                   JSON file with the hands (手札) release writes into the DB (OctantSpecs plans/presets.md §8);
                            null: keep the previous version's
  notice                    Markdown file with the DB's terms and credits release writes into the manifest (DB-NOTICE.md;
                            each view's credits come from its encoder); null: keep the previous version's
  recalibrateDrift          a view whose mean moved more than this gets a new calibration (a full release)
  signing.key               the maintainer's private key file that approval signs with (OctantSpecs contracts/distribution.md, 署名)
  signing.publicKey         its public key (base64, as `keygen` prints it), passed to OctantVector's build_index as a
                            trusted key: publish indexes only versions signed by a key OctantVector bundles or this
                            one (null: the bundled keys only, as for the maintainer's key)
  signing.allowUnsigned     development: approve without a key, index unsigned versions
  publish.partSizeMb        largest file (GitHub Releases: 2 GB)
  publish.url               URL prefix of the files
"""
import copy
import json
import os
from importlib import resources
from pathlib import Path

DEFAULTS = json.loads(resources.files(__package__).joinpath("defaults.json").read_text(encoding="utf-8"))
FREE = {"octantvector"}  # passed through as given; OctantVector checks the keys


def path():
    return Path(os.environ.get("OCTANTHARVEST_SETTINGS", "octantharvest.json"))


def _merge(base, over, where=""):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k not in base:
            raise SystemExit(f"unknown setting {where}{k} (OctantSpecs octantharvest/config.md)")
        if isinstance(base[k], dict) and k not in FREE:
            if not isinstance(v, dict):
                raise SystemExit(f"setting {where}{k} must be an object")
            out[k] = _merge(base[k], v, f"{where}{k}.")
        else:
            out[k] = v
    return out


def load(p=None):
    """-> the settings with defaults filled in and paths made absolute."""
    p = Path(p) if p is not None else path()
    given = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    s = _merge(DEFAULTS, given)
    root = p.resolve().parent

    def absolute(v):
        return (root / Path(v).expanduser()).resolve() if v else v

    s["paths"] = {k: absolute(v) for k, v in s["paths"].items()}
    s["presets"] = absolute(s["presets"])
    s["notice"] = absolute(s["notice"])
    s["signing"]["key"] = absolute(s["signing"]["key"])
    return s
