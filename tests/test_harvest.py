"""Settings, the crawl commands, and the daily run -> approval -> index with OctantVector's generation functions."""
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path

import numpy as np
import pytest

from octantharvest import cli, crawl, releases, settings
from octantharvest.crawl import artists

from conftest import VIEWS, write_capture

AT = datetime(2026, 10, 2, tzinfo=timezone.utc)


def test_settings_fill_defaults_and_refuse_unknown_keys(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"paths": {"store": "/data/store"}, "crawl": {"download": {"concurrency": 1}}}))
    s = settings.load(p)
    assert s["crawl"]["download"] == {**settings.DEFAULTS["crawl"]["download"], "concurrency": 1}
    assert str(s["paths"]["store"]) == "/data/store" and s["paths"]["archive"] == tmp_path / "archive"
    assert settings.load(tmp_path / "missing.json")["recalibrateDrift"] == settings.DEFAULTS["recalibrateDrift"]
    for bad in ({"crawl": {"donwload": {}}}, {"publish": {"partSize": 1}}, {"paths": "x"}):
        p.write_text(json.dumps(bad))
        with pytest.raises(SystemExit):
            settings.load(p)
    p.write_text(json.dumps({"octantvector": {"anything": 1}}))  # OctantVector checks its own params
    assert settings.load(p)["octantvector"] == {"anything": 1}


def test_crawl_runs_the_scripts_with_the_settings(harvest):
    tmp_path, s, ms = harvest
    calls = []

    def run(cmd, log):
        calls.append([str(c) for c in cmd])
        if "capture" in calls[-1]:
            write_capture(tmp_path / "catalogue", "2026-10-02", ms)
        return 0

    s["crawl"]["catalogue"]["minDelay"] = 2.0
    s["crawl"]["download"]["concurrency"] = 4
    cat = crawl.catalogue(s, today=AT, log=StringIO(), run=run)
    assert cat == tmp_path / "catalogue" / "2026-10-02" / "catalogue.jsonl.gz"
    assert calls[0][1].endswith("beatsaver_catalogue_capture.py") and calls[0][calls[0].index("--min-delay") + 1] == "2.0"
    assert "--timeout" not in calls[0]  # not set: the script's own default
    assert crawl.catalogue(s, today=AT, log=StringIO(), run=run) == cat and len(calls) == 1  # done: not run again
    assert crawl.download(s, cat, log=StringIO(), run=run) == "complete"
    d = calls[-1]
    assert d[1].endswith("beatsaver_bulk_download.py") and d[d.index("--concurrency") + 1] == "4" and d[3] == str(s["paths"]["archive"])
    assert "--max-retry-cycles" not in d
    assert crawl.download(s, cat, log=StringIO(), run=lambda c, l: 1) == "incomplete"
    with pytest.raises(SystemExit):
        crawl.download(s, cat, log=StringIO(), run=lambda c, l: 130)
    with pytest.raises(SystemExit):
        crawl.catalogue({**s}, today=AT + timedelta(days=1), log=StringIO(), run=lambda c, l: 2)
    s["crawl"]["keepCaptures"] = 1
    crawl._prune(s, StringIO())
    assert [c.name for c in crawl.captures(tmp_path / "catalogue")] == ["2026-10-02"]


