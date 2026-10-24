"""The daily catalogue capture (crawl.catalogue and crawl/beatsaver_catalogue_capture.py) against a fake BeatSaver API
in memory: the first capture walks the whole catalogue, the next ones start from the newest complete capture and fetch
only what changed since, and keepCaptures never deletes the capture the next run starts from."""
import gzip
import io
import json
import re
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from io import StringIO

import pytest

from octantharvest import cli, crawl, settings
from octantharvest.crawl import beatsaver_catalogue_capture as cap

OLD = "2025-01-01T00:00:00.000000Z"


def stamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def at(value):
    return datetime.fromisoformat(value) if value else None


class Response(io.BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(body)
        self.headers = {"Content-Type": "application/json"}


class FakeBeatSaver:
    """/maps/latest (sort CREATED or UPDATED; `after` ascending, `before` alone descending), /maps/deleted, /vote and
    the swagger document, as the capture uses them. A vote changes a map's stats and not its updatedAt, as on
    BeatSaver."""

    def __init__(self, n=12):
        self.maps, self.deleted, self.voted, self.requests, self.fail = {}, [], {}, [], None
        for i in range(1, n + 1):
            self.add(i, OLD)

    def add(self, i, when):
        mid = f"{i:x}"
        self.maps[mid] = {"id": mid, "name": f"map {i}", "metadata": {"songName": "s", "songAuthorName": f"a{i}",
                                                                     "levelAuthorName": "m", "bpm": 120, "duration": 90},
                          "stats": {"plays": 0, "downloads": 0, "upvotes": i, "downvotes": 0, "score": 0.5},
                          "tags": ["pop"], "createdAt": when, "updatedAt": when, "uploaded": when,
                          "versions": [self.version(i, 0, when)]}
        return mid

    def version(self, i, n, when):
        h = f"{i:08x}{n:032x}"
        return {"hash": h, "key": f"{i:x}", "state": "Published", "createdAt": when,
                "downloadURL": f"https://r2cdn.beatsaver.com/{h}.zip",
                "diffs": [{"characteristic": "Standard", "difficulty": "Expert", "nps": 4.0, "njs": 16}]}

    def new_version(self, mid, when=None):
        now = when or stamp()
        self.maps[mid].update(versions=[self.version(int(mid, 16), 1, now)], updatedAt=now)

    def delete(self, mid, when=None):
        del self.maps[mid]
        self.deleted.append({"id": mid, "deletedAt": when or stamp()})

    def vote(self, mid, upvotes):
        m = self.maps[mid]
        m["stats"]["upvotes"] = upvotes
        self.voted[mid] = (stamp(), {"hash": m["versions"][0]["hash"], "mapId": int(mid, 16), "key64": mid,
                                     **{k: m["stats"][k] for k in ("upvotes", "downvotes", "score")}})

    def urlopen(self, req, timeout):
        url = urllib.parse.urlsplit(req.full_url)
        q = dict(urllib.parse.parse_qsl(url.query))
        self.requests.append((url.path, q))
        assert req.get_method() == "GET" and req.headers["User-agent"]
        if self.fail == url.path:
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, io.BytesIO(b"no"))
        if url.path == "/docs/swagger.json":
            body = {"swagger": "2.0", "info": {"title": "BeatSaver API", "version": "0.1"}}
        elif url.path == "/maps/latest":
            body = {"docs": self.page(self.maps.values(), {"CREATED": "createdAt", "UPDATED": "updatedAt"}[q["sort"]], q)}
        elif url.path == "/maps/deleted":
            body = {"docs": self.page(self.deleted, "deletedAt", q)}
        elif url.path == "/vote":
            body = [summary for when, summary in self.voted.values() if at(when) > at(q["since"])]
        else:
            raise AssertionError(url.path)
        return Response(json.dumps(body).encode())

    def page(self, docs, field, q):
        after, before = at(q.get("after")), at(q.get("before"))
        hits = [d for d in docs if (after is None or at(d[field]) > after) and (before is None or at(d[field]) < before)]
        hits.sort(key=lambda d: d[field], reverse=after is None)
        return [json.loads(json.dumps(d)) for d in hits[:int(q["pageSize"])]]

    def walked(self):
        """Whether the last capture walked the whole catalogue (it asked for maps by CREATED)."""
        return any(q.get("sort") == "CREATED" for _, q in self.requests)


def in_process(cmd, log):
    """crawl's runner, with the capture script run in this process (so it reaches the fake)."""
    return cap.main([str(c) for c in cmd[2:]])


