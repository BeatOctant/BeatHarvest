#!/usr/bin/env python3
"""BeatSaver metadata catalogue capture for OctantHarvest.

Goals:
- unauthenticated/public metadata only (no map ZIP downloads)
- sequential, rate-limit-respecting requests
- resumable capture
- no partial output is ever labelled complete
- preserve raw response bytes (gzip-compressed) + SHA-256 ledger
- build a deterministic active-map catalogue only after terminal traversal

The base traversal uses /maps/latest?sort=CREATED&automapper=true because the
BeatSaver server applies before/after to the selected sort field. createdAt is
used as the stable traversal axis; later UPDATED and deleted deltas reconcile
changes that happened while the long base crawl was running.

A diff capture (--base) starts from a complete earlier capture instead of the
base traversal: its catalogue, updated with the maps UPDATED after that
capture's snapshotCutoff (less OVERLAP) and without those deleted since. Votes
do not move updatedAt, so every capture ends with GET /vote, which gives the
upvotes, downvotes and score of the maps voted on since votesSince.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import email.utils
import gzip
import hashlib
import itertools
import json
import os
import random
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable

SCHEMA = "octantharvest-beatsaver-catalogue-capture/v1"
BASE_URL = "https://api.beatsaver.com"
MAPS_LATEST = f"{BASE_URL}/maps/latest"
MAPS_DELETED = f"{BASE_URL}/maps/deleted"
VOTES = f"{BASE_URL}/vote"
SWAGGER_URL = f"{BASE_URL}/docs/swagger.json"
DEFAULT_UA = "OctantHarvest/catalogue-capture"
PAGE_SIZE = 100
# A diff and its votes start this much before the base's boundaries, for the gap between this clock and BeatSaver's
# and for changes that become visible after their timestamp; what is fetched twice is applied twice, to the same end.
OVERLAP = dt.timedelta(hours=1)

def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def write_json(path: Path, obj: Any) -> None:
    atomic_write(path, json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")


def read_json(path: Path) -> Any:
    with open(path, "rb") as f:
        return json.load(f)


def parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.timezone.utc)
        return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
    except Exception:
        return None


@dataclasses.dataclass
class HttpClient:
    user_agent: str
    min_delay: float
    timeout: float
    max_retries: int
    last_request_monotonic: float = 0.0

    def get(self, url: str) -> tuple[bytes, dict[str, str], int]:
        attempt = 0
        while True:
            elapsed = time.monotonic() - self.last_request_monotonic
            if elapsed < self.min_delay:
                time.sleep(self.min_delay - elapsed)
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "application/json",
                    # BeatSaver metadata pages are large JSON documents. Accept
                    # transport gzip, then digest/store the decompressed JSON bytes
                    # exactly as before. This preserves capture semantics while
                    # avoiding unnecessary uncompressed transfer volume.
                    "Accept-Encoding": "gzip",
                },
                method="GET",
            )
            self.last_request_monotonic = time.monotonic()
            request_started = time.monotonic()
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    wire_body = r.read()
                    headers = {k.lower(): v for k, v in r.headers.items()}
                    encoding = headers.get("content-encoding", "identity").strip().lower()
                    if encoding in ("", "identity"):
                        body = wire_body
                        encoding = "identity"
                    elif encoding == "gzip":
                        try:
                            body = gzip.decompress(wire_body)
                        except Exception as e:
                            raise RuntimeError(f"Invalid gzip response from BeatSaver for {url}: {e}") from e
                    else:
                        raise RuntimeError(f"Unsupported Content-Encoding {encoding!r} for {url}")
                    headers["x-octantharvest-wire-bytes"] = str(len(wire_body))
                    headers["x-octantharvest-wire-sha256"] = sha256_bytes(wire_body)
                    headers["x-octantharvest-content-encoding"] = encoding
                    headers["x-octantharvest-request-seconds"] = f"{time.monotonic() - request_started:.3f}"
                    return body, headers, int(r.status)
            except urllib.error.HTTPError as e:
                body = e.read()
                if e.code == 429:
                    wait = parse_retry_after(e.headers.get("Retry-After"))
                    if wait is None:
                        wait = min(300.0, 2.0 ** min(attempt + 1, 8))
                    print(f"HTTP 429: Retry-Afterを尊重して {wait:.1f} 秒待機します", file=sys.stderr)
                    time.sleep(wait)
                elif 500 <= e.code <= 599 and attempt < self.max_retries:
                    wait = min(120.0, (2.0 ** attempt) + random.random())
                    print(f"HTTP {e.code}: {wait:.1f} 秒後に再試行します", file=sys.stderr)
                    time.sleep(wait)
                else:
                    raise RuntimeError(f"HTTP {e.code} for {url}: {body[:500]!r}") from e
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt >= self.max_retries:
                    raise RuntimeError(f"Network failure after retries for {url}: {e}") from e
                wait = min(120.0, (2.0 ** attempt) + random.random())
                print(f"通信エラー: {wait:.1f} 秒後に再試行します: {e}", file=sys.stderr)
                time.sleep(wait)
            attempt += 1
            if attempt > self.max_retries + 1:
                raise RuntimeError(f"Too many retries for {url}")


def phase_dir(root: Path, phase: str) -> Path:
    return root / "raw" / phase


def page_filename(page_no: int) -> str:
    return f"{page_no:08d}.json.gz"


def append_ledger(root: Path, record: dict[str, Any]) -> None:
    path = root / "ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    line = canonical_json_bytes(record) + b"\n"
    with open(path, "ab") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def load_state(root: Path) -> dict[str, Any] | None:
    path = root / "capture-state.json"
    return read_json(path) if path.exists() else None


def save_state(root: Path, state: dict[str, Any]) -> None:
    write_json(root / "capture-state.json", state)


def make_url(endpoint: str, params: dict[str, Any]) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return endpoint + "?" + urllib.parse.urlencode(clean)


def docs_from_body(body: bytes) -> list[dict[str, Any]]:
    try:
        obj = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"BeatSaver returned invalid JSON: {e}") from e
    docs = obj.get("docs")
    if not isinstance(docs, list):
        raise RuntimeError("BeatSaver response has no docs[] array")
    if any(not isinstance(x, dict) for x in docs):
        raise RuntimeError("BeatSaver docs[] contains a non-object")
    return docs


def votes_from_body(body: bytes) -> list[dict[str, Any]]:
    try:
        votes = json.loads(body)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"BeatSaver returned invalid JSON: {e}") from e
    if not isinstance(votes, list) or any(
        not isinstance(v, dict) or not isinstance(v.get("mapId"), int)
        or not all(isinstance(v.get(k), (int, float)) for k in ("upvotes", "downvotes", "score"))
        for v in votes
    ):
        raise RuntimeError("BeatSaver /vote response is not a list of vote summaries")
    return votes


def records_from_body(body: bytes, phase: str) -> list[dict[str, Any]]:
    return votes_from_body(body) if phase == "votes" else docs_from_body(body)


def cursor_extreme(docs: list[dict[str, Any]], field: str, direction: str) -> str:
    values = [d.get(field) for d in docs]
    if any(not isinstance(v, str) or not v for v in values):
        raise RuntimeError(f"Missing cursor field {field!r} in response")
    return min(values) if direction == "before" else max(values)


def just_before(timestamp: str) -> str:
    """The timestamp one microsecond earlier."""
    return (dt.datetime.fromisoformat(timestamp) - dt.timedelta(microseconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def boundary_tie_count(docs: list[dict[str, Any]], field: str, cursor: str) -> int:
    return sum(1 for d in docs if d.get(field) == cursor)


def save_page(
    root: Path,
    phase: str,
    page_no: int,
    url: str,
    body: bytes,
    status: int,
    headers: dict[str, str],
    docs: list[dict[str, Any]],
    cursor_in: str | None,
    cursor_out: str | None,
    cursor_field: str,
    direction: str,
) -> None:
    p = phase_dir(root, phase) / page_filename(page_no)
    p.parent.mkdir(parents=True, exist_ok=True)
    compressed = gzip.compress(body, compresslevel=6, mtime=0)
    atomic_write(p, compressed)
    rec = {
        "schema": SCHEMA,
        "phase": phase,
        "page": page_no,
        "path": str(p.relative_to(root)).replace(os.sep, "/"),
        "url": url,
        "fetchedAt": utc_now(),
        "httpStatus": status,
        "rawBytes": len(body),
        "rawSha256": sha256_bytes(body),
        "gzipBytes": len(compressed),
        "gzipSha256": sha256_bytes(compressed),
        "docCount": len(docs),
        "cursorField": cursor_field,
        "direction": direction,
        "cursorIn": cursor_in,
        "cursorOut": cursor_out,
        "boundaryTieCount": boundary_tie_count(docs, cursor_field, cursor_out) if docs and cursor_out else 0,
        "responseHeaders": {
            k: headers[k]
            for k in (
                "date", "etag", "last-modified", "retry-after",
                "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset",
                "content-encoding", "content-length",
                "x-octantharvest-wire-bytes", "x-octantharvest-wire-sha256",
                "x-octantharvest-content-encoding", "x-octantharvest-request-seconds",
            )
            if k in headers
        },
    }
    append_ledger(root, rec)


def run_traversal(
    root: Path,
    client: HttpClient,
    state: dict[str, Any],
    *,
    phase: str,
    endpoint: str,
    sort: str | None,
    cursor_field: str,
    direction: str,
    start_cursor: str | None,
    fixed_other_bound: str | None = None,
    max_pages: int | None = None,
) -> bool:
    """Return whether the phase reached its terminal empty page (False: stopped at max_pages)."""
    ps = state.setdefault("phases", {}).setdefault(
        phase,
        {
            "status": "running",
            "nextPage": 0,
            "cursor": start_cursor,
            "startedAt": utc_now(),
            "terminalAt": None,
            "terminalEmptyPage": None,
        },
    )
    if ps.get("status") == "complete":
        return True

    cursor = ps.get("cursor", start_cursor)
    page_no = int(ps.get("nextPage", 0))
    fetched = 0

    while True:
        params: dict[str, Any] = {"pageSize": PAGE_SIZE}
        if endpoint == MAPS_LATEST:
            params["automapper"] = "true"
            if sort:
                params["sort"] = sort
        if direction == "before":
            params["before"] = cursor
            if fixed_other_bound is not None:
                params["after"] = fixed_other_bound
        else:
            params["after"] = cursor
            if fixed_other_bound is not None:
                params["before"] = fixed_other_bound

        url = make_url(endpoint, params)
        body, headers, status = client.get(url)
        docs = docs_from_body(body)
        next_cursor = cursor_extreme(docs, cursor_field, direction) if docs else cursor
        if direction == "after" and len(docs) == PAGE_SIZE:
            # `after` is strict: records sharing this full page's last timestamp may not all be on it, so ask again
            # from just before it (what comes twice is applied twice, to the same end). A page all at one timestamp
            # cannot be got past that way; then go on after it, and the tie is kept as a warning.
            again = just_before(next_cursor)
            if again != cursor:
                next_cursor = again

        save_page(
            root,
            phase,
            page_no,
            url,
            body,
            status,
            headers,
            docs,
            cursor,
            next_cursor,
            cursor_field,
            direction,
        )

        if not docs:
            ps["status"] = "complete"
            ps["terminalAt"] = utc_now()
            ps["terminalEmptyPage"] = page_no
            ps["cursor"] = cursor
            ps["nextPage"] = page_no + 1
            save_state(root, state)
            print(f"[{phase}] terminal empty page: {page_no}")
            return True

        if next_cursor == cursor:
            raise RuntimeError(f"Cursor did not advance in {phase}: {cursor}")

        cursor = next_cursor
        page_no += 1
        fetched += 1
        ps["cursor"] = cursor
        ps["nextPage"] = page_no
        ps["lastPageAt"] = utc_now()
        ps["lastRequestSeconds"] = float(headers.get("x-octantharvest-request-seconds", "0") or 0)
        ps["lastWireBytes"] = int(headers.get("x-octantharvest-wire-bytes", len(body)))
        ps["lastRawBytes"] = len(body)
        ps["lastContentEncoding"] = headers.get("x-octantharvest-content-encoding", headers.get("content-encoding", "identity"))
        ps["lastDocCount"] = len(docs)
        save_state(root, state)

        wire_kib = ps["lastWireBytes"] / 1024.0
        raw_kib = ps["lastRawBytes"] / 1024.0
        print(
            f"[{phase}] page={page_no - 1} docs={len(docs)} "
            f"request={ps['lastRequestSeconds']:.1f}s "
            f"wire={wire_kib:.1f}KiB raw={raw_kib:.1f}KiB "
            f"encoding={ps['lastContentEncoding']} cursor={cursor}",
            flush=True,
        )

        if max_pages is not None and fetched >= max_pages:
            print(f"[{phase}] --max-pages={max_pages} に到達。状態を保存して停止します。")
            return False


def fetch_source_contract(root: Path, client: HttpClient, state: dict[str, Any]) -> None:
    path = root / "source" / "swagger.json.gz"
    if path.exists():
        return
    body, headers, status = client.get(SWAGGER_URL)
    obj = json.loads(body)
    if obj.get("info", {}).get("title") != "BeatSaver API":
        raise RuntimeError("Unexpected Swagger document")
    compressed = gzip.compress(body, compresslevel=6, mtime=0)
    atomic_write(path, compressed)
    state["sourceContract"] = {
        "url": SWAGGER_URL,
        "fetchedAt": utc_now(),
        "httpStatus": status,
        "rawSha256": sha256_bytes(body),
        "gzipSha256": sha256_bytes(compressed),
        "swaggerVersion": obj.get("swagger"),
        "apiVersion": obj.get("info", {}).get("version"),
    }
    save_state(root, state)


def fetch_votes(root: Path, client: HttpClient, state: dict[str, Any]) -> None:
    """The vote totals of the maps voted on after votesSince: one request (GET /vote has no pages)."""
    if state["phases"].get("votes", {}).get("status") == "complete":
        return
    started = utc_now()
    since = state["votesSince"]
    url = make_url(VOTES, {"since": since})
    body, headers, status = client.get(url)
    votes = votes_from_body(body)
    save_page(root, "votes", 0, url, body, status, headers, votes, since, None, "since", "after")
    state["phases"]["votes"] = {"status": "complete", "startedAt": started, "terminalAt": utc_now(),
                                "since": since, "docCount": len(votes)}
    save_state(root, state)
    print(f"[votes] maps voted on since {since}: {len(votes)}")


def vote_time(started: str) -> str:
    """GET /vote's `since` for what was fetched from `started` on, in the form the API documents."""
    return (dt.datetime.fromisoformat(started) - OVERLAP).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def full_walk_at(manifest: dict[str, Any]) -> str:
    """When the last walk of the whole catalogue that a complete capture builds on began: its own start for a full
    walk, its base's for a diff."""
    return manifest["startedAt"] if "base_created" in manifest["phases"] else manifest["base"]["fullWalkAt"]


