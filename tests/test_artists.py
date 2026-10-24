"""crawl/artists.py: name splitting, the MusicBrainz match, the ListenBrainz answer, and a resumable budgeted run."""
import gzip
import io
import json
import urllib.error
import urllib.parse

import pytest

from octantharvest import cli, settings
from octantharvest.crawl import artists

SETTINGS = {"paths": {"artists": "artists"}, "crawl": {"artists": {"userAgent": "OctantHarvest-test ( test@example.org )"}}}


def test_split_artists():
    f = artists.split_artists
    assert f("covered by Bao The Whale") == ("Bao The Whale", [])
    assert f("Doja Cat/Rainych") == ("Doja Cat/Rainych", ["Doja Cat", "Rainych"])
    assert f("Akame (CV: Sora Amamiya)") == ("Akame", ["Sora Amamiya"])
    assert f("Round Table ft. Nino") == ("Round Table ft. Nino", ["Round Table", "Nino"])
    assert f("Sabrina Carpenter.mp3") == ("Sabrina Carpenter", [])
    assert f("Sabrina Carpenter - Sabrina Carpenter") == ("Sabrina Carpenter", [])
    assert f("米津玄師") == ("米津玄師", [])
    assert f("") == (None, [])
    assert f("Tyler, The Creator") == ("Tyler, The Creator", [])
    assert f("Ado, Reol & yama") == ("Ado, Reol & yama", ["Ado", "Reol", "yama"])


def test_match_needs_an_exact_name_with_the_top_score():
    found = {"artists": [{"id": "a", "name": "Rainych", "score": 100},
                         {"id": "b", "name": "Rain", "score": 100}]}
    assert artists.match_artist("rainych", found)["id"] == "a"
    assert artists.match_artist("RAINYCH", found)["id"] == "a"
    assert artists.match_artist("rain", found)["id"] == "b"
    alias = {"artists": [{"id": "c", "name": "Kenshi Yonezu", "score": 100, "aliases": [{"name": "米津玄師"}]}]}
    assert artists.match_artist("米津玄師", alias)["id"] == "c"
    assert artists.match_artist("Rainy", found) is None
    assert artists.match_artist("rainych", {"artists": [{"id": "a", "name": "Rainych", "score": 92}]})["id"] == "a"
    assert artists.match_artist("rainych", {"artists": [{"id": "a", "name": "Rainych", "score": 80}]}) is None
    assert artists.match_artist("x", None) is None
    miku = {"artists": [{"id": "z", "name": "Hatsune", "score": 100},
                        {"id": "m", "name": "初音ミク", "sort-name": "Miku, Hatsune", "score": 95}]}
    assert artists.match_artist("hatsune miku", miku)["id"] == "m"
    assert artists.match_artist("i-dle", {"artists": [{"id": "i", "name": "(G)I-DLE", "score": 95,
                                                        "aliases": [{"name": "I-DLE"}]}]})["id"] == "i"
    special = {"artists": [{"id": "125ec42a-7229-4250-afc5-e057484327fe", "name": "[unknown]", "score": 100}]}
    assert artists.match_artist("[unknown]", special) is None
    assert artists.match_artist("unknown", {"artists": [{"id": "u", "name": "Unknown", "score": 100}]}) is None


def test_map_ids_prefer_the_whole_name():
    names = {"earth, wind & fire": {"mbid": "ewf"}, "fire": {"mbid": "wrong"},
             "doja cat": {"mbid": "doja"}, "rainych": {"mbid": "rain"}, "doja cat/rainych": {"mbid": None}}
    assert artists.map_artist_ids("Earth, Wind & Fire", names) == ["ewf"]
    assert artists.map_artist_ids("Doja Cat/Rainych", names) == ["doja", "rain"]
    assert artists.map_artist_ids("Nobody", names) == []
    assert artists.map_artist_ids("Doja Cat & Somebody", names) == []  # a part without an ID drops them all
    names["unknown"] = {"mbid": "125ec42a-7229-4250-afc5-e057484327fe"}  # cached by the first matcher
    assert artists.map_artist_ids("unknown", names) == []


def test_parse_similar():
    row = {"artist_mbid": "m", "name": "YOASOBI", "score": 42}
    assert artists.parse_similar([row]) == [{"mbid": "m", "name": "YOASOBI", "score": 42}]
    assert artists.parse_similar(None) == []