@pytest.fixture
def world(tmp_path, monkeypatch):
    api = FakeBeatSaver()
    monkeypatch.setattr(cap.urllib.request, "urlopen", api.urlopen)
    p = tmp_path / "octantharvest.json"
    p.write_text(json.dumps({"crawl": {"catalogue": {"minDelay": 0}}}))
    s = settings.load(p)
    now = datetime.now(timezone.utc)

    def capture(day, **kw):
        api.requests.clear()
        return crawl.catalogue(s, today=now + timedelta(days=day), **{"log": StringIO(), "run": in_process, **kw})
    capture.folder = lambda day: s["paths"]["catalogues"] / (now + timedelta(days=day)).strftime("%Y-%m-%d")
    return api, s, capture


def lines(cat):
    with gzip.open(cat, "rt") as f:
        return {json.loads(line)["id"]: line for line in f}


def manifest(cat):
    return json.loads((cat.parent / "manifest.json").read_text())


def test_the_first_capture_walks_the_whole_catalogue(world):
    api, _, capture = world
    cat = capture(0)
    assert api.walked() and ("/vote", {"since": manifest(cat)["votesSince"]}) in api.requests
    m = manifest(cat)
    assert m["complete"] and m["base"] is None and cap.full_walk_at(m) == m["startedAt"]
    assert sorted(lines(cat)) == sorted(api.maps)


def test_the_next_capture_fetches_only_what_changed(world):
    api, _, capture = world
    first = capture(0)
    before = lines(first)
    api.new_version("2")
    new = api.add(0x20, stamp())
    api.vote("4", 999)  # stats only: updatedAt does not move
    api.vote("3", 50)
    api.delete("3")
    cat = capture(1)

    assert not api.walked()
    # a page of what changed and the empty page that ends it, for the updated and the deleted maps
    assert sorted(p for p, _ in api.requests) == ["/docs/swagger.json", "/maps/deleted", "/maps/deleted",
                                                  "/maps/latest", "/maps/latest", "/vote"]
    got = lines(cat)
    assert sorted(got) == sorted(set(before) - {"3"} | {new})
    assert json.loads(got["2"])["versions"][0]["hash"] == api.maps["2"]["versions"][0]["hash"] != \
        json.loads(before["2"])["versions"][0]["hash"]
    assert json.loads(got["4"])["stats"]["upvotes"] == 999
    assert json.loads(got["4"]) == dict(json.loads(before["4"]), stats=dict(json.loads(before["4"])["stats"], upvotes=999))
    assert all(got[k] == before[k] for k in ("1", "5", "c"))  # unchanged maps are carried over as they were
    with gzip.open(cat.parent / "tombstones.jsonl.gz", "rt") as f:
        assert [json.loads(line)["id"] for line in f] == ["3"]

    m, b = manifest(cat), manifest(first)
    assert m["schema"] == b["schema"] and "base_created" not in m["phases"]
    assert m["base"]["capture"] == first.parent.name and cap.full_walk_at(m) == b["startedAt"]
    assert at(m["baseCutoff"]) == at(b["snapshotCutoff"]) - cap.OVERLAP
    assert at(m["votesSince"]) <= at(b["startedAt"]) - cap.OVERLAP
    assert m["build"]["votesObserved"] == 2 and m["build"]["votesApplied"] == 1  # 3 is no longer in the catalogue
    cap.verify_capture(cat.parent, require_manifest=True)

    api.new_version("5")
    third = capture(2)  # a diff of a diff
    assert not api.walked() and manifest(third)["base"]["capture"] == cat.parent.name
    assert json.loads(lines(third)["5"])["versions"][0]["hash"] == api.maps["5"]["versions"][0]["hash"]
    assert cap.full_walk_at(manifest(third)) == b["startedAt"]
    (first.parent / "catalogue.jsonl.gz").unlink()  # a diff is rebuilt from its base's catalogue
    with pytest.raises(RuntimeError, match="Cannot build: base capture"):
        cap.build_catalogue(cat.parent)


def test_maps_sharing_a_timestamp_across_a_page_boundary_are_not_skipped(world, monkeypatch):
    api, _, capture = world
    capture(0)
    monkeypatch.setattr(cap, "PAGE_SIZE", 4)
    t = [(datetime.now(timezone.utc) - timedelta(minutes=10 - i)).strftime("%Y-%m-%dT%H:%M:%S.%fZ") for i in range(4)]
    # three maps one at a time, then three at once (one timestamp, as one UPDATE writes): the first page of each
    # ends on that timestamp with only one of the three
    for i, (gone, changed) in enumerate(zip("123456", "789abc")):
        api.delete(gone, t[min(i, 3)])
        api.new_version(changed, t[min(i, 3)])
    cat = capture(1)
    assert sorted(lines(cat)) == sorted(api.maps) == list("789abc")
    assert all(json.loads(line)["versions"][0]["hash"].endswith("1") for line in lines(cat).values())
    m = manifest(cat)
    asked = {"pageSize": "4", "after": cap.just_before(t[3]), "before": m["snapshotCutoff"]}
    assert ("/maps/deleted", asked) in api.requests and m["verification"]["boundaryTieWarnings"] == []

    # a full page all at one timestamp cannot be got past: the capture goes on after it, with a warning
    for mid in "789abc":
        api.new_version(mid, t[3])
    assert manifest(capture(2))["verification"]["boundaryTieWarnings"]