def base_record(base: Path) -> dict[str, Any]:
    """What a diff takes from its base: a complete capture whose catalogue is the one its manifest records."""
    path = base / "manifest.json"
    manifest = read_json(path) if path.exists() else {}
    if manifest.get("complete") is not True or manifest.get("schema") != SCHEMA:
        raise RuntimeError(f"--base {base} is not a complete capture")
    if sha256_bytes((base / "catalogue.jsonl.gz").read_bytes()) != manifest["build"]["catalogueFileSha256"]:
        raise RuntimeError(f"--base {base}: catalogue.jsonl.gz is not the one its manifest.json records")
    return {
        "capture": base.name,
        "path": str(base),
        "startedAt": manifest["startedAt"],
        "snapshotCutoff": manifest["snapshotCutoff"],
        "catalogueFileSha256": manifest["build"]["catalogueFileSha256"],
        "fullWalkAt": full_walk_at(manifest),
    }


def init_state(root: Path, base: Path | None = None) -> dict[str, Any]:
    """The capture's state; a new one walks the whole catalogue, or starts from `base` (a diff). A capture that is
    resumed keeps what it started as."""
    state = load_state(root)
    if state is not None:
        if state.get("schema") != SCHEMA:
            raise RuntimeError(f"Unsupported existing capture schema: {state.get('schema')}")
        started_from = state["base"]["path"] if state["base"] else None
        if base is not None and str(base) != started_from:
            how = f"{started_from} からの差分" if started_from else "全件の取得"
            print(f"再開します。このcaptureは{how}として始めたので、--base {base} は使いません", file=sys.stderr)
        return state
    b = base_record(base) if base is not None else None  # before the folder is made: a rejected base leaves none
    root.mkdir(parents=True, exist_ok=True)
    started = utc_now()
    state = {
        "schema": SCHEMA,
        "complete": False,
        "startedAt": started,
        "baseCutoff": started,
        # the votes of a map are as of when it was fetched; this capture fetches from startedAt
        "votesSince": vote_time(started),
        "pageSize": PAGE_SIZE,
        "baseUrl": BASE_URL,
        "base": None,
        "strategy": {
            "base": "CREATED descending before fixed baseCutoff",
            "reconcile": "UPDATED ascending after baseCutoff to a terminal empty response; then deleted ascending after baseCutoff and before that update-terminal observation time",
            "votes": "GET /vote since votesSince: the upvotes, downvotes and score of the maps voted on since, applied last",
            "automapper": True,
            "archivesDownloaded": False,
            "authenticationUsed": False,
        },
        "phases": {},
        "sourceContract": None,
    }
    if b is not None:
        state["base"] = b
        # the base's catalogue is as of its snapshotCutoff, and its votes as of its own start or later
        cutoff = dt.datetime.fromisoformat(b["snapshotCutoff"]) - OVERLAP
        state["baseCutoff"] = cutoff.isoformat().replace("+00:00", "Z")
        state["votesSince"] = vote_time(b["startedAt"])
        state["strategy"]["base"] = "the catalogue of the complete capture in base; baseCutoff is its snapshotCutoff less OVERLAP (one hour)"
    save_state(root, state)
    return state


