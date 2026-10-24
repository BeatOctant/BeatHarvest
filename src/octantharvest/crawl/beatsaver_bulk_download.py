#!/usr/bin/env python3
"""Resumable BeatSaver catalogue ZIP downloader for an HDD archive.

The supplied BeatSaver catalogue is the source of truth.  Every unique
versions[].hash is downloaded at most once to:

    <archive>/maps/<sha1[0:2]>/<sha1>.zip

The BeatSaver version hash is used as the canonical archive key and URL key;
it is NOT assumed to be the SHA-1 of the distributed ZIP bytes.  A committed
file is accepted only after the HTTP transfer completes, Content-Length
matches when supplied, the ZIP central directory opens, every member passes
its ZIP CRC check, and the exact ZIP bytes receive an archival SHA-256.
An append-only journal makes reruns and a later, larger catalogue cheap:
already verified hashes are skipped without reading the ZIP from HDD when
size+mtime still match their journal verification record.

HDD policy:
- concurrent HTTP downloads;
- staging on /dev/shm (or another non-HDD path) when possible;
- exactly one HDD writer, so network concurrency does not become random HDD
  write concurrency;
- writes are atomic (.part -> final).

Failure policy:
- 408/425/429/500/502/503/504 and network timeouts use shared exponential
  backoff plus random jitter;
- after a bounded burst of retries a task is deferred, not abandoned;
- deferred tasks are retried in later passes (2 extra passes by default) and a
  hash still deferred after 3 runs is given up, then tried again after 30 days;
- 404/410/451 are recorded as unavailable and do not stop the run;
- HTTP 403, output low-space, and other conditions that indicate an operator
  or policy problem stop the run deliberately rather than hammering a server;
- SIGINT/SIGTERM is graceful.  Rerunning the same command resumes safely.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import argparse
import dataclasses
import email.utils
import errno
import gzip
import hashlib
import json
import os
import posixpath
import queue
import random
import re
import shutil
import signal
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

VERSION = "0.2.1"
HEX40_RE = re.compile(r"^[0-9a-fA-F]{40}$")
TRANSIENT_HTTP = {408, 425, 429, 500, 502, 503, 504}
PERMANENT_UNAVAILABLE_HTTP = {404, 410, 451}
CHUNK = 8 * 1024 * 1024
GIB = 1024 ** 3
MIB = 1024 ** 2


class PermanentDownloadError(RuntimeError):
    """The current task cannot be made safe by immediate retries."""


class TransientDownloadError(RuntimeError):
    """A retry may produce the canonical BeatSaver ZIP."""


class BrokenZipError(TransientDownloadError):
    """The whole response arrived but is not a valid ZIP. Usually the file on the server is broken, so it is tried
    once per pass rather than retriesPerCycle times; a hash broken in several runs is given up (record_deferred)."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log(message: str, *, stream=sys.stdout) -> None:
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    print(f"[{stamp}] {message}", file=stream, flush=True)


def gib(n: int) -> float:
    return n / GIB


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def validate_zip_crc(path: Path) -> int:
    """Fully read every ZIP member so Python verifies each member CRC.

    Returns the member count. Raises BadZipFile/RuntimeError on failure.
    This validates the downloaded transport/archive without assuming that
    BeatSaver versions[].hash is the SHA-1 of the ZIP container bytes.
    """
    with zipfile.ZipFile(path, "r") as zf:
        infos = zf.infolist()
        bad = zf.testzip()
        if bad is not None:
            raise zipfile.BadZipFile(f"CRC failure in member: {bad}")
        if not infos:
            raise zipfile.BadZipFile("empty ZIP archive")
        return len(infos)


def open_catalogue(path: Path):
    if path.suffix.casefold() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("rt", encoding="utf-8")


def allowed_beatsaver_url(url: str, expected_hash: str) -> bool:
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    host = (p.hostname or "").casefold().rstrip(".")
    if p.scheme != "https":
        return False
    if not (host == "beatsaver.com" or host.endswith(".beatsaver.com")):
        return False
    return posixpath.basename(p.path).casefold() == f"{expected_hash.casefold()}.zip"


@dataclasses.dataclass(frozen=True, slots=True)
class Task:
    map_id: str
    key: str
    version_hash: str
    url: str

    def destination(self, root: Path) -> Path:
        return root / "maps" / self.version_hash[:2] / f"{self.version_hash}.zip"


@dataclasses.dataclass(slots=True)
class DownloadResult:
    task: Task
    status: str
    stage_path: Path | None = None
    size: int = 0
    final_url: str | None = None
    http_status: int | None = None
    error: str | None = None
    attempts: int = 0
    elapsed_s: float = 0.0
    holds_stage_slot: bool = False
    zip_sha256: str | None = None
    zip_members: int = 0


