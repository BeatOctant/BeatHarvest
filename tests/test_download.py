"""The archive downloader's retry rules (crawl/beatsaver_bulk_download.py), without the network: a zip that is
broken on the server is tried once per pass, given up after giveUpAfterRuns runs and tried again after
recheckGivenUpDays."""
import gzip
import io
import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from octantharvest.crawl import beatsaver_bulk_download as dl

GOOD, BROKEN = "a" * 40, "b" * 40


def good_zip():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Info.dat", "{}")
    return buf.getvalue()


class Response(io.BytesIO):
    def __init__(self, url, body):
        super().__init__(body)
        self.url, self.headers = url, {"Content-Length": str(len(body))}

    def geturl(self):
        return self.url


@pytest.fixture
def world(tmp_path, monkeypatch):
    cat = tmp_path / "catalogue.jsonl.gz"
    with gzip.open(cat, "wt") as f:
        for i, h in enumerate((GOOD, BROKEN)):
            f.write(json.dumps({"id": f"k{i}", "versions": [
                {"hash": h, "key": f"k{i}", "state": "Published", "downloadURL": f"https://r2cdn.beatsaver.com/{h}.zip"}]})
                + "\n")
    requests = []

    def urlopen(req, timeout):
        requests.append(req.full_url)
        return Response(req.full_url, good_zip() if GOOD in req.full_url else b"PK\x03\x04 not a zip at all")
    monkeypatch.setattr(dl.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(dl, "transient_delay", lambda *a, **k: 0.0)
    monkeypatch.setattr(dl.random, "uniform", lambda a, b: 0.0)  # no cooldown between passes
    out = tmp_path / "archive"

    def run(*extra):
        return dl.main([str(cat), str(out), "--staging-dir", str(tmp_path / "stage"), "--min-free-gib", "0",
                        "--staging-gib", "0.01", "--max-file-mib", "1", "--max-retry-cycles", "1", "--give-up-after-runs", "2", *extra])
    return out, requests, run


def journal(out):
    return [json.loads(line) for line in (out / "download.journal.jsonl").read_text().splitlines()]


def test_a_broken_zip_is_tried_once_per_pass_and_given_up_after_two_runs(world):
    out, requests, run = world
    assert run() == 1  # incomplete: the broken zip is still deferred
    assert (out / "maps" / GOOD[:2] / f"{GOOD}.zip").exists()
    assert sum(BROKEN in r for r in requests) == 2  # 2 passes, one try each (not retriesPerCycle tries)
    last = [e for e in journal(out) if e.get("hash") == BROKEN][-1]
    assert last["event"] == "run_deferred" and last["status"] == "retry_later" and last["deferred_runs"] == 1

    requests.clear()
    assert run() == 1  # second run: given up, so blocked
    last = [e for e in journal(out) if e.get("hash") == BROKEN][-1]
    assert last["status"] == "blocked" and last["deferred_runs"] == 2 and "ZIP integrity" in last["error"]
    assert requests == [f"https://r2cdn.beatsaver.com/{BROKEN}.zip"] * 2

    requests.clear()
    assert run() == 1  # third run: skipped without a request; the run still ends as incomplete
    assert requests == []


def test_a_given_up_zip_is_tried_again_after_the_recheck_days(world):
    out, requests, run = world
    run(), run()
    path = out / "download.journal.jsonl"
    old = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat(timespec="seconds").replace("+00:00", "Z")
    events = journal(out)
    gave_up = max(n for n, e in enumerate(events) if e.get("event") == "run_deferred" and e["status"] == "blocked")
    events[gave_up]["ts"] = old  # given up 31 days ago
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    requests.clear()
    run("--recheck-given-up-days", "30")
    assert requests == [f"https://r2cdn.beatsaver.com/{BROKEN}.zip"] * 2
    requests.clear()
    run("--recheck-given-up-days", "30")  # given up again just now: not due yet
    assert requests == []
