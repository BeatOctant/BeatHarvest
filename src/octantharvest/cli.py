"""octantharvest command line. Settings: OctantSpecs octantharvest/config.md. Usage: README.md."""
import argparse
import json
import sys

from . import crawl, releases, settings


def cmd_crawl(s, a):
    cat = crawl.catalogue(s, full=a.full)
    print(f"catalogue: {cat}")
    print(f"archive: {crawl.download(s, cat)}")


def cmd_artists(s, a):
    if a.export:
        print(json.dumps(crawl.artists.export(s, crawl.latest_catalogue(s)), ensure_ascii=False, indent=1))
        return
    print(json.dumps(crawl.artists.fetch(s, crawl.latest_catalogue(s), budget=a.budget), ensure_ascii=False, indent=1))


def cmd_daily(s, a):
    print(json.dumps(releases.daily(s, fetch=not a.no_crawl), ensure_ascii=False, indent=1, default=str))


def cmd_models(s, a):
    print(json.dumps(releases.models(s), indent=1))


def cmd_first_release(s, a):
    r = releases.first(s)
    print(f"wrote {r['full']}")


def cmd_approve(s, a):
    v = releases.approve(s, a.version)
    print(f"approved {a.version}: {v['published']}")


def cmd_keygen(s, a):
    k = releases.keygen(s)
    print(f"wrote {s['signing']['key']} (keep it private; never commit it)")
    print(f"keyId {k['keyId']}\npublicKey {k['publicKey']}")
    print("set signing.publicKey to the public key to publish with it; receivers add it to OctantCompass's trusted_keys "
          "(the maintainer's key is bundled with OctantVector instead)")


def cmd_publish(s, a):
    p = releases.publish(s)
    idx = json.loads(p.read_text())
    print(f"wrote {p}: " + ", ".join(f"{e['dbVersion']} ({e['kind']}, {len(e['parts'])} parts)" for e in idx["entries"]))


def cmd_handoff(s, a):
    print(f"wrote {releases.handoff(s, a.version)}")


def cmd_status(s, a):
    print(json.dumps({"settings": str(settings.path()), "paths": {k: str(v) for k, v in s["paths"].items()},
                      "captures": [str(c) for c in crawl.captures(s["paths"]["catalogues"])],
                      "views": releases.check(s), "versions": releases.state(s)["versions"]},
                     ensure_ascii=False, indent=1))


def main(argv=None):
    ap = argparse.ArgumentParser(prog="octantharvest", description="Collect BeatSaver, build and publish the vector DB")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("crawl", help="capture today's catalogue (what changed since the last capture), then download "
                       "the zips the archive lacks")
    p.add_argument("--full", action="store_true", help="walk the whole BeatSaver catalogue instead of fetching what "
                   "changed since the last capture (repair)")
    p = sub.add_parser("artists", help="look up the map artists on MusicBrainz and their similar artists on ListenBrainz")
    p.add_argument("--budget", type=int, default=None, help="requests to each service in this run (default crawl.artists.budget; 0: no limit)")
    p.add_argument("--export", action="store_true", help="only write OctantVector's two artist files from the caches "
                   "(the daily run and first-release do it themselves)")
    p = sub.add_parser("daily", help="crawl, fetch models, compute, check calibration drift, write the next version "
                       "(for a scheduler)")
    p.add_argument("--no-crawl", action="store_true", help="use the newest complete capture and the archive as they are")
    sub.add_parser("models", help="fetch the model files the settings' views read into paths.models "
                   "(daily and first-release do it themselves)")
    sub.add_parser("first-release", help="compute and calibrate the settings' views and write the first version")
    sub.add_parser("keygen", help="make the signing key at signing.key and print its public key")
    p = sub.add_parser("approve", help="approve and sign a written version for publishing (contract 5)")
    p.add_argument("version", help="dbVersion, e.g. 2026.10.03")
    sub.add_parser("publish", help="write the files to upload and the contract 5 index for the approved versions")
    p = sub.add_parser("handoff", help="write an approved version's full as one tar to hand over without an index")
    p.add_argument("version", help="dbVersion, e.g. 2026.10.03")
    sub.add_parser("status", help="show the paths, the captures, whether each view can be computed (its encoder and "
                   "model files) and the written versions")
    a = ap.parse_args(argv)
    s = settings.load()
    {"crawl": cmd_crawl, "artists": cmd_artists, "daily": cmd_daily, "models": cmd_models,
     "first-release": cmd_first_release, "approve": cmd_approve, "keygen": cmd_keygen, "publish": cmd_publish,
     "handoff": cmd_handoff, "status": cmd_status}[a.cmd](s, a)


if __name__ == "__main__":
    sys.exit(main())