class Journal:
    """Append-only, crash-tolerant state.  Only the last event per hash is indexed."""

    def __init__(self, path: Path, fsync_every: int = 64):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.fsync_every = max(1, int(fsync_every))
        self._since_sync = 0
        self.latest: dict[str, dict] = {}
        self.deferred_runs: dict[str, int] = {}  # runs that ended with the hash still deferred (record_deferred)
        self.commit_seq = 0
        if path.exists():
            with path.open("rt", encoding="utf-8", errors="replace") as f:
                for line in f:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        # A torn final line after power loss is harmless.
                        continue
                    h = obj.get("hash")
                    if isinstance(h, str) and HEX40_RE.fullmatch(h):
                        self.latest[h.casefold()] = obj
                        if obj.get("event") == "run_deferred":
                            self.deferred_runs[h.casefold()] = int(obj.get("deferred_runs", 0))
                    seq = obj.get("commit_seq")
                    if isinstance(seq, int):
                        self.commit_seq = max(self.commit_seq, seq)
        self.fp = path.open("a", encoding="utf-8", buffering=1)

    def next_commit_seq(self) -> int:
        # Called only by the single HDD writer, but keep it protected anyway.
        with self.lock:
            self.commit_seq += 1
            return self.commit_seq

    def append(self, obj: dict, *, force_sync: bool = False) -> None:
        obj = dict(obj)
        obj.setdefault("ts", utc_now())
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            self.fp.write(line)
            h = obj.get("hash")
            if isinstance(h, str) and HEX40_RE.fullmatch(h):
                self.latest[h.casefold()] = obj
                if obj.get("event") == "run_deferred":
                    self.deferred_runs[h.casefold()] = int(obj.get("deferred_runs", 0))
            self._since_sync += 1
            if force_sync or self._since_sync >= self.fsync_every:
                self.fp.flush()
                os.fsync(self.fp.fileno())
                self._since_sync = 0

    def close(self) -> None:
        with self.lock:
            self.fp.flush()
            os.fsync(self.fp.fileno())
            self.fp.close()


class StartRateLimiter:
    """Global request-start limiter plus shared server/network cooldown."""

    def __init__(self, requests_per_second: float):
        self.interval = 1.0 / max(0.01, float(requests_per_second))
        self.lock = threading.Lock()
        self.next_start = 0.0
        self.block_until = 0.0

    def wait(self, stop_event: threading.Event) -> bool:
        while not stop_event.is_set():
            with self.lock:
                now = time.monotonic()
                target = max(self.next_start, self.block_until)
                delay = target - now
                if delay <= 0:
                    self.next_start = now + self.interval
                    return True
            stop_event.wait(min(delay, 1.0))
        return False

    def backoff(self, seconds: float) -> None:
        seconds = max(0.0, float(seconds))
        with self.lock:
            self.block_until = max(self.block_until, time.monotonic() + seconds)