def walked_phases(state: dict[str, Any]) -> tuple[str, ...]:
    """The traversals that end on a terminal empty page; a diff starts from its base's catalogue, not base_created."""
    return (() if state["base"] else ("base_created",)) + ("delta_updated", "delta_deleted")


def iter_page_bodies(root: Path, phase: str) -> Iterable[bytes]:
    d = phase_dir(root, phase)
    if not d.exists():
        return
    for p in sorted(d.glob("*.json.gz")):
        with gzip.open(p, "rb") as f:
            yield f.read()


def build_catalogue(root: Path) -> dict[str, Any]:
    state = load_state(root)
    if not state:
        raise RuntimeError("No capture-state.json")
    for name in walked_phases(state) + ("votes",):
        if state.get("phases", {}).get(name, {}).get("status") != "complete":
            raise RuntimeError(f"Cannot build: phase {name} is not complete")
    base = state["base"]
    if base is not None:
        base_catalogue = Path(base["path"]) / "catalogue.jsonl.gz"
        if not base_catalogue.exists() or sha256_bytes(base_catalogue.read_bytes()) != base["catalogueFileSha256"]:
            raise RuntimeError(f"Cannot build: base capture {base['path']} is gone or its catalogue changed")

    # Use a temporary SQLite staging database so the build does not require all
    # MapDetail objects to fit in RAM. The canonical output remains JSONL; the
    # SQLite file is only working state and is removed after a successful build.
    db_path = root / "catalogue-build-working.sqlite3"
    if db_path.exists():
        db_path.unlink()
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("CREATE TABLE maps (id TEXT PRIMARY KEY, id_int INTEGER NOT NULL, json BLOB NOT NULL)")
    con.execute("CREATE INDEX maps_order ON maps(id_int, id)")
    con.execute("CREATE TABLE tombstones (id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)")

    base_seen = 0
    update_seen = 0
    delete_seen = 0
    duplicate_base = 0
    votes_seen = 0
    votes_applied = 0

    def base_pages() -> Iterable[list[dict[str, Any]]]:
        if base is None:
            for body in iter_page_bodies(root, "base_created"):
                yield docs_from_body(body)
        else:
            with gzip.open(base_catalogue, "rb") as f:
                while docs := [json.loads(line) for line in itertools.islice(f, PAGE_SIZE)]:
                    yield docs

    try:
        for docs in base_pages():
            for doc in docs:
                mid = doc.get("id")
                if not isinstance(mid, str) or not mid:
                    raise RuntimeError("Map without id in base")
                base_seen += 1
                exists = con.execute("SELECT 1 FROM maps WHERE id=?", (mid,)).fetchone() is not None
                if exists:
                    duplicate_base += 1
                try:
                    iid = int(mid, 16)
                except ValueError:
                    iid = 2**63 - 1
                con.execute(
                    "INSERT INTO maps(id,id_int,json) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET id_int=excluded.id_int,json=excluded.json",
                    (mid, iid, canonical_json_bytes(doc)),
                )
            con.commit()

        for body in iter_page_bodies(root, "delta_updated"):
            for doc in docs_from_body(body):
                mid = doc.get("id")
                if not isinstance(mid, str) or not mid:
                    raise RuntimeError("Map without id in update delta")
                update_seen += 1
                try:
                    iid = int(mid, 16)
                except ValueError:
                    iid = 2**63 - 1
                con.execute(
                    "INSERT INTO maps(id,id_int,json) VALUES(?,?,?) "
                    "ON CONFLICT(id) DO UPDATE SET id_int=excluded.id_int,json=excluded.json",
                    (mid, iid, canonical_json_bytes(doc)),
                )
            con.commit()

        for body in iter_page_bodies(root, "delta_deleted"):
            for doc in docs_from_body(body):
                mid = doc.get("id")
                deleted_at = doc.get("deletedAt")
                if not isinstance(mid, str) or not isinstance(deleted_at, str):
                    raise RuntimeError("Malformed deletion record")
                delete_seen += 1
                con.execute(
                    "INSERT INTO tombstones(id,deleted_at) VALUES(?,?) "
                    "ON CONFLICT(id) DO UPDATE SET deleted_at=excluded.deleted_at",
                    (mid, deleted_at),
                )
                con.execute("DELETE FROM maps WHERE id=?", (mid,))
            con.commit()

        # Last: the totals of the maps voted on since votesSince, newer than any record above. A map that is not in
        # the catalogue (deleted, unpublished) has no record to update.
        for body in iter_page_bodies(root, "votes"):
            for vote in votes_from_body(body):
                votes_seen += 1
                row = con.execute("SELECT id,json FROM maps WHERE id_int=?", (vote["mapId"],)).fetchone()
                if row is None:
                    continue
                doc = json.loads(row[1])
                doc["stats"].update({k: vote[k] for k in ("upvotes", "downvotes", "score")})
                con.execute("UPDATE maps SET json=? WHERE id=?", (canonical_json_bytes(doc), row[0]))
                votes_applied += 1
            con.commit()

        catalogue_path = root / "catalogue.jsonl.gz"
        h = hashlib.sha256()
        record_count = 0
        tmp = catalogue_path.with_name(catalogue_path.name + ".tmp")
        with open(tmp, "wb") as raw_out:
            with gzip.GzipFile(fileobj=raw_out, mode="wb", compresslevel=6, mtime=0) as gz:
                for (blob,) in con.execute("SELECT json FROM maps ORDER BY id_int, id"):
                    line = bytes(blob) + b"\n"
                    h.update(line)
                    gz.write(line)
                    record_count += 1
            raw_out.flush()
            os.fsync(raw_out.fileno())
        os.replace(tmp, catalogue_path)

        tombstone_path = root / "tombstones.jsonl.gz"
        th = hashlib.sha256()
        tmp = tombstone_path.with_name(tombstone_path.name + ".tmp")
        tombstone_count = 0
        with open(tmp, "wb") as raw_out:
            with gzip.GzipFile(fileobj=raw_out, mode="wb", compresslevel=6, mtime=0) as gz:
                for mid, deleted_at in con.execute("SELECT id,deleted_at FROM tombstones ORDER BY deleted_at,id"):
                    line = canonical_json_bytes({"id": mid, "deletedAt": deleted_at}) + b"\n"
                    th.update(line)
                    gz.write(line)
                    tombstone_count += 1
            raw_out.flush()
            os.fsync(raw_out.fileno())
        os.replace(tmp, tombstone_path)
    finally:
        con.close()

    # Working state is not part of the finished capture.
    for suffix in ("", "-wal", "-shm"):
        q = Path(str(db_path) + suffix)
        if q.exists():
            q.unlink()

    summary = {
        "baseDocsObserved": base_seen,
        "baseDuplicateIds": duplicate_base,
        "updateDocsObserved": update_seen,
        "deleteDocsObserved": delete_seen,
        "votesObserved": votes_seen,
        "votesApplied": votes_applied,
        "activeRecordCount": record_count,
        "tombstoneCount": tombstone_count,
        "catalogueCanonicalSha256": "sha256:" + h.hexdigest(),
        "catalogueFileSha256": sha256_bytes(catalogue_path.read_bytes()),
        "tombstonesCanonicalSha256": "sha256:" + th.hexdigest(),
        "tombstonesFileSha256": sha256_bytes(tombstone_path.read_bytes()),
    }
    write_json(root / "build-summary.json", summary)
    return summary


