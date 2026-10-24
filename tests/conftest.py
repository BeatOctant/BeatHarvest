"""A synthetic crawl result (contract 4 (OctantHarvest crawl to OctantVector)) and settings pointing at it: no network, no models, no GPU."""
import gzip
import hashlib
import json
import zipfile

import numpy as np
import pytest

from octantharvest import settings

NOTES = [{"_time": i * 0.5, "_lineIndex": i % 4, "_lineLayer": i % 3, "_type": i % 2,
          "_cutDirection": [1, 0][i % 2] if i % 3 else 8} for i in range(40)]
INFO = {"_beatsPerMinute": 120, "_songFilename": "song.egg", "_difficultyBeatmapSets": [
    {"_beatmapCharacteristicName": "Standard", "_difficultyBeatmaps": [
        {"_difficulty": "Expert", "_beatmapFilename": "Expert.dat", "_noteJumpMovementSpeed": 16}]}]}
VIEWS = ["chart_patterns", "meta_tags"]  # zip and catalogue views: no audio models needed


def H(i):
    return f"{i:02x}" + "0" * 38


def maps(n=24, seed=0):
    rng = np.random.default_rng(seed)
    out = []
    for i in range(1, n + 1):
        v = {"hash": H(i), "state": "Published", "createdAt": "2025-01-01T00:00:00Z",
             "diffs": [{"characteristic": "Standard", "difficulty": "Expert",
                        "nps": float(rng.uniform(1, 9)), "njs": float(rng.uniform(10, 22))}]}
        out.append({"id": f"k{i}", "metadata": {"songName": "s", "songAuthorName": "a", "levelAuthorName": "m",
                                                 "bpm": 120, "duration": 100},
                    "stats": {"upvotes": 200}, "tags": [["pop"], ["rock"], ["tech", "metal"]][i % 3],
                    "versions": [v]})
    return out


def write_capture(root, day, ms):
    d = root / day
    d.mkdir(parents=True, exist_ok=True)
    with gzip.open(d / "catalogue.jsonl.gz", "wt") as f:
        for m in ms:
            f.write(json.dumps(m) + "\n")
    (d / "manifest.json").write_text(json.dumps({"schema": "octantharvest-beatsaver-catalogue-capture/v1",
                                                "complete": True, "startedAt": f"{day}T00:00:00Z",
                                                "snapshotCutoff": f"{day}T00:00:00Z",
                                                "phases": {"base_created": {"status": "complete"}},
                                                "build": {"catalogueFileSha256": "sha256:" + hashlib.sha256(
                                                    (d / "catalogue.jsonl.gz").read_bytes()).hexdigest()}}))
    return d / "catalogue.jsonl.gz"


def write_archive(root, n):
    for i in range(1, n + 1):
        p = root / "maps" / H(i)[:2] / f"{H(i)}.zip"
        p.parent.mkdir(parents=True, exist_ok=True)
        notes = [dict(x, _lineIndex=(x["_lineIndex"] + i) % 4, _time=x["_time"] * (1 + i % 3)) for x in NOTES]
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("Info.dat", json.dumps(INFO))
            z.writestr("Expert.dat", json.dumps({"_version": "2.2.0", "_notes": notes, "_obstacles": []}))
    (root / "archive_meta.json").write_text(json.dumps({"format": "beatmap-music-identity-beatsaver-archive-v2"}))
    (root / "download.journal.jsonl").write_text("".join(
        json.dumps({"event": "committed", "hash": H(i), "commit_seq": i}) + "\n" for i in range(1, n + 1)))


@pytest.fixture
def harvest(tmp_path, monkeypatch):
    ms = maps()
    write_capture(tmp_path / "catalogue", "2026-10-01", ms)
    write_archive(tmp_path / "archive", len(ms))
    (tmp_path / "octantharvest.json").write_text(json.dumps({
        "views": VIEWS, "octantvector": {"workers": 1, "device": "cpu", "pools": {"distribution": {"min_upvotes": 0}}},
        "publish": {"partSizeMb": 0.01}}))
    monkeypatch.setenv("OCTANTHARVEST_SETTINGS", str(tmp_path / "octantharvest.json"))
    return tmp_path, settings.load(), ms