def test_the_crawl_scripts_still_run():
    out = subprocess.run([sys.executable, crawl.CAPTURE, "self-test"], capture_output=True, text=True)
    assert out.returncode == 0 and "self-test: OK" in out.stdout
    out = subprocess.run([sys.executable, crawl.DOWNLOAD, "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "--user-agent" in out.stdout


def test_daily_approve_publish(harvest, monkeypatch):
    tmp_path, s, ms = harvest
    rec = releases.daily(s, fetch=False, log=StringIO())
    assert rec["release"].startswith("skipped") and rec["compute"]["computed"] == 72  # with swing_flow (Pass, Tech)
    with pytest.raises(SystemExit):
        releases.publish(s)  # nothing written, nothing approved

    first = releases.first(s, built_at=AT, log=StringIO())
    with pytest.raises(SystemExit):
        releases.first(s, log=StringIO())
    rec = releases.daily(s, fetch=False, built_at=AT + timedelta(days=1), log=StringIO())
    assert rec["compute"]["computed"] == 0 and max(rec["drift"].values()) < 1e-6
    assert rec["release"]["published_as"] == "diff"
    second = rec["release"]["version"]

    for m in ms[:12]:
        m["tags"] = ["metal", "anime", "jazz"]  # half the maps change their tags: meta_tags moves
    write_capture(tmp_path / "catalogue", "2026-10-03", ms)
    rec = releases.daily(s, fetch=False, built_at=AT + timedelta(days=2), log=StringIO())
    assert "meta_tags@2" in rec["recalibrated"]["because"] and rec["release"]["published_as"] == "full"
    lines = (tmp_path / "releases" / "daily.jsonl").read_text().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["release"].startswith("skipped")

    with pytest.raises(SystemExit):
        releases.approve(s, "1999.01.01")
    with pytest.raises(SystemExit, match="no signing key"):
        releases.approve(s, first["dbVersion"])
    s["signing"]["allowUnsigned"] = True  # development: approved without a signature, still indexable
    releases.approve(s, first["dbVersion"])
    assert not (Path(first["full"]) / "manifest.sig").exists()
    s["signing"]["allowUnsigned"] = False
    s["signing"]["key"] = tmp_path / "keys" / "maintainer.pem"
    key = releases.keygen(s)
    with pytest.raises(SystemExit, match="never overwritten"):
        releases.keygen(s)
    from octantvector.common import trusted_keys  # as if OctantVector bundled the maintainer's public key
    monkeypatch.setattr(trusted_keys, "KEYS", {key["keyId"]: key["publicKey"]})
    releases.approve(s, first["dbVersion"])
    assert json.loads((Path(first["full"]) / "manifest.sig").read_text())["keyId"] == key["keyId"]
    releases.approve(s, second)
    idx = json.loads(releases.publish(s).read_text())
    assert [(e["dbVersion"], e["kind"]) for e in idx["entries"]] == [(first["dbVersion"], "full"), (second, "diff")]
    assert len(idx["entries"][0]["parts"]) > 1 and idx["entries"][0]["parts"][0]["url"] is None

    with pytest.raises(SystemExit, match="not approved"):
        releases.handoff(s, rec["release"]["version"])
    tar = releases.handoff(s, second)  # published as a diff: its full is signed for the hand-off
    assert tar == tmp_path / "publish" / "handoff" / f"{second}.full.tar"
    assert json.loads((Path(releases.state(s)["versions"][second]["full"]) / "manifest.sig").read_text())["keyId"] == \
        key["keyId"]
    import warnings
    from octantvector import runtime
    with warnings.catch_warnings():
        warnings.simplefilter("error", runtime.SignatureWarning)
        assert runtime.open_db(runtime.import_archive(tar, tmp_path / "host" / second)).version == second


def test_a_key_octantvector_does_not_bundle_publishes_with_signing_public_key(harvest):
    """Anyone making their own DB (README, 自分の DB を作って配る) signs with their own key, which no OctantVector
    bundles: signing.publicKey is passed to build_index as a trusted key."""
    tmp_path, s, ms = harvest
    first = releases.first(s, built_at=AT, log=StringIO())
    s["signing"]["key"] = tmp_path / "keys" / "mine.pem"
    key = releases.keygen(s)
    from octantvector.common import trusted_keys
    assert key["keyId"] not in trusted_keys.KEYS
    releases.approve(s, first["dbVersion"])
    with pytest.raises(SystemExit, match=f"key {key['keyId']}, which this OctantVector does not know"):
        releases.publish(s)  # the bundled keys only
    s["signing"]["publicKey"] = key["publicKey"]
    idx = json.loads(releases.publish(s).read_text())
    assert [(e["dbVersion"], e["kind"]) for e in idx["entries"]] == [(first["dbVersion"], "full")]
    s["signing"]["publicKey"] = "not base64!"
    with pytest.raises(SystemExit, match="signing.publicKey is not base64"):
        releases.publish(s)


def test_a_generation_error_ends_the_run_with_its_message_and_is_logged(harvest):
    tmp_path, s, _ = harvest
    s["views"] = ["no_such_encoder"]
    with pytest.raises(SystemExit, match="^OctantVector: .*no_such_encoder"):  # a message, not a traceback
        releases.daily(s, fetch=False, log=StringIO())
    rec = json.loads((tmp_path / "releases" / "daily.jsonl").read_text().splitlines()[-1])
    assert "no_such_encoder" in rec["error"] and "compute" not in rec
    with pytest.raises(SystemExit, match="^OctantVector: .*no_such_encoder"):
        releases.first(s, log=StringIO())


def test_a_view_octantvector_cannot_compute_stops_the_run_before_crawling(harvest, monkeypatch):
    tmp_path, s, _ = harvest
    s["views"] = VIEWS + ["clap_htsat_unfused@1"]  # its plugin not installed
    monkeypatch.setattr(releases.crawl, "catalogue", lambda *a, **k: pytest.fail("crawled"))
    with pytest.raises(SystemExit, match="^OctantVector: clap_htsat_unfused@1: no such encoder"):
        releases.daily(s, log=StringIO())
    rec = json.loads((tmp_path / "releases" / "daily.jsonl").read_text().splitlines()[-1])
    assert "no such encoder" in rec["error"] and "crawl" not in rec


def test_another_exception_keeps_its_traceback(harvest, monkeypatch):
    tmp_path, s, _ = harvest
    import octantvector.generation
    monkeypatch.setattr(octantvector.generation, "compute", lambda *a, **k: 1 / 0)
    with pytest.raises(ZeroDivisionError):
        releases.daily(s, fetch=False, log=StringIO())
    rec = json.loads((tmp_path / "releases" / "daily.jsonl").read_text().splitlines()[-1])
    assert rec["error"].startswith("ZeroDivisionError")


def test_views_are_required(harvest):
    tmp_path, s, _ = harvest
    s["views"] = None
    for run in (lambda: releases.daily(s, fetch=False, log=StringIO()), lambda: releases.first(s, log=StringIO()),
                lambda: releases.models(s)):
        with pytest.raises(SystemExit, match="set `views` in the settings"):
            run()
    rec = json.loads((tmp_path / "releases" / "daily.jsonl").read_text().splitlines()[-1])
    assert "views" in rec["error"] and "crawl" not in rec  # stops before crawling


def test_maps_the_gpu_had_no_room_for_stop_the_version_until_a_run_computes_them(harvest, monkeypatch):
    tmp_path, s, _ = harvest
    import octantvector.generation
    releases.first(s, built_at=AT, log=StringIO())
    compute = octantvector.generation.compute
    short = {"computed": 3, "failed": 0, "skipped": 0, "deferred": 2,
             "views": {"meta_tags@2": {"ok": 3}, "chart_patterns@3": {"deferred": 2}}}
    monkeypatch.setattr(octantvector.generation, "compute", lambda *a, **k: short)
    with pytest.raises(SystemExit, match=r"no room for 2 maps \(chart_patterns@3 2\).*octantharvest daily --no-crawl"):
        releases.daily(s, fetch=False, built_at=AT + timedelta(days=1), log=StringIO())
    rec = json.loads((tmp_path / "releases" / "daily.jsonl").read_text().splitlines()[-1])
    assert rec["compute"] == short and "no version written" in rec["error"] and "release" not in rec
    assert len(releases.state(s)["versions"]) == 1
    monkeypatch.setattr(octantvector.generation, "compute", compute)
    rec = releases.daily(s, fetch=False, built_at=AT + timedelta(days=1), log=StringIO())
    assert rec["release"]["published_as"] == "diff" and len(releases.state(s)["versions"]) == 2


def test_a_octantvector_out_of_range_stops_the_run_and_is_logged(harvest, monkeypatch):
    tmp_path, s, _ = harvest
    import octantvector.generation
    monkeypatch.setattr(octantvector.generation, "API_VERSION", "2.0")
    with pytest.raises(SystemExit, match="engine_incompatible"):
        releases.daily(s, log=StringIO())  # stops before crawling
    rec = json.loads((tmp_path / "releases" / "daily.jsonl").read_text().splitlines()[-1])
    assert "generation API 2.0" in rec["error"] and "crawl" not in rec


@pytest.mark.parametrize("version", ["1.1", "1.3"])
def test_a_octantvector_with_a_usable_api_version_runs(version, monkeypatch):
    import octantvector.generation
    monkeypatch.setattr(octantvector.generation, "API_VERSION", version)
    assert releases._bv() is octantvector.generation


@pytest.mark.parametrize("version", ["0.9", "1.0", "2.0", None])
def test_a_octantvector_without_a_usable_api_version_stops(version, monkeypatch):
    import octantvector.generation
    monkeypatch.setattr(octantvector.generation, "API_VERSION", version, raising=False)
    with pytest.raises(SystemExit, match="engine_incompatible"):
        releases._bv()


def test_cli_status(harvest, capsys):
    cli.main(["status"])
    out = json.loads(capsys.readouterr().out)
    assert out["captures"][0].endswith("2026-10-01") and out["versions"] == {}
    assert out["views"] == {v: "ready" for v in VIEWS}


def test_status_says_which_views_cannot_be_computed(harvest, monkeypatch):
    tmp, s, _ = harvest
    s["views"] = ["chart_patterns", "no_such_view"]
    got = releases.check(s)
    assert got["chart_patterns"] == "ready" and "no_such_view: no such encoder" in got["no_such_view"]

    import octantvector.generation
    fake = FakeModels()
    monkeypatch.setattr(octantvector.generation, "models", fake.models)
    s["views"] = ["clap"]
    assert releases.check(s)["clap"].startswith("models missing (`octantharvest models`): org/a@")
    assert fake.fetched == []  # nothing is fetched
    releases.models(s, log=StringIO(), download=fake.download)
    assert releases.check(s) == {"clap": "ready"}
    (s["paths"]["models"] / "hf" / "org" / "b" / ("2" * 40) / "w.bin").unlink()
    assert releases.check(s) == {"clap": f"models missing (`octantharvest models`): org/b@{'2' * 40}/w.bin"}


def test_status_without_a_usable_octantvector(harvest, monkeypatch):
    tmp, s, _ = harvest
    import octantvector.generation
    monkeypatch.setattr(octantvector.generation, "API_VERSION", "2.0")
    assert "engine_incompatible" in releases.check(s)["octantvector"]


def test_the_hands_file_in_the_settings_goes_into_the_release(harvest):
    tmp_path, s, ms = harvest
    hands = [{"id": "tags", "label": "タグ", "inputs": [{"kind": "seed"}], "handling": "near",
              "knobs": {"weights": {"meta_tags": 1}}}]
    (tmp_path / "hands.json").write_text(json.dumps(hands))
    s["presets"] = tmp_path / "hands.json"
    releases.daily(s, fetch=False, log=StringIO())
    first = releases.first(s, built_at=AT, log=StringIO())
    assert json.loads((first["full"] / "manifest.json").read_text())["presets"] == hands
    (tmp_path / "hands.json").write_text(json.dumps([dict(hands[0], knobs={"weights": {"meta": 1}})]))
    with pytest.raises(SystemExit, match="'meta'"):  # a role is not a view: refused
        releases.daily(s, fetch=False, built_at=AT + timedelta(days=1), log=StringIO())


def test_the_notice_file_in_the_settings_goes_into_the_release_and_is_kept(harvest):
    tmp_path, s, ms = harvest
    notice = Path(__file__).parents[1] / "DB-NOTICE.md"
    s["notice"] = notice
    releases.daily(s, fetch=False, log=StringIO())
    first = releases.first(s, built_at=AT, log=StringIO())
    text = notice.read_text(encoding="utf-8")
    assert json.loads((first["full"] / "manifest.json").read_text())["notice"] == text
    s["notice"] = None  # not given: the previous version's
    rec = releases.daily(s, fetch=False, built_at=AT + timedelta(days=1), log=StringIO())
    new = Path(s["paths"]["releases"]) / rec["release"]["version"]
    assert json.loads((new / "manifest.json").read_text())["notice"] == text


OFFICIAL = Path(__file__).parents[1] / "presets" / "official.json"
# the views of the official DB that come from plugins, by role (the official DB of 2026.10.11; vocal_wavlm_sv was left
# out on 2026-10-23, its model being CC BY-SA 3.0)
PLUGIN_VIEWS = {"clap_htsat_unfused": "music", "mert_v1_95m": "music", "clap_concepts": "genre",
                "vocal_mert_v1_95m": "vocal", "vocal_clap_htsat_unfused": "vocal",
                "whisper_language": "language", "siglip_cover": "cover"}
MAPPERS = "beatsaver_mappers"  # the relation view OctantVector builds from the catalogue


def _stand_ins():
    """Catalogue-only stand-ins for the plugin views, registered through octantvector.plugin as a plugin would: the
    official hands are then checked by `release` itself, with no torch and no models. The genre view's words are the
    official hands' (the words themselves are checked against the plugin's list below)."""
    import octantvector.generation as bv
    from octantvector import plugin
    try:
        bv.models(list(PLUGIN_VIEWS))
    except bv.GenerationError:
        pass
    else:
        pytest.skip("the plugins are installed: their views would need the models")
    words = sorted({w for h in json.loads(OFFICIAL.read_text(encoding="utf-8")) for w in h["knobs"].get("genreFrom", [])})
    for view, role in PLUGIN_VIEWS.items():
        if view in _stand_ins.made:
            continue

        class StandIn(plugin.Encoder):
            id, inputs, dim = view, ("meta:metadata",), len(words) if role == "genre" else 4
            names = words

            def embed_map(self, m, v):
                return np.frombuffer(hashlib.sha256((self.id + v["hash"]).encode()).digest(), np.uint8)[:self.dim] \
                    .astype(np.float32)
        StandIn.role = role
        plugin.register(StandIn)
        _stand_ins.made.add(view)


_stand_ins.made = set()


def test_the_official_hands_fit_the_official_views(harvest):
    tmp, s, ms = harvest
    _stand_ins()
    root = Path(s["paths"]["artists"])
    root.mkdir()
    (root / "mb_names.jsonl").write_text(json.dumps({"name": "a", "mbid": "art-a"}) + "\n")
    (root / "lb_similar.jsonl").write_text(json.dumps(
        {"mbid": "art-a", "algorithm": artists.ALGORITHM, "at": "2026-10-12T00:00:00Z", "similar": []}) + "\n")
    s["views"], s["presets"] = s["views"] + list(PLUGIN_VIEWS) + [MAPPERS], OFFICIAL
    first = releases.first(s, built_at=AT, log=StringIO())
    m = json.loads((first["full"] / "manifest.json").read_text())
    assert [h["id"] for h in m["presets"]] == [h["id"] for h in json.loads(OFFICIAL.read_text(encoding="utf-8"))]


    def refused(views, artist_view=True):
        """The hands a first release of `views` refuses (a fresh releases folder each; the store is shared)."""
        s["views"], s["paths"]["releases"] = views, tmp / f"releases-{len(views)}-{artist_view}"
        if not artist_view:
            shutil.rmtree(root, ignore_errors=True)
        with pytest.raises(SystemExit) as ex:
            releases.first(s, built_at=AT, log=StringIO())
        return {part.split(":")[0].strip() for part in str(ex.value).split("cannot serve:")[1].split(";")}

    views = s["views"]
    # a DB without the vocal views refuses the hand that names them, and おまかせ, which draws from it; without the
    # word view, 好みのジャンル
    assert refused([v for v in views if PLUGIN_VIEWS.get(v) != "vocal"]) == {"vocal", "mixed"}
    assert refused([v for v in views if PLUGIN_VIEWS.get(v) not in ("vocal", "genre")]) == {"vocal", "mixed", "your_genre"}
    # the language, the cover and the mappers are named by 似た曲（言語・カバー・作者） only (and おまかせ draws from it)
    for v in ("whisper_language", "siglip_cover", MAPPERS):
        assert refused([x for x in views if x != v]) == {"song_plus", "mixed"}
    # すべて, 似た曲, 似た曲（言語・カバー・作者）, 似たアーティスト, 連想でたどる and おまかせ name the artist view
    assert refused(views, artist_view=False) == {"all", "song", "song_plus", "artist", "chain", "mixed"}


def test_the_genre_words_are_the_clap_plugins():
    clap = pytest.importorskip("octantvector_clap")
    hands = json.loads(OFFICIAL.read_text(encoding="utf-8"))
    words = next(h for h in hands if h["id"] == "your_genre")["knobs"]["genreFrom"]
    assert len(words) == 9 and set(words) <= set(clap.CONCEPTS)  # only the words that hit in OctantSpecs spike R1
    assert set(next(h for h in hands if h["id"] == "your_genre")["genreLabels"]) == set(words)  # each has a name


def test_every_text_of_the_official_hands_is_in_english_and_japanese():
    hands = json.loads((Path(__file__).parents[1] / "presets" / "official.json").read_text(encoding="utf-8"))
    texts = [h[k] for h in hands for k in ("group", "label", "hint") if k in h]
    for item in (i for h in hands for i in h["inputs"]):
        texts += [item["label"]] if "label" in item else []
        texts += [o["label"] for o in item.get("options", [])]
    texts += [t for h in hands for t in h.get("genreLabels", {}).values()]
    assert texts and all(set(t) == {"en", "ja"} and all(t.values()) for t in texts)


def test_the_artist_caches_become_the_artist_view_of_the_release(harvest):
    tmp, s, ms = harvest
    root = Path(s["paths"]["artists"])
    root.mkdir()
    (root / "mb_names.jsonl").write_text(json.dumps({"name": "a", "mbid": "art-a"}) + "\n")  # every map's artist
    (root / "lb_similar.jsonl").write_text(json.dumps(
        {"mbid": "art-a", "algorithm": artists.ALGORITHM, "at": "2026-10-12T00:00:00Z", "similar": []}) + "\n")
    first = releases.first(s, built_at=AT, log=StringIO())
    views = json.loads((first["full"] / "manifest.json").read_text())["views"]
    art = views["listenbrainz_artists@20261012"]
    assert art["kind"] == "relation" and art["role"] == "artist"
    assert (root / "map_artists.jsonl.gz").exists() and (root / "similar_artists.jsonl.gz").exists()


def test_the_daily_run_follows_a_release_with_the_artist_view(harvest):
    tmp, s, ms = harvest
    root = Path(s["paths"]["artists"])
    root.mkdir()
    (root / "mb_names.jsonl").write_text(json.dumps({"name": "a", "mbid": "art-a"}) + "\n")
    (root / "lb_similar.jsonl").write_text(json.dumps(
        {"mbid": "art-a", "algorithm": artists.ALGORITHM, "at": "2026-10-12T00:00:00Z", "similar": []}) + "\n")
    releases.first(s, built_at=AT, log=StringIO())
    rec = releases.daily(s, fetch=False, built_at=AT + timedelta(days=1), log=StringIO())
    assert rec["release"]["published_as"] == "diff"


class FakeModels:
    """bv.models with two models (one shared by two views, as a derived view's source would be) and a downloader
    that writes known bytes, counting what it fetched."""
    LIST = [{"repo": "org/a", "revision": "1" * 40, "files": ["config.json", "w.bin"], "path": "hf/org/a/" + "1" * 40},
            {"repo": "org/b", "revision": "2" * 40, "files": ["w.bin"], "path": "hf/org/b/" + "2" * 40}]

    def __init__(self):
        self.fetched, self.asked = [], []

    def models(self, views):
        self.asked.append(list(views))
        return self.LIST

    def download(self, repo, revision, name, folder):
        self.fetched.append((repo, name))
        Path(folder).mkdir(parents=True, exist_ok=True)
        (Path(folder) / name).write_bytes(f"{repo}/{name}".encode())


def test_models_fetches_records_and_refuses_a_changed_file(harvest, monkeypatch):
    tmp, s, _ = harvest
    import octantvector.generation
    fake = FakeModels()
    monkeypatch.setattr(octantvector.generation, "models", fake.models)
    assert releases.models(s, log=StringIO(), download=fake.download) == {f"org/a@{'1' * 40}": "fetched",
                                                                         f"org/b@{'2' * 40}": "fetched"}
    assert fake.asked == [s["views"]] and len(fake.fetched) == 3
    root = s["paths"]["models"]
    assert (root / "hf" / "org" / "a" / ("1" * 40) / "w.bin").read_bytes() == b"org/a/w.bin"
    rec = json.loads((root / "models.json").read_text())
    assert rec[f"org/a@{'1' * 40}"]["files"]["w.bin"] == "sha256:" + hashlib.sha256(b"org/a/w.bin").hexdigest()
    assert rec[f"org/b@{'2' * 40}"]["fetchedAt"]

    assert set(releases.models(s, log=StringIO(), download=fake.download).values()) == {"present"}
    assert len(fake.fetched) == 3  # recorded and intact: not fetched again
    (root / "hf" / "org" / "b" / ("2" * 40) / "w.bin").unlink()
    releases.models(s, log=StringIO(), download=fake.download)
    assert fake.fetched[3:] == [("org/b", "w.bin")]  # a missing file is fetched again
    (root / "hf" / "org" / "a" / ("1" * 40) / "w.bin").write_bytes(b"changed")
    with pytest.raises(SystemExit, match="never replaced silently"):
        releases.models(s, log=StringIO(), download=fake.download)
    assert (root / "hf" / "org" / "a" / ("1" * 40) / "w.bin").read_bytes() == b"changed"
    rec_path = root / "models.json"
    rec = json.loads(rec_path.read_text())
    del rec[f"org/b@{'2' * 40}"]["files"]["w.bin"]  # there but unrecorded (record lost, or copied in by hand)
    rec_path.write_text(json.dumps(rec))
    (root / "hf" / "org" / "a" / ("1" * 40) / "w.bin").write_bytes(b"org/a/w.bin")
    n = len(fake.fetched)
    with pytest.raises(SystemExit, match="not recorded"):
        releases.models(s, log=StringIO(), download=fake.download)
    assert len(fake.fetched) == n


def test_daily_and_first_release_fetch_the_models_before_computing(harvest, monkeypatch):
    tmp, s, _ = harvest
    import octantvector.generation
    calls = []
    monkeypatch.setattr(releases, "models", lambda s, log: calls.append("models") or {})
    compute = octantvector.generation.compute

    def computing(*a, **k):
        calls.append(("compute", k["models_dir"]))
        return compute(*a, **k)
    monkeypatch.setattr(octantvector.generation, "compute", computing)
    releases.daily(s, fetch=False, log=StringIO())
    releases.first(s, built_at=AT, log=StringIO())
    assert calls == ["models", ("compute", s["paths"]["models"])] * 2


def test_the_models_command(harvest, monkeypatch, capsys):
    tmp, s, _ = harvest
    monkeypatch.setattr(releases, "models", lambda s: {"org/a@" + "1" * 40: "present"})
    cli.main(["models"])
    assert json.loads(capsys.readouterr().out) == {"org/a@" + "1" * 40: "present"}