def verify_capture(root: Path, require_manifest: bool = False) -> dict[str, Any]:
    state = load_state(root)
    if not state:
        raise RuntimeError("No capture-state.json")
    ledger_path = root / "ledger.jsonl"
    if not ledger_path.exists():
        raise RuntimeError("No ledger.jsonl")
    rows = []
    with open(ledger_path, "rb") as f:
        for n, line in enumerate(f, 1):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise RuntimeError(f"Invalid ledger line {n}: {e}") from e

    seen_paths: set[str] = set()
    tie_warnings = []
    for row in rows:
        rel = row["path"]
        if rel in seen_paths:
            raise RuntimeError(f"Duplicate ledger path: {rel}")
        seen_paths.add(rel)
        p = root / rel
        if not p.exists():
            raise RuntimeError(f"Missing page: {rel}")
        gz = p.read_bytes()
        if sha256_bytes(gz) != row["gzipSha256"]:
            raise RuntimeError(f"Gzip digest mismatch: {rel}")
        raw = gzip.decompress(gz)
        if sha256_bytes(raw) != row["rawSha256"]:
            raise RuntimeError(f"Raw digest mismatch: {rel}")
        docs = records_from_body(raw, row["phase"])
        if len(docs) != row["docCount"]:
            raise RuntimeError(f"docCount mismatch: {rel}")
        if row.get("boundaryTieCount", 0) > 1 and row.get("docCount") == PAGE_SIZE:
            tie_warnings.append({"path": rel, "count": row["boundaryTieCount"], "cursor": row.get("cursorOut")})

    for phase in walked_phases(state):
        ps = state.get("phases", {}).get(phase)
        if not ps or ps.get("status") != "complete":
            raise RuntimeError(f"Phase is not complete: {phase}")
        terminal = ps.get("terminalEmptyPage")
        rel = f"raw/{phase}/{page_filename(int(terminal))}"
        terminal_row = next((r for r in rows if r["path"] == rel), None)
        if not terminal_row or terminal_row.get("docCount") != 0:
            raise RuntimeError(f"Terminal empty page not proven for {phase}")
    votes = state.get("phases", {}).get("votes")
    if not votes or votes.get("status") != "complete" or f"raw/votes/{page_filename(0)}" not in seen_paths:
        raise RuntimeError("Phase is not complete: votes")

    summary = read_json(root / "build-summary.json") if (root / "build-summary.json").exists() else None
    if summary:
        cat = root / "catalogue.jsonl.gz"
        tomb = root / "tombstones.jsonl.gz"
        if sha256_bytes(cat.read_bytes()) != summary["catalogueFileSha256"]:
            raise RuntimeError("catalogue.jsonl.gz digest mismatch")
        if sha256_bytes(tomb.read_bytes()) != summary["tombstonesFileSha256"]:
            raise RuntimeError("tombstones.jsonl.gz digest mismatch")

    if require_manifest:
        manifest = read_json(root / "manifest.json")
        if manifest.get("complete") is not True:
            raise RuntimeError("manifest.json does not claim complete=true")

    return {
        "verifiedPageCount": len(rows),
        "boundaryTieWarnings": tie_warnings,
        "buildSummary": summary,
    }