class FakeHttp:
    def __init__(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        if "musicbrainz" in url:
            q = url.split("query=")[1]
            if "rainych" in q:
                return {"artists": [{"id": "rain", "name": "Rainych", "score": 100}]}
            return {"artists": []}
        return [{"artist_mbid": "yoa", "name": "YOASOBI", "score": 10}]


def test_fetch_is_budgeted_and_resumes(tmp_path):
    cat = tmp_path / "catalogue.jsonl.gz"
    rows = [("Rainych", 3), ("Nobody Known", 2), ("covered by Rainych", 1)]
    with gzip.open(cat, "wt") as f:
        n = 0
        for name, k in rows:
            for _ in range(k):
                n += 1
                f.write(json.dumps({"id": str(n), "metadata": {"songAuthorName": name},
                                    "versions": [{"hash": f"{n:040x}"}]}) + "\n")
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SETTINGS))
    s = settings.load(p)
    http = FakeHttp()
    (tmp_path / "artists").mkdir()
    (tmp_path / "artists" / "mb_names.jsonl").write_text(
        json.dumps({"name": "nobody known", "mbid": None, "v": 1}) + "\n")  # unmatched by the first matcher: asked again
    r = artists.fetch(s, cat, budget=1, http=http)  # one request to each service
    assert r["run"] == {"names": 1, "matched": 1, "similar": 1}  # "rainych" has the most maps, then its neighbours
    r = artists.fetch(s, cat, budget=0, http=http)
    assert r["run"] == {"names": 1} and len(http.urls) == 3
    assert r["cache"] == {"names": 2, "matched": 1, "artistsWithSimilar": 1}
    assert artists.fetch(s, cat, budget=0, http=http)["requests"] == 0  # everything cached
    sim = [json.loads(x) for x in (tmp_path / "artists" / "lb_similar.jsonl").read_text().splitlines()]
    assert sim[0]["mbid"] == "rain" and sim[0]["similar"][0]["name"] == "YOASOBI"
    assert any("algorithm=session_based" in u for u in http.urls)


def test_fetch_refuses_to_start_without_a_contact_in_the_user_agent(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"paths": {"artists": "artists"}}))
    http = FakeHttp()
    with pytest.raises(SystemExit, match="crawl.artists.userAgent"):
        artists.fetch(settings.load(p), tmp_path / "catalogue.jsonl.gz", http=http)
    assert http.urls == [] and not (tmp_path / "artists").exists()


def test_fetch_refuses_to_start_with_less_than_a_second_between_requests(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({**SETTINGS, "crawl": {"artists": {**SETTINGS["crawl"]["artists"], "minDelay": 0.5}}}))
    http = FakeHttp()
    with pytest.raises(SystemExit, match="crawl.artists.minDelay"):
        artists.fetch(settings.load(p), tmp_path / "catalogue.jsonl.gz", http=http)
    assert http.urls == [] and not (tmp_path / "artists").exists()


def test_cli_has_the_artists_command(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(artists, "fetch", lambda s, c, budget=None: seen.update(budget=budget) or {})
    monkeypatch.setattr(cli.crawl, "latest_catalogue", lambda s: tmp_path / "c.jsonl.gz")
    monkeypatch.setenv("OCTANTHARVEST_SETTINGS", str(tmp_path / "none.json"))
    cli.main(["artists", "--budget", "5"])
    assert seen == {"budget": 5}


def test_search_covers_aliases_and_sort_names(tmp_path):
    cat = tmp_path / "catalogue.jsonl.gz"
    with gzip.open(cat, "wt") as f:
        f.write(json.dumps({"id": "1", "metadata": {"songAuthorName": "yorushika"},
                            "versions": [{"hash": "a" * 40}]}) + "\n")
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SETTINGS))
    http = FakeHttp()
    artists.fetch(settings.load(p), cat, budget=1, http=http)
    q = urllib.parse.unquote_plus(http.urls[0].split("query=")[1].split("&")[0])
    assert q == 'artist:"yorushika" OR alias:"yorushika" OR sortname:"yorushika"'