def retry_after_seconds(headers, default: float) -> float:
    raw = headers.get("Retry-After") if headers else None
    if raw:
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return max(0.0, dt.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                pass
    return max(0.0, float(default))


def transient_delay(attempt: int, *, http_status: int | None = None, headers=None) -> float:
    base = min(300.0, 2.0 ** min(attempt, 8))
    if headers is not None and headers.get("Retry-After"):
        base = retry_after_seconds(headers, base)
        return base + random.uniform(0.25, max(0.5, base * 0.10))
    if http_status == 503:
        return random.uniform(base, min(600.0, base * 2.0))
    if http_status == 429:
        return random.uniform(base, min(450.0, base * 1.5))
    return random.uniform(base, min(450.0, base * 1.5))


def load_tasks(catalogue: Path) -> tuple[list[Task], dict, list[dict]]:
    tasks_by_hash: dict[str, Task] = {}
    map_count = 0
    version_occurrences = 0
    duplicate_hashes = 0
    state_counts: Counter[str] = Counter()
    host_counts: Counter[str] = Counter()
    errors: list[dict] = []

    def source_error(line_no: int, error: str, detail: str = "") -> None:
        item = {"line": line_no, "error": error}
        if detail:
            item["detail"] = detail[:2000]
        errors.append(item)

    # gzip CRC/truncation errors are intentionally not swallowed: a physically corrupt
    # catalogue cannot establish that we saw the complete source set.
    with open_catalogue(catalogue) as f:
        for line_no, line in enumerate(f, 1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                source_error(line_no, "invalid_json", str(e))
                continue
            map_count += 1
            map_id = str(obj.get("id", ""))
            versions = obj.get("versions")
            if not isinstance(versions, list) or not versions:
                source_error(line_no, "missing_versions", map_id)
                continue
            for v in versions:
                version_occurrences += 1
                if not isinstance(v, dict):
                    source_error(line_no, "invalid_version_object", map_id)
                    continue
                h = str(v.get("hash", "")).casefold()
                url = str(v.get("downloadURL", ""))
                key = str(v.get("key", map_id))
                state = str(v.get("state", ""))
                state_counts[state] += 1
                if not HEX40_RE.fullmatch(h):
                    source_error(line_no, "invalid_version_hash", repr(h))
                    continue
                if not allowed_beatsaver_url(url, h):
                    source_error(line_no, "rejected_download_url", f"{h} {url}")
                    continue
                host_counts[urllib.parse.urlsplit(url).hostname or ""] += 1
                task = Task(map_id=map_id, key=key, version_hash=h, url=url)
                prior = tasks_by_hash.get(h)
                if prior is not None:
                    # Content-addressed archive: one canonical ZIP per version hash.
                    duplicate_hashes += 1
                    continue
                tasks_by_hash[h] = task

    tasks = sorted(tasks_by_hash.values(), key=lambda t: t.version_hash)
    return tasks, {
        "maps": map_count,
        "version_occurrences": version_occurrences,
        "unique_versions": len(tasks),
        "duplicate_hash_occurrences": duplicate_hashes,
        "states": dict(state_counts),
        "hosts": dict(host_counts),
        "source_errors": len(errors),
    }, errors


def atomic_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_jsonl(path: Path, rows: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def same_device(a: Path, b: Path) -> bool:
    try:
        return a.stat().st_dev == b.stat().st_dev
    except OSError:
        return False


def choose_staging_dir(output: Path, requested: str) -> tuple[Path, bool]:
    if requested != "auto":
        p = Path(requested).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        return p, same_device(p, output)

    candidates = [Path("/dev/shm") / "octantharvest-download", Path("/tmp") / "octantharvest-download"]
    for p in candidates:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.mkdir(parents=True, exist_ok=True)
            if not os.access(p, os.W_OK):
                continue
            if shutil.disk_usage(p).free < 4 * GIB:
                continue
            if same_device(p, output):
                continue
            return p.resolve(), False
        except OSError:
            continue

    p = (output / ".staging").resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p, True


def cleanup_staging(staging: Path) -> int:
    removed = 0
    try:
        for p in staging.rglob("*.part"):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def verified_by_journal(task: Task, dest: Path, event: dict | None) -> bool:
    if not event or event.get("status") != "verified":
        return False
    try:
        st = dest.stat()
    except OSError:
        return False
    digest = event.get("zip_sha256")
    return (
        int(event.get("size", -1)) == st.st_size
        and int(event.get("mtime_ns", -1)) == st.st_mtime_ns
        and isinstance(digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def quarantine_corrupt(output: Path, task: Task, dest: Path, reason_tag: str) -> Path:
    qdir = output / "quarantine" / task.version_hash[:2]
    qdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", reason_tag)[:80] or "invalid"
    target = qdir / f"{task.version_hash}.{safe}.{stamp}.bad.zip"
    os.replace(dest, target)
    return target


def record_deferred(journal: Journal, pending: Iterable[Task], give_up_after_runs: int) -> list[Task]:
    """A run used up its retry passes: count it for every hash still deferred. A hash deferred at the end of
    `give_up_after_runs` runs (0: never) is marked blocked, so later runs skip it unless --retry-blocked; a version
    whose zip is broken on the server stays broken, and a fixed upload is a new version hash.
    -> the tasks that stay deferred."""
    still = []
    for task in pending:
        h = task.version_hash.casefold()
        n = journal.deferred_runs.get(h, 0) + 1
        give_up = give_up_after_runs > 0 and n >= give_up_after_runs
        journal.append({
            "event": "run_deferred",
            "status": "blocked" if give_up else "retry_later",
            "hash": task.version_hash,
            "map_id": task.map_id,
            "key": task.key,
            "source_url": task.url,
            "deferred_runs": n,
            "error": (journal.latest.get(h) or {}).get("error"),
        }, force_sync=True)
        if not give_up:
            still.append(task)
    return still


def due_for_recheck(event: dict, recheck_days: float) -> bool:
    """A hash given up by record_deferred is tried again once `recheck_days` have passed (0: never), in case the
    server's file was repaired."""
    if recheck_days <= 0 or event.get("event") != "run_deferred":
        return False
    try:
        at = datetime.fromisoformat(str(event.get("ts", "")).replace("Z", "+00:00"))
    except ValueError:
        return True
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - at >= timedelta(days=recheck_days)


def preflight_existing(
    tasks: Iterable[Task],
    output: Path,
    journal: Journal,
    retry_permanent: bool,
    retry_blocked: bool,
    stop_event: threading.Event,
    recheck_days: float = 0,
) -> tuple[list[Task], Counter]:
    pending: list[Task] = []
    stats = Counter()
    for i, task in enumerate(tasks, 1):
        if stop_event.is_set():
            break
        dest = task.destination(output)
        part = dest.with_suffix(dest.suffix + ".part")
        if part.exists():
            part.unlink(missing_ok=True)
        event = journal.latest.get(task.version_hash)
        if verified_by_journal(task, dest, event):
            stats["verified_cached"] += 1
            continue
        if (
            not retry_permanent
            and event
            and event.get("status") == "unavailable"
            and int(event.get("http_status", 0)) in PERMANENT_UNAVAILABLE_HTTP
        ):
            stats["unavailable_cached"] += 1
            continue
        if not retry_blocked and event and event.get("status") == "blocked" and not due_for_recheck(event, recheck_days):
            stats["blocked_cached"] += 1
            continue
        if dest.exists():
            try:
                members = validate_zip_crc(dest)
                digest = file_sha256(dest)
                st = dest.stat()
                journal.append({
                    "event": "existing_verified",
                    "status": "verified",
                    "verification": "zip_crc+sha256-v1",
                    "hash": task.version_hash,
                    "map_id": task.map_id,
                    "key": task.key,
                    "source_url": task.url,
                    "zip_sha256": digest,
                    "zip_members": members,
                    "size": st.st_size,
                    "mtime_ns": st.st_mtime_ns,
                })
                stats["verified_existing"] += 1
                continue
            except (OSError, RuntimeError, zipfile.BadZipFile, EOFError) as e:
                q = quarantine_corrupt(output, task, dest, type(e).__name__)
                journal.append({
                    "event": "existing_corrupt",
                    "status": "corrupt",
                    "hash": task.version_hash,
                    "map_id": task.map_id,
                    "key": task.key,
                    "source_url": task.url,
                    "error": repr(e),
                    "quarantine_path": str(q),
                }, force_sync=True)
                stats["corrupt_existing"] += 1
        pending.append(task)
        if i % 10000 == 0:
            log(
                f"preflight {i:,}: pending={len(pending):,} "
                f"verified={stats['verified_cached'] + stats['verified_existing']:,}"
            )
    return pending, stats


def download_one(
    task: Task,
    staging: Path,
    limiter: StartRateLimiter,
    stage_slots: threading.Semaphore,
    stop_event: threading.Event,
    timeout: float,
    retries_per_cycle: int,
    max_file_bytes: int,
    user_agent: str,
) -> DownloadResult:
    start = time.monotonic()
    while not stop_event.is_set():
        if stage_slots.acquire(timeout=0.5):
            break
    else:
        return DownloadResult(task=task, status="cancelled")

    keep_stage = False
    stage_path = staging / task.version_hash[:2] / f"{task.version_hash}.{threading.get_ident()}.part"
    stage_path.parent.mkdir(parents=True, exist_ok=True)
    last_error = ""
    last_http: int | None = None
    try:
        for attempt in range(1, retries_per_cycle + 1):
            if stop_event.is_set():
                return DownloadResult(task=task, status="cancelled", attempts=attempt - 1)
            if not limiter.wait(stop_event):
                return DownloadResult(task=task, status="cancelled", attempts=attempt - 1)
            stage_path.unlink(missing_ok=True)
            req = urllib.request.Request(task.url, headers={
                "User-Agent": user_agent,
                "Accept": "application/zip,application/octet-stream;q=0.9,*/*;q=0.1",
            })
            total = 0
            sha256 = hashlib.sha256()
            final_url = None
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    final_url = resp.geturl()
                    if not allowed_beatsaver_url(final_url, task.version_hash):
                        raise PermanentDownloadError(f"redirected to rejected URL: {final_url}")
                    size_hdr = resp.headers.get("Content-Length")
                    if size_hdr:
                        try:
                            advertised = int(size_hdr)
                        except ValueError:
                            advertised = -1
                        if advertised > max_file_bytes:
                            raise PermanentDownloadError(
                                f"Content-Length {advertised} exceeds --max-file-mib limit {max_file_bytes}"
                            )
                    with stage_path.open("wb") as out:
                        while True:
                            chunk = resp.read(CHUNK)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > max_file_bytes:
                                raise PermanentDownloadError(
                                    f"download exceeded --max-file-mib limit {max_file_bytes}"
                                )
                            sha256.update(chunk)
                            out.write(chunk)
                        out.flush()
                        os.fsync(out.fileno())
                digest = sha256.hexdigest()
                if total <= 0:
                    raise TransientDownloadError("zero-byte response")
                if size_hdr:
                    try:
                        advertised = int(size_hdr)
                    except ValueError:
                        advertised = -1
                    if advertised >= 0 and total != advertised:
                        raise TransientDownloadError(
                            f"Content-Length mismatch expected={advertised} actual={total}"
                        )
                try:
                    members = validate_zip_crc(stage_path)
                except (zipfile.BadZipFile, EOFError, RuntimeError, OSError) as e:
                    raise BrokenZipError(f"ZIP integrity failure: {e!r}") from e
                keep_stage = True
                return DownloadResult(
                    task=task,
                    status="downloaded",
                    stage_path=stage_path,
                    size=total,
                    final_url=final_url,
                    attempts=attempt,
                    elapsed_s=time.monotonic() - start,
                    holds_stage_slot=True,
                    zip_sha256=digest,
                    zip_members=members,
                )
            except urllib.error.HTTPError as e:
                stage_path.unlink(missing_ok=True)
                last_http = e.code
                last_error = str(e)
                if e.code in PERMANENT_UNAVAILABLE_HTTP:
                    return DownloadResult(
                        task=task, status="unavailable", http_status=e.code,
                        error=str(e), attempts=attempt, elapsed_s=time.monotonic() - start,
                    )
                if e.code == 403:
                    # 403 is deliberately not treated as a minor network failure. Repeated
                    # unattended retries after an explicit access denial are unsafe.
                    stop_event.set()
                    return DownloadResult(
                        task=task, status="fatal", http_status=e.code,
                        error="HTTP 403: access denied; stopping to avoid repeated denied bulk requests",
                        attempts=attempt, elapsed_s=time.monotonic() - start,
                    )
                if e.code not in TRANSIENT_HTTP:
                    return DownloadResult(
                        task=task, status="blocked", http_status=e.code,
                        error=str(e), attempts=attempt, elapsed_s=time.monotonic() - start,
                    )
                delay = transient_delay(attempt, http_status=e.code, headers=e.headers)
                limiter.backoff(delay)
                log(
                    f"transient HTTP {e.code} hash={task.version_hash[:10]}... "
                    f"attempt={attempt}/{retries_per_cycle}; shared backoff {delay:.1f}s",
                    stream=sys.stderr,
                )
                stop_event.wait(delay)
            except PermanentDownloadError as e:
                stage_path.unlink(missing_ok=True)
                return DownloadResult(
                    task=task, status="blocked", error=str(e), attempts=attempt,
                    elapsed_s=time.monotonic() - start,
                )
            except BrokenZipError as e:
                stage_path.unlink(missing_ok=True)
                return DownloadResult(
                    task=task, status="retry_later", error=repr(e), attempts=attempt,
                    elapsed_s=time.monotonic() - start,
                )
            except (urllib.error.URLError, TimeoutError, TransientDownloadError) as e:
                stage_path.unlink(missing_ok=True)
                last_error = repr(e)
                delay = transient_delay(attempt)
                limiter.backoff(delay)
                stop_event.wait(delay)
            except PermissionError as e:
                stage_path.unlink(missing_ok=True)
                stop_event.set()
                return DownloadResult(
                    task=task, status="fatal", error=f"staging permission error: {e!r}",
                    attempts=attempt, elapsed_s=time.monotonic() - start,
                )
            except OSError as e:
                stage_path.unlink(missing_ok=True)
                last_error = repr(e)
                if e.errno in (errno.ENOSPC, errno.EROFS):
                    stop_event.set()
                    return DownloadResult(
                        task=task, status="fatal", error=f"staging filesystem error: {e!r}",
                        attempts=attempt, elapsed_s=time.monotonic() - start,
                    )
                delay = transient_delay(attempt)
                limiter.backoff(delay)
                stop_event.wait(delay)

        # Do not abandon an otherwise plausible hash. Main will put this task in a later pass.
        return DownloadResult(
            task=task, status="retry_later", http_status=last_http,
            error=last_error or "transient retry burst exhausted", attempts=retries_per_cycle,
            elapsed_s=time.monotonic() - start,
        )
    finally:
        if not keep_stage:
            stage_path.unlink(missing_ok=True)
            stage_slots.release()


def commit_to_hdd(
    result: DownloadResult,
    output: Path,
    journal: Journal,
    stage_slots: threading.Semaphore,
    min_free_bytes: int,
) -> tuple[str, int]:
    task = result.task
    if result.status != "downloaded" or result.stage_path is None:
        journal.append({
            "event": "download_result",
            "status": result.status,
            "hash": task.version_hash,
            "map_id": task.map_id,
            "key": task.key,
            "source_url": task.url,
            "http_status": result.http_status,
            "error": result.error,
            "attempts": result.attempts,
            "elapsed_s": round(result.elapsed_s, 3),
        }, force_sync=result.status == "fatal")
        return result.status, 0

    stage = result.stage_path
    dest = task.destination(output)
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    try:
        usage = shutil.disk_usage(output)
        if usage.free - result.size < min_free_bytes:
            journal.append({
                "event": "low_space",
                "status": "fatal",
                "hash": task.version_hash,
                "map_id": task.map_id,
                "key": task.key,
                "source_url": task.url,
                "needed_bytes": result.size,
                "free_bytes": usage.free,
                "min_free_bytes": min_free_bytes,
            }, force_sync=True)
            return "fatal", 0

        # The only HDD-writing thread executes this block.
        with stage.open("rb") as src, part.open("wb") as dst:
            shutil.copyfileobj(src, dst, CHUNK)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(part, dest)
        st = dest.stat()
        commit_seq = journal.next_commit_seq()
        journal.append({
            "event": "committed",
            "status": "verified",
            "hash": task.version_hash,
            "map_id": task.map_id,
            "key": task.key,
            "source_url": task.url,
            "final_url": result.final_url,
            "verification": "zip_crc+sha256-v1",
            "zip_sha256": result.zip_sha256,
            "zip_members": result.zip_members,
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "attempts": result.attempts,
            "download_elapsed_s": round(result.elapsed_s, 3),
            # Future HDD scanners can consume commit order as a locality hint.
            "commit_seq": commit_seq,
        })
        return "verified", st.st_size
    except OSError as e:
        journal.append({
            "event": "hdd_write_error",
            "status": "fatal",
            "hash": task.version_hash,
            "map_id": task.map_id,
            "key": task.key,
            "source_url": task.url,
            "error": repr(e),
        }, force_sync=True)
        return "fatal", 0
    finally:
        part.unlink(missing_ok=True)
        stage.unlink(missing_ok=True)
        if result.holds_stage_slot:
            stage_slots.release()


def writer_loop(
    result_q: queue.Queue,
    output: Path,
    journal: Journal,
    stage_slots: threading.Semaphore,
    min_free_bytes: int,
    stop_event: threading.Event,
    stats: Counter,
    stats_lock: threading.Lock,
    retry_tasks: list[Task],
    retry_lock: threading.Lock,
    total_pending: int,
    pass_no: int,
) -> None:
    done = 0
    bytes_written = 0
    while True:
        item = result_q.get()
        try:
            if item is None:
                return
            result: DownloadResult = item
            status, nbytes = commit_to_hdd(result, output, journal, stage_slots, min_free_bytes)
            done += 1
            bytes_written += nbytes
            if status == "retry_later":
                with retry_lock:
                    retry_tasks.append(result.task)
            with stats_lock:
                stats[status] += 1
                stats["bytes_written"] += nbytes
            if status == "fatal":
                stop_event.set()
            if done == 1 or done % 100 == 0 or done == total_pending:
                with stats_lock:
                    ok = stats["verified"]
                    unavailable = stats["unavailable"]
                    deferred = stats["retry_later"]
                    blocked = stats["blocked"]
                log(
                    f"pass={pass_no} progress {done:,}/{total_pending:,} "
                    f"verified={ok:,} unavailable={unavailable:,} "
                    f"deferred={deferred:,} blocked={blocked:,} "
                    f"written={gib(bytes_written):.2f} GiB"
                )
        finally:
            result_q.task_done()


def worker_loop(
    task_q: queue.Queue,
    result_q: queue.Queue,
    staging: Path,
    limiter: StartRateLimiter,
    stage_slots: threading.Semaphore,
    stop_event: threading.Event,
    timeout: float,
    retries_per_cycle: int,
    max_file_bytes: int,
    user_agent: str,
) -> None:
    while True:
        task = task_q.get()
        try:
            if task is None:
                return
            if stop_event.is_set():
                result_q.put(DownloadResult(task=task, status="cancelled"))
                continue
            result = download_one(
                task, staging, limiter, stage_slots, stop_event,
                timeout, retries_per_cycle, max_file_bytes, user_agent,
            )
            result_q.put(result)
        finally:
            task_q.task_done()


def run_pass(
    pending: list[Task],
    *,
    pass_no: int,
    output: Path,
    staging: Path,
    journal: Journal,
    limiter: StartRateLimiter,
    stop_event: threading.Event,
    concurrency: int,
    stage_slot_count: int,
    timeout: float,
    retries_per_cycle: int,
    max_file_bytes: int,
    min_free_bytes: int,
    user_agent: str,
) -> tuple[list[Task], Counter]:
    stage_slots = threading.Semaphore(stage_slot_count)
    task_q: queue.Queue = queue.Queue(maxsize=max(8, concurrency * 4))
    result_q: queue.Queue = queue.Queue(maxsize=max(2, stage_slot_count * 2))
    stats = Counter()
    stats_lock = threading.Lock()
    retry_tasks: list[Task] = []
    retry_lock = threading.Lock()

    writer = threading.Thread(
        target=writer_loop,
        name="hdd-writer",
        args=(
            result_q, output, journal, stage_slots, min_free_bytes,
            stop_event, stats, stats_lock, retry_tasks, retry_lock,
            len(pending), pass_no,
        ),
        daemon=True,
    )
    writer.start()

    workers = [
        threading.Thread(
            target=worker_loop,
            name=f"net-{i+1}",
            args=(
                task_q, result_q, staging, limiter, stage_slots, stop_event,
                timeout, retries_per_cycle, max_file_bytes, user_agent,
            ),
            daemon=True,
        )
        for i in range(concurrency)
    ]
    for w in workers:
        w.start()

    for task in pending:
        if stop_event.is_set():
            break
        while not stop_event.is_set():
            try:
                task_q.put(task, timeout=0.5)
                break
            except queue.Full:
                pass

    for _ in workers:
        while True:
            try:
                task_q.put(None, timeout=0.5)
                break
            except queue.Full:
                if stop_event.is_set() and all(not w.is_alive() for w in workers):
                    break

    task_q.join()
    for w in workers:
        w.join(timeout=2.0)

    result_q.put(None)
    result_q.join()
    writer.join(timeout=10.0)

    with retry_lock:
        retry_copy = sorted(retry_tasks, key=lambda t: t.version_hash)
    with stats_lock:
        stats_copy = Counter(stats)
    return retry_copy, stats_copy


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Download every unique BeatSaver ZIP referenced by catalogue.jsonl(.gz), "
            "with ZIP CRC/SHA-256 verification, crash-resume, indefinite transient retries, and one HDD writer."
        )
    )
    p.add_argument("catalogue", type=Path, help="catalogue.jsonl or catalogue.jsonl.gz")
    p.add_argument("output", type=Path, help="HDD archive root")
    p.add_argument(
        "--concurrency", type=int, default=6,
        help="concurrent network downloads (default: 6)",
    )
    p.add_argument(
        "--rps", type=float, default=3.0,
        help="global request starts/sec (default: 3.0, the most BeatSaver is asked for)",
    )
    p.add_argument("--staging-dir", default="auto", help="auto prefers /dev/shm, then a non-HDD /tmp")
    p.add_argument(
        "--staging-gib", type=float, default=12.0,
        help="maximum transient staging reservation budget (default: 12 GiB)",
    )
    p.add_argument(
        "--max-file-mib", type=int, default=1536,
        help="hard per-ZIP safety limit (default: 1536 MiB)",
    )
    p.add_argument(
        "--min-free-gib", type=float, default=50.0,
        help="stop before HDD free space would fall below this (default: 50 GiB)",
    )
    p.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout seconds (default: 120)")
    p.add_argument(
        "--retries-per-cycle", type=int, default=6,
        help="transient attempts before deferring a hash to a later retry pass (default: 6)",
    )
    p.add_argument(
        "--max-retry-cycles", type=int, default=2,
        help="extra deferred retry passes; 0 = unlimited (default: 2)",
    )
    p.add_argument(
        "--user-agent", default=f"OctantHarvest/bulk-download-{VERSION}",
        help="explicit User-Agent",
    )
    p.add_argument("--retry-permanent", action="store_true", help="retry prior 404/410/451 entries")
    p.add_argument("--retry-blocked", action="store_true", help="retry prior non-transient blocked entries")
    p.add_argument(
        "--give-up-after-runs", type=int, default=3,
        help="mark a hash blocked once this many runs ended with it still deferred; 0 = never (default: 3)",
    )
    p.add_argument(
        "--recheck-given-up-days", type=float, default=30,
        help="try a hash blocked by --give-up-after-runs again after this many days; 0 = never (default: 30)",
    )
    p.add_argument("--dry-run", action="store_true", help="validate catalogue and print plan without network requests")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    catalogue = args.catalogue.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not catalogue.is_file():
        log(f"catalogue not found: {catalogue}", stream=sys.stderr)
        return 2
    if args.concurrency < 1 or args.concurrency > 16:
        log("--concurrency must be 1..16", stream=sys.stderr)
        return 2
    if not (0.05 <= args.rps <= 3.0):
        log("--rps must be 0.05..3.0; BeatSaver is never asked more than 3 starts/s", stream=sys.stderr)
        return 2
    if args.retries_per_cycle < 1 or args.retries_per_cycle > 20:
        log("--retries-per-cycle must be 1..20", stream=sys.stderr)
        return 2
    if args.max_retry_cycles < 0:
        log("--max-retry-cycles must be >=0", stream=sys.stderr)
        return 2
    if args.give_up_after_runs < 0:
        log("--give-up-after-runs must be >=0", stream=sys.stderr)
        return 2

    stop_event = threading.Event()

    def request_stop(signum, _frame):
        if not stop_event.is_set():
            log(f"signal {signum}: graceful stop requested; rerun is safe", stream=sys.stderr)
            stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    log(f"validating catalogue: {catalogue}")
    catalogue_sha256 = file_sha256(catalogue)
    try:
        tasks, cat_stats, cat_errors = load_tasks(catalogue)
    except (OSError, EOFError, gzip.BadGzipFile) as e:
        log(f"catalogue validation failed: {e}", stream=sys.stderr)
        return 2

    output.mkdir(parents=True, exist_ok=True)

    if cat_errors:
        atomic_jsonl(output / "catalogue.errors.jsonl", cat_errors)

    staging, staging_on_output_device = choose_staging_dir(output, args.staging_dir)
    removed_parts = cleanup_staging(staging)
    if removed_parts:
        log(f"removed {removed_parts:,} stale staging .part files")

    max_file_bytes = args.max_file_mib * MIB
    requested_stage_budget = int(args.staging_gib * GIB)
    try:
        stage_free = shutil.disk_usage(staging).free
    except OSError as e:
        log(f"cannot inspect staging filesystem: {e}", stream=sys.stderr)
        return 2
    # Leave at least 20% (and at least 2 GiB) of the staging filesystem free.
    staging_reserve = max(2 * GIB, int(stage_free * 0.20))
    usable_stage = max(0, stage_free - staging_reserve)
    effective_stage_budget = min(requested_stage_budget, usable_stage)
    stage_slot_count = effective_stage_budget // max_file_bytes
    if stage_slot_count < 1:
        log(
            f"staging has insufficient safe free space: free={gib(stage_free):.2f} GiB "
            f"max_file={args.max_file_mib} MiB",
            stream=sys.stderr,
        )
        return 2

    effective_concurrency = min(args.concurrency, int(stage_slot_count))
    if staging_on_output_device and effective_concurrency > 1:
        log(
            "staging is on the same filesystem as the archive; forcing network concurrency=1 "
            "to avoid concurrent random writes on the HDD",
            stream=sys.stderr,
        )
        effective_concurrency = 1
        stage_slot_count = 1
    else:
        stage_slot_count = min(int(stage_slot_count), max(1, effective_concurrency * 2))

    min_free_bytes = int(args.min_free_gib * GIB)

    log(
        f"catalogue maps={cat_stats['maps']:,} version_occurrences={cat_stats['version_occurrences']:,} "
        f"unique_versions={cat_stats['unique_versions']:,} selected={len(tasks):,} "
        f"duplicates_deduped={cat_stats['duplicate_hash_occurrences']:,} source_errors={cat_stats['source_errors']:,}"
    )
    log(f"catalogue_sha256={catalogue_sha256} states={cat_stats['states']} hosts={cat_stats['hosts']}")
    log(
        f"output={output} staging={staging} staging_same_device={staging_on_output_device} "
        f"network_concurrency={effective_concurrency} request_starts={args.rps:.2f}/s "
        f"stage_slots={stage_slot_count} one_HDD_writer=true transient_retry_cycles="
        f"{'unlimited' if args.max_retry_cycles == 0 else args.max_retry_cycles}"
    )

    meta_path = output / "archive_meta.json"
    journal_path = output / "download.journal.jsonl"
    meta = {
        "format": "beatmap-music-identity-beatsaver-archive-v2",
        "downloader_version": VERSION,
        "catalogue_path": str(catalogue),
        "catalogue_sha256": catalogue_sha256,
        "catalogue_maps": cat_stats["maps"],
        "catalogue_version_occurrences": cat_stats["version_occurrences"],
        "catalogue_unique_versions": cat_stats["unique_versions"],
        "duplicate_hash_occurrences_deduped": cat_stats["duplicate_hash_occurrences"],
        "catalogue_source_errors": cat_stats["source_errors"],
        "selected_versions": len(tasks),
        "states": cat_stats["states"],
        "hosts": cat_stats["hosts"],
        "layout": "maps/<version_hash[0:2]>/<version_hash>.zip",
        "integrity_rule": "version hash is archive key; transfer length (when provided), full ZIP CRC, and ZIP SHA-256 are verified before commit",
        "journal": "download.journal.jsonl",
        "commit_seq_note": "monotonic HDD commit order; useful as a later sequential-scan locality hint",
        "complete": False,
        "updated_at": utc_now(),
    }
    atomic_json(meta_path, meta)

    if args.dry_run:
        log("dry-run complete; no network requests made")
        return 0 if not cat_errors else 1

    journal = Journal(journal_path)
    try:
        journal.append({
            "event": "run_start",
            "status": "info",
            "catalogue_sha256": catalogue_sha256,
            "selected_versions": len(tasks),
            "source_errors": len(cat_errors),
            "rps": args.rps,
            "network_concurrency": effective_concurrency,
            "staging_dir": str(staging),
            "staging_same_device": staging_on_output_device,
            "output": str(output),
        }, force_sync=True)

        log("checking existing archive/journal before network work")
        pending, pre_stats = preflight_existing(
            tasks, output, journal, args.retry_permanent, args.retry_blocked, stop_event,
            args.recheck_given_up_days,
        )
        if stop_event.is_set():
            log("stopped during preflight", stream=sys.stderr)
            return 130
        log(
            f"preflight complete: pending={len(pending):,} "
            f"verified_cached={pre_stats['verified_cached']:,} "
            f"verified_existing={pre_stats['verified_existing']:,} "
            f"unavailable_cached={pre_stats['unavailable_cached']:,} "
            f"blocked_cached={pre_stats['blocked_cached']:,} "
            f"corrupt_quarantined={pre_stats['corrupt_existing']:,}"
        )

        verified_total = pre_stats["verified_cached"] + pre_stats["verified_existing"]
        unavailable_total = pre_stats["unavailable_cached"]
        blocked_total = pre_stats["blocked_cached"]
        bytes_written_total = 0
        retry_events_total = 0
        limiter = StartRateLimiter(args.rps)
        pass_no = 0

        while pending and not stop_event.is_set():
            pass_no += 1
            log(f"starting download pass {pass_no}: {len(pending):,} hashes")
            deferred, pass_stats = run_pass(
                pending,
                pass_no=pass_no,
                output=output,
                staging=staging,
                journal=journal,
                limiter=limiter,
                stop_event=stop_event,
                concurrency=effective_concurrency,
                stage_slot_count=int(stage_slot_count),
                timeout=args.timeout,
                retries_per_cycle=args.retries_per_cycle,
                max_file_bytes=max_file_bytes,
                min_free_bytes=min_free_bytes,
                user_agent=args.user_agent,
            )
            verified_total += pass_stats["verified"]
            unavailable_total += pass_stats["unavailable"]
            blocked_total += pass_stats["blocked"]
            bytes_written_total += pass_stats["bytes_written"]
            retry_events_total += pass_stats["retry_later"]

            meta.update({
                "updated_at": utc_now(),
                "pass": pass_no,
                "verified": verified_total,
                "unavailable": unavailable_total,
                "blocked": blocked_total,
                "deferred_after_pass": len(deferred),
                "bytes_written_this_run": bytes_written_total,
            })
            atomic_json(meta_path, meta)

            if stop_event.is_set():
                pending = deferred
                break
            if pass_stats["fatal"]:
                stop_event.set()
                pending = deferred
                break
            if not deferred:
                pending = []
                break

            extra_cycles_done = pass_no - 1
            if args.max_retry_cycles and extra_cycles_done >= args.max_retry_cycles:
                pending = deferred
                log(
                    f"retry cycle limit reached with {len(pending):,} deferred hashes",
                    stream=sys.stderr,
                )
                break

            # The longer a server/source problem survives, the less aggressively we revisit it.
            base = min(1800.0, 30.0 * (2.0 ** min(pass_no - 1, 6)))
            cooldown = random.uniform(base, min(1800.0, base * 2.0))
            log(
                f"pass {pass_no} left {len(deferred):,} transient hashes; "
                f"cooling down {cooldown:.1f}s before automatic retry",
                stream=sys.stderr,
            )
            if stop_event.wait(cooldown):
                pending = deferred
                break
            pending = deferred

        if pending and not stop_event.is_set():  # the retry passes ran out, not a stop
            still = record_deferred(journal, pending, args.give_up_after_runs)
            if len(still) < len(pending):
                log(f"gave up on {len(pending) - len(still):,} hashes deferred in "
                    f"{args.give_up_after_runs} runs; marked blocked", stream=sys.stderr)
            blocked_total += len(pending) - len(still)
            pending = still

        blocked_is_error = blocked_total > 0
        complete = (
            not stop_event.is_set()
            and not pending
            and not blocked_is_error
            and not cat_errors
            and verified_total + unavailable_total == len(tasks)
        )
        meta.update({
            "complete": complete,
            "updated_at": utc_now(),
            "verified": verified_total,
            "unavailable": unavailable_total,
            "blocked": blocked_total,
            "deferred_remaining": len(pending),
            "retry_events_this_run": retry_events_total,
            "bytes_written_this_run": bytes_written_total,
        })
        atomic_json(meta_path, meta)
        journal.append({
            "event": "run_complete" if complete else "run_incomplete",
            "status": "info",
            "complete": complete,
            "verified": verified_total,
            "unavailable": unavailable_total,
            "blocked": blocked_total,
            "deferred_remaining": len(pending),
            "source_errors": len(cat_errors),
            "passes": pass_no,
        }, force_sync=True)

        if complete:
            log(
                f"complete: verified={verified_total:,} unavailable={unavailable_total:,} "
                f"passes={pass_no:,}"
            )
            return 0
        if stop_event.is_set():
            log(
                f"stopped: verified={verified_total:,} unavailable={unavailable_total:,} "
                f"blocked={blocked_total:,} deferred={len(pending):,}; rerun is safe",
                stream=sys.stderr,
            )
            return 130
        log(
            f"incomplete: verified={verified_total:,} unavailable={unavailable_total:,} "
            f"blocked={blocked_total:,} deferred={len(pending):,} source_errors={len(cat_errors):,}; "
            "rerun is safe",
            stream=sys.stderr,
        )
        return 1
    finally:
        journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