def finalize_manifest(root: Path, state: dict[str, Any], summary: dict[str, Any], verification: dict[str, Any]) -> dict[str, Any]:
    ledger_bytes = (root / "ledger.jsonl").read_bytes()
    manifest = {
        "schema": SCHEMA,
        "complete": True,
        "completedAt": utc_now(),
        "startedAt": state["startedAt"],
        "baseCutoff": state["baseCutoff"],
        "snapshotCutoff": state.get("snapshotCutoff"),
        "votesSince": state["votesSince"],
        "base": state["base"],
        "sourceContract": state.get("sourceContract"),
        "source": {
            "mapsLatest": MAPS_LATEST,
            "mapsDeleted": MAPS_DELETED,
            "votes": VOTES,
            "authentication": "none",
            "mapArchivesDownloaded": False,
            "automapper": True,
            "pageSize": PAGE_SIZE,
        },
        "strategy": state["strategy"],
        "phases": state["phases"],
        "ledgerSha256": sha256_bytes(ledger_bytes),
        "ledgerBytes": len(ledger_bytes),
        "build": summary,
        "verification": {
            "verifiedPageCount": verification["verifiedPageCount"],
            "boundaryTieWarnings": verification["boundaryTieWarnings"],
        },
        "limitations": [
            "BeatSaver before/after cursors are strict timestamp comparisons and expose no secondary ID tie-breaker.",
            "snapshotCutoff is the local observation time of the terminal empty UPDATED response, not a database transaction timestamp; the capture therefore retains this boundary evidence rather than claiming a server-side transactional snapshot.",
            "Any full 100-record page whose boundary timestamp is shared by multiple returned records is retained as a warning for downstream review.",
            "This artifact is public metadata collected as OctantHarvest's crawl input, not a BeatSaver archive mirror.",
            "upvotes, downvotes and score are GET /vote's totals for the maps voted on since votesSince; a map's other stats are as of when its record was fetched.",
        ] + ([
            "A diff capture carries its base's records forward: a map not updated since keeps the record fetched then. Changes BeatSaver does not mark with updatedAt reach a diff only with the next full walk.",
        ] if state["base"] else []),
    }
    write_json(root / "manifest.json", manifest)
    state["complete"] = True
    state["completedAt"] = manifest["completedAt"]
    save_state(root, state)
    return manifest