def test_an_incomplete_or_failed_capture_is_not_a_base(world, capsys):
    api, _, capture = world
    first = capture(0)
    api.fail = "/vote"
    with pytest.raises(SystemExit, match="running again resumes it"):
        capture(1)
    interrupted = capture.folder(1)
    assert (interrupted / "capture-state.json").exists() and not (interrupted / "manifest.json").exists()
    # the script resumes it from `first` whatever --base it is given, and says so
    assert cap.main(["capture", "--output", str(interrupted), "--base", str(capture.folder(3))]) == 1
    assert f"{first.parent} からの差分" in capsys.readouterr().err and api.requests[-1][0] == "/vote"
    failed = capture.folder(2)
    failed.mkdir()
    (failed / "manifest.json").write_text(json.dumps({"schema": cap.SCHEMA, "complete": False}))
    api.fail = None
    cat = capture(3)
    assert not api.walked() and manifest(cat)["base"]["capture"] == first.parent.name

    log = StringIO()
    resumed = capture(1, log=log)  # the interrupted capture goes on as it began, from `first`
    assert [p for p, _ in api.requests] == ["/vote"] and manifest(resumed)["base"]["capture"] == first.parent.name
    assert log.getvalue() == f"catalogue {interrupted.name}: resuming it as it began\n"

    with pytest.raises(RuntimeError, match="not a complete capture"):
        cap.base_record(failed)
    (first.parent / "catalogue.jsonl.gz").write_bytes(gzip.compress(b"{}\n"))
    with pytest.raises(RuntimeError, match="not the one its manifest.json records"):
        cap.base_record(first.parent)


def test_a_damaged_base_stops_the_crawl_before_today_s_capture_begins(world):
    api, _, capture = world
    first = capture(0).parent
    (first / "catalogue.jsonl.gz").write_bytes(gzip.compress(b"{}\n"))
    with pytest.raises(SystemExit, match=re.escape(f"Remove {first} ") + ".*crawl --full"):
        capture(1)
    assert api.requests == [] and not capture.folder(1).exists()
    assert cap.main(["capture", "--output", str(capture.folder(1)), "--base", str(first)]) == 1
    assert not capture.folder(1).exists()  # the script refuses the base before it makes the folder
    assert manifest(capture(1, full=True))["base"] is None


def test_keep_captures_keeps_the_capture_the_next_run_starts_from(world):
    api, s, capture = world
    s["crawl"]["keepCaptures"] = 1
    first = capture(0)
    api.fail = "/vote"
    with pytest.raises(SystemExit):
        capture(1)  # left incomplete: never pruned
    api.fail = None
    second = capture(2)
    assert [d.name for d in crawl.captures(s["paths"]["catalogues"])] == [second.parent.name]
    assert not first.parent.exists() and capture.folder(1).exists()
    api.new_version("6")
    third = capture(3)
    assert not api.walked() and manifest(third)["base"]["capture"] == second.parent.name
    assert [d.name for d in crawl.captures(s["paths"]["catalogues"])] == [third.parent.name]


def test_a_full_walk_when_it_is_due_or_asked_for(world):
    api, s, capture = world
    capture(0)
    full = capture(1, full=True)  # repair
    assert api.walked() and manifest(full)["base"] is None
    with pytest.raises(SystemExit, match="remove"):
        capture(1, full=True)  # today's capture exists: never removed by the crawl
    s["crawl"]["fullWalkDays"] = 30
    capture(30)
    assert not api.walked()  # the last full walk began 29 days before
    capture(31)
    assert api.walked()
    s["crawl"]["fullWalkDays"] = 0  # only when asked for
    capture(80)
    assert not api.walked()


def test_crawl_full_on_the_command_line(harvest, monkeypatch):
    calls = []
    monkeypatch.setattr(crawl, "catalogue", lambda s, full=False: calls.append(full) or "cat")
    monkeypatch.setattr(crawl, "download", lambda s, cat: "complete")
    cli.main(["crawl", "--full"])
    cli.main(["crawl"])
    assert calls == [True, False]