def test_retry_after_zero_backs_off(monkeypatch):
    calls, pauses = [], []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {"Retry-After": "0"}, None)
        return io.BytesIO(b'{"ok": 1}')

    monkeypatch.setattr(artists.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(artists.time, "sleep", pauses.append)
    h = artists.Http("ua", min_delay=0, log=io.StringIO())
    assert h.get("https://musicbrainz.org/x") == {"ok": 1}
    assert [round(p) for p in pauses if p >= 1] == [2, 4]


def test_an_error_in_either_service_stops_the_run(tmp_path):
    cat = tmp_path / "catalogue.jsonl.gz"
    with gzip.open(cat, "wt") as f:
        f.write(json.dumps({"id": "1", "metadata": {"songAuthorName": "Rainych"},
                            "versions": [{"hash": "a" * 40}]}) + "\n")
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SETTINGS))

    class Broken(FakeHttp):
        def get(self, url):
            if "listenbrainz" in url:
                raise RuntimeError("down")
            return super().get(url)

    try:
        artists.fetch(settings.load(p), cat, budget=0, http=Broken())
    except RuntimeError as e:
        assert str(e) == "down"
    else:
        raise AssertionError("the ListenBrainz error was swallowed")
    assert (tmp_path / "artists" / "mb_names.jsonl").exists()  # the lookup made before the error is kept


def test_overlapping_requests_keep_their_starts_apart(monkeypatch):
    import threading
    import time
    starts, lock = [], threading.Lock()

    def urlopen(req, timeout):
        with lock:
            starts.append(time.monotonic())
        time.sleep(0.15)  # a slow answer: the next request starts while this one waits
        return io.BytesIO(b'{"ok": 1}')

    monkeypatch.setattr(artists.urllib.request, "urlopen", urlopen)
    h = artists.Http("ua", min_delay=0.05, log=io.StringIO())
    t0 = time.monotonic()
    out = list(artists._windowed(range(6), lambda i: (i, h.get("https://musicbrainz.org/x")), 4))
    assert [i for i, _ in out] == list(range(6)) and all(r == (i, {"ok": 1}) for i, r in out)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert min(gaps) >= 0.045  # never faster than min_delay
    assert time.monotonic() - t0 < 6 * 0.15  # but faster than one at a time


def test_a_dropped_connection_is_retried(monkeypatch):
    import http.client
    calls = []

    def urlopen(req, timeout):
        calls.append(1)
        if len(calls) == 1:
            raise http.client.RemoteDisconnected("Remote end closed connection without response")
        return io.BytesIO(b'{"ok": 1}')

    monkeypatch.setattr(artists.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(artists.time, "sleep", lambda s: None)
    assert artists.Http("ua", min_delay=0, log=io.StringIO()).get("https://labs.api.listenbrainz.org/x") == {"ok": 1}


def test_export_writes_octantvectors_two_files_without_names(tmp_path):
    cat = tmp_path / "catalogue.jsonl.gz"
    with gzip.open(cat, "wt") as f:
        for n, name in enumerate(["Rainych", "Doja Cat/Rainych", "Nobody Known", "Various Artists"], 1):
            f.write(json.dumps({"id": str(n), "metadata": {"songAuthorName": name},
                                "versions": [{"hash": f"{n:040x}"}]}) + "\n")
    p = tmp_path / "s.json"
    p.write_text(json.dumps(SETTINGS))
    s = settings.load(p)
    root = tmp_path / "artists"
    assert artists.export(s, cat) is None  # nothing looked up yet
    root.mkdir()
    alg = artists.ALGORITHM
    (root / "mb_names.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"name": "rainych", "mbid": "rain"}, {"name": "doja cat", "mbid": "doja"},
        {"name": "nobody known", "mbid": None}]))
    special = next(iter(artists.SPECIAL_MBIDS))
    (root / "lb_similar.jsonl").write_text("".join(json.dumps(r) + "\n" for r in [
        {"mbid": "rain", "algorithm": alg, "at": "2026-10-12T01:00:00Z",
         "similar": [{"mbid": "yoa", "name": "YOASOBI", "score": 10}, {"mbid": special, "name": "[unknown]", "score": 9}]},
        {"mbid": "doja", "algorithm": alg, "at": "2026-10-12T02:00:00Z", "similar": []},
        {"mbid": "rain", "algorithm": "other", "at": "2026-10-13T00:00:00Z", "similar": []}]))
    out = artists.export(s, cat)
    assert out == {"maps": 2, "artists": 2, "folder": str(root)}
    with gzip.open(root / artists.MAP_FILE, "rt") as f:
        maps = [json.loads(x) for x in f]
    assert maps == [{"hash": f"{1:040x}", "mbids": ["rain"]}, {"hash": f"{2:040x}", "mbids": ["doja", "rain"]}]
    with gzip.open(root / artists.SIMILAR_FILE, "rt") as f:
        sim = [json.loads(x) for x in f]
    assert sim[0] == {"algorithm": alg, "fetchedAt": "2026-10-12T02:00:00Z"}
    assert sim[1:] == [{"mbid": "doja", "similar": []}, {"mbid": "rain", "similar": [["yoa", 10]]}]