def capture(args: argparse.Namespace) -> int:
    root = Path(args.output).expanduser().resolve()
    state = init_state(root, Path(args.base).expanduser().resolve() if args.base else None)
    client = HttpClient(args.user_agent, args.min_delay, args.timeout, args.max_retries)
    fetch_source_contract(root, client, state)

    max_pages = args.max_pages
    base_cutoff = state["baseCutoff"]

    if state["base"] is None:
        terminal = run_traversal(
            root, client, state,
            phase="base_created",
            endpoint=MAPS_LATEST,
            sort="CREATED",
            cursor_field="createdAt",
            direction="before",
            start_cursor=base_cutoff,
            max_pages=max_pages,
        )
        if not terminal:
            return 2

    # Reconcile every public map update/new map that occurred after the long
    # base crawl began. Because `after` is strict and the server query is ASC,
    # we advance using the maximum updatedAt seen in each page (just before it
    # after a full page, see run_traversal).
    terminal = run_traversal(
        root, client, state,
        phase="delta_updated",
        endpoint=MAPS_LATEST,
        sort="UPDATED",
        cursor_field="updatedAt",
        direction="after",
        start_cursor=base_cutoff,
        max_pages=max_pages,
    )
    if not terminal:
        return 2

    # Bound deletion reconciliation by the update traversal's terminal
    # observation. This avoids allowing later deletions to move the intended
    # catalogue boundary forward after the active-map view has already been
    # frozen.
    snapshot_cutoff = state["phases"]["delta_updated"]["terminalAt"]
    state["snapshotCutoff"] = snapshot_cutoff
    save_state(root, state)

    terminal = run_traversal(
        root, client, state,
        phase="delta_deleted",
        endpoint=MAPS_DELETED,
        sort=None,
        cursor_field="deletedAt",
        direction="after",
        start_cursor=base_cutoff,
        fixed_other_bound=snapshot_cutoff,
        max_pages=max_pages,
    )
    if not terminal:
        return 2

    fetch_votes(root, client, state)

    summary = build_catalogue(root)
    verification = verify_capture(root)
    manifest = finalize_manifest(root, state, summary, verification)
    print("\n完全captureをfinalizeしました。")
    print(f"active maps: {summary['activeRecordCount']}")
    print(f"catalogue digest: {summary['catalogueCanonicalSha256']}")
    print(f"manifest: {root / 'manifest.json'}")
    if manifest["verification"]["boundaryTieWarnings"]:
        print("注意: timestamp boundary tie warningがあります。catalogueを使う前にmanifestを確認してください。", file=sys.stderr)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.output).expanduser().resolve()
    result = verify_capture(root, require_manifest=args.require_manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    root = Path(args.output).expanduser().resolve()
    summary = build_catalogue(root)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    root = Path(args.output).expanduser().resolve()
    state = load_state(root)
    if not state:
        print("capture stateなし")
        return 1
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


def self_test() -> int:
    # Pure local sanity checks: canonicalization, gzip determinism, cursor rules.
    sample = {"docs": [
        {"id": "2", "createdAt": "2026-01-01T00:00:02Z", "updatedAt": "2026-01-01T00:00:02Z"},
        {"id": "1", "createdAt": "2026-01-01T00:00:01Z", "updatedAt": "2026-01-01T00:00:01Z"},
    ]}
    raw = canonical_json_bytes(sample)
    assert docs_from_body(raw)[0]["id"] == "2"
    docs = docs_from_body(raw)
    assert cursor_extreme(docs, "createdAt", "before").endswith("01Z")
    assert cursor_extreme(docs, "createdAt", "after").endswith("02Z")
    gz = gzip.compress(raw, mtime=0)
    assert gz == gzip.compress(raw, mtime=0)
    assert gzip.decompress(gz) == raw
    assert sha256_bytes(raw).startswith("sha256:")
    print("self-test: OK")
    return 0


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="BeatSaver complete metadata catalogue capture for OctantHarvest")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("capture", help="開始またはresumeして、terminal traversal後にだけmanifestを作る")
    c.add_argument("--output", default="beatsaver-catalogue-capture", help="出力directory")
    c.add_argument("--min-delay", type=float, default=1.0, help="各HTTP request間の最低秒数 (default: 1.0)")
    c.add_argument("--timeout", type=float, default=60.0, help="socket inactivity timeout; not a whole-request wall-clock deadline")
    c.add_argument("--max-retries", type=int, default=8)
    c.add_argument("--max-pages", type=int, default=None, help="動作確認用。到達時はincompleteのまま安全停止")
    c.add_argument("--user-agent", default=DEFAULT_UA)
    c.add_argument("--base", default=None, help="この完了済みcaptureから始め、その後の更新・削除・投票だけを取る（差分）。再開するcaptureは始めたときのまま")
    c.set_defaults(func=capture)

    v = sub.add_parser("verify", help="保存page/digest/terminal evidenceを検証")
    v.add_argument("--output", default="beatsaver-catalogue-capture")
    v.add_argument("--require-manifest", action="store_true")
    v.set_defaults(func=cmd_verify)

    b = sub.add_parser("build", help="完了済みraw captureからcatalogueを再構築")
    b.add_argument("--output", default="beatsaver-catalogue-capture")
    b.set_defaults(func=cmd_build)

    s = sub.add_parser("status", help="resume stateを表示")
    s.add_argument("--output", default="beatsaver-catalogue-capture")
    s.set_defaults(func=cmd_status)

    t = sub.add_parser("self-test", help="network不要の簡易self-test")
    t.set_defaults(func=lambda _: self_test())
    return p


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n中断しました。完了済みpageとcapture-state.jsonは保持されています。再実行でresumeできます。", file=sys.stderr)
        return 130
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
