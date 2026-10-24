"""Nearby artists for "似た曲": map artists -> MusicBrainz artist IDs -> ListenBrainz similar artists (OctantSpecs
decisions.md, 近いアーティスト; ListenBrainz was chosen 2026-10-11, and OctantVector law 3.2 allows it since OctantSpecs #83).

Names are only used to look an artist up (law 3.2: no similarity between name strings). Each service is asked one
request per second at most with a User-Agent that names us and a contact (crawl.artists.userAgent, required;
OctantSpecs contracts/external.md); the two limits are separate,
so the two services are asked side by side. Both caches are append-only JSONL under paths.artists, so a run can stop
anywhere and the next one continues; `budget` caps the requests to each service in one run, spent on the names with
the most maps first.

  mb_names.jsonl    {"name", "mbid" (null: no confident match), "mbName", "v", "at"}  one line per looked-up name;
                    a name left unmatched by an older matcher (`v` below MATCHER) is looked up again
  lb_similar.jsonl  {"mbid", "algorithm", "similar": [{"mbid", "name", "score"}], "at"}

`export` writes OctantVector's input from the caches (OctantSpecs contracts/generation-input.md, contract 4 (OctantHarvest
crawl to OctantVector)), next to them; no names go into it (law 3.2):
  map_artists.jsonl.gz      {"hash", "mbids"} for each map version with IDs (map_artist_ids)
  similar_artists.jsonl.gz  a header {"algorithm", "fetchedAt"}, then {"mbid", "similar": [[mbid, score], ...]}
"""
import gzip
import http.client
import itertools
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import now

MB_SEARCH = "https://musicbrainz.org/ws/2/artist"
LB_SIMILAR = "https://labs.api.listenbrainz.org/similar-artists/json"
# the ListenBrainz similar-artists algorithm: part of the cache key and of the exported header, so changing it starts
# the similar-artist cache over
ALGORITHM = "session_based_days_7500_session_300_contribution_5_threshold_10_limit_100_filter_True_skip_30"
MATCHER = 3  # 2: any result with score >= 90, punctuation-blind names, no placeholder names or artists; 3: search aliases and sort names too

# MusicBrainz's special purpose artists (musicbrainz.org/doc/Style/Unknown_and_untitled/Special_purpose_artist): their "similar artists" are noise ([unknown], Various Artists, ...)
SPECIAL_MBIDS = {"125ec42a-7229-4250-afc5-e057484327fe", "89ad4ac3-39f7-470e-963a-56509c546377",
                 "f731ccc4-e22a-43af-a747-64213329e088", "33cf029c-63b0-41a0-9855-be2a3665fb3b",
                 "314e1c25-dde7-4e4d-b2f4-0a7b9f7c56dc", "eec63d3c-3b81-4ad4-b1e4-7c147d4d2b61",
                 "9be7f096-97ec-4615-8957-8d40b5dcbc41", "66ea0139-149f-4a0c-8fbf-5ea9ec4a6e49",
                 "a0ef7e1d-44ff-4039-9435-7d5fefdeecc9", "7e84f845-ac16-41fe-9ff8-df12eb32af55",
                 "90068d37-bae7-4292-be4a-704c145bd616", "80a8851f-444c-4539-892b-ad2a49292aa9"}
PLACEHOLDERS = {"unknown", "unknown artist", "various", "various artists", "va", "idk", "n/a", "na", "none", "no one",
                "nobody", "anonymous", "artist", "?", "??", "???", "-", "tba", "tbd", "me", "test"}

_COVER = re.compile(r"^\s*(covered\s+by|cover(ed)?\s*:)\s*", re.I)
_CV = re.compile(r"[（(]\s*(?:cv|vo|vocal|vocals)\s*[.:：]?\s*([^)）]+)[)）]", re.I)
_PAREN = re.compile(r"[（(][^)）]*[)）]")
_SPLIT = re.compile(r"\s*(?:/|／|,(?!\s*the\s)|、|&|＆|×|\s+x\s+|\s+vs\.?\s+|\s+feat\.?\s+|\s+ft\.?\s+|\s+featuring\s+)\s*",
                    re.I)


def _clean(n):
    return re.sub(r"\s+", " ", n).strip(" -–—_.")


def split_artists(raw):
    """-> (the whole name, its parts) of one songAuthorName, e.g. "covered by Bao The Whale" -> ("Bao The Whale", []),
    "Doja Cat/Rainych" -> ("Doja Cat/Rainych", ["Doja Cat", "Rainych"]), "Akame (CV: Sora Amamiya)" -> ("Akame",
    ["Sora Amamiya"]). The whole name is tried first, so a band like "Earth, Wind & Fire" is not split when it matches."""
    s = _COVER.sub("", (raw or "").strip())
    voices = [_clean(m) for m in _CV.findall(s)]
    s = re.sub(r"\.(mp3|ogg|wav|egg)\b", "", _PAREN.sub(" ", s), flags=re.I)
    whole = _clean(s)
    twice = re.fullmatch(r"(.+?)\s+-\s+(.+)", whole)
    if twice and _key(twice[1]) == _key(twice[2]):  # "Sabrina Carpenter - Sabrina Carpenter"
        whole = twice[1]
    parts = []
    for n in _SPLIT.split(whole) + voices:
        n = _clean(n)
        if len(n) >= 2 and _key(n) != _key(whole) and _key(n) not in {_key(x) for x in parts}:
            parts.append(n)
    return (whole if len(whole) >= 2 else None), parts


def _mbid(names, name):
    m = (names.get(_key(name)) or {}).get("mbid")
    return m if m and m not in SPECIAL_MBIDS and _key(name) not in PLACEHOLDERS else None


def map_artist_ids(raw, names):
    """MusicBrainz IDs of one songAuthorName from the name cache: the whole name's when it matched, else its parts',
    only when every part matched (a band name split at a comma would otherwise keep a wrong part's ID)."""
    whole, parts = split_artists(raw)
    hit = _mbid(names, whole) if whole else None
    if hit:
        return [hit]
    ids = [_mbid(names, p) for p in parts]
    if not ids or not all(ids):
        return []
    return list(dict.fromkeys(ids))


def _key(name):
    return re.sub(r"\s+", " ", name).strip().casefold()


def _loose(name):
    return re.sub(r"[\W_]+", "", name.casefold())


def catalogue_artists(catalogue_path):
    """-> ({map version hash: songAuthorName}, Counter of names (whole and parts) by the maps that carry them)."""
    by_map, count = {}, Counter()
    with gzip.open(catalogue_path, "rt", encoding="utf-8") as f:
        for line in f:
            m = json.loads(line)
            raw = m.get("metadata", {}).get("songAuthorName") or ""
            whole, parts = split_artists(raw)
            for v in m.get("versions", []):
                by_map[v["hash"]] = raw
            count.update(_key(n) for n in ([whole] if whole else []) + parts)
    return by_map, count


class Http:
    """GET JSON with request starts at least `min_delay` seconds apart per host, retrying 429/5xx and network errors.
    Safe to call from several threads: each start takes the next free slot of its host, so requests may overlap while
    the rate of starts stays at the host's limit. A 429/5xx pushes the host's next slot back for every thread."""

    def __init__(self, user_agent, min_delay=1.0, timeout=30.0, max_retries=6, log=sys.stderr):
        self.ua, self.min_delay, self.timeout, self.max_retries, self.log = user_agent, min_delay, timeout, max_retries, log
        self.next = {}  # host -> monotonic time of its next free slot
        self.lock = threading.Lock()

    def _slot(self, host):
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next.get(host, now))
            self.next[host] = start + self.min_delay
        if start > now:
            time.sleep(start - now)

    def _hold(self, host, pause):
        with self.lock:
            self.next[host] = max(self.next.get(host, 0.0), time.monotonic() + pause)

    def get(self, url):
        host = urllib.parse.urlsplit(url).netloc
        for attempt in range(self.max_retries + 1):
            self._slot(host)
            req = urllib.request.Request(url, headers={"User-Agent": self.ua, "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None
                if e.code != 429 and not 500 <= e.code <= 599 or attempt == self.max_retries:
                    raise
                after = e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Reset-In")
                pause = float(after) if after and after.replace(".", "", 1).isdigit() else 0.0
                if pause <= 0:  # MusicBrainz sends 503 with Retry-After: 0
                    pause = min(120.0, 2.0 ** (attempt + 1))
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):  # RemoteDisconnected is not a URLError
                if attempt == self.max_retries:
                    raise
                pause = min(120.0, 2.0 ** attempt + random.random())
            print(f"{host}: waiting {pause:.0f}s before trying again", file=self.log)
            self._hold(host, pause)  # the next slot of this host, for every thread, is after the pause


def _windowed(items, fn, width):
    """(item, fn(item)) in the order of `items`, with up to `width` calls running at once."""
    with ThreadPoolExecutor(max_workers=width) as pool:
        pending = deque()
        for item in items:
            pending.append((item, pool.submit(fn, item)))
            if len(pending) >= width:
                item0, f = pending.popleft()
                yield item0, f.result()
        while pending:
            item0, f = pending.popleft()
            yield item0, f.result()


def _load(path, key):
    out = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                out[key(r)] = r
    return out


def _append(path, rec):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def match_artist(name, found):
    """The MusicBrainz artist for `name` from a search result, or None when no result is an exact name, sort name or
    alias match (ignoring case, spaces and punctuation) with a score of 90 or more (a wrong ID would pull in another artist's neighbours, so unsure means none)."""
    k = _loose(name)
    if not k or _key(name) in PLACEHOLDERS:
        return None
    for a in (found or {}).get("artists", []):
        if int(a.get("score", 0)) < 90:
            break
        if a.get("id") in SPECIAL_MBIDS:
            continue
        names = [a.get("name", ""), a.get("sort-name", "")] + [x.get("name", "") for x in a.get("aliases") or []]
        names += [", ".join(reversed(n.split(", ", 1))) for n in names if ", " in n]  # "Miku, Hatsune" -> "Hatsune Miku"
        if k in {_loose(n) for n in names if n}:
            return a
    return None


def parse_similar(body):
    """ListenBrainz labs answers a list of rows."""
    return [{"mbid": r["artist_mbid"], "name": r.get("name"), "score": r.get("score")}
            for r in body or [] if r.get("artist_mbid")]


def fetch(s, catalogue_path, budget=None, http=None, log=sys.stderr):
    """Most maps first: look names up on MusicBrainz and fetch the similar artists of their IDs from ListenBrainz, each
    service in its own thread at its own one request per second, so the two run side by side, and up to
    crawl.artists.inflight requests per service wait for their answers at once (their starts stay minDelay apart, so a
    slow answer no longer slows the rate). The ListenBrainz thread
    follows the MusicBrainz one in the same order, which gives the popular artists their neighbours in the first runs.
    `budget` caps the requests to each service (None: crawl.artists.budget; 0: no limit). -> counts of this run and of
    the caches."""
    c = s["crawl"]["artists"]
    if not c["userAgent"]:
        raise SystemExit("set crawl.artists.userAgent in the settings: MusicBrainz asks for a User-Agent with a contact, "
                         "e.g. \"OctantHarvest/0.1 ( you@example.org )\" (OctantSpecs contracts/external.md)")
    if c["minDelay"] < 1:
        raise SystemExit("crawl.artists.minDelay must be at least 1: MusicBrainz and ListenBrainz are asked one request "
                         "per second at most (OctantSpecs contracts/external.md)")
    root = Path(s["paths"]["artists"])
    http = http or Http(c["userAgent"], c["minDelay"], c["timeout"], c["maxRetries"], log)
    budget = c["budget"] if budget is None else budget
    names = _load(root / "mb_names.jsonl", lambda r: r["name"])
    similar = _load(root / "lb_similar.jsonl", lambda r: (r["mbid"], r["algorithm"]))
    _, count = catalogue_artists(catalogue_path)
    alg = ALGORITHM
    order = [k for k, n in count.most_common() if n >= c["minMaps"] and k not in PLACEHOLDERS]
    spent, run = Counter(), Counter()
    looked = threading.Condition()
    state = {"done": 0, "errors": []}  # done: how many of `order` the MusicBrainz thread has passed

    width = max(1, c["inflight"])

    def search(k):
        phrase = k.replace("\\", " ").replace('"', " ")
        # artist: alone searches only the official name (ヨルシカ), so a romanised "yorushika" finds nothing
        query = f'artist:"{phrase}" OR alias:"{phrase}" OR sortname:"{phrase}"'
        return match_artist(k, http.get(f"{MB_SEARCH}?{urllib.parse.urlencode({'query': query, 'fmt': 'json', 'limit': 10})}"))

    def neighbours_of(mbid):
        return parse_similar(http.get(f"{LB_SIMILAR}?{urllib.parse.urlencode({'artist_mbids': mbid, 'algorithm': alg})}"))

    def lookups():
        try:
            todo = [(i, k) for i, k in enumerate(order)
                    if names.get(k) is None or (not names[k]["mbid"] and names[k]["v"] < MATCHER)]
            for (i, k), a in _windowed(todo[:budget] if budget else todo, lambda ik: search(ik[1]), width):
                spent["mb"] += 1
                rec = {"name": k, "mbid": a and a["id"], "mbName": a and a.get("name"), "v": MATCHER, "at": now()}
                _append(root / "mb_names.jsonl", rec)
                with looked:
                    names[k] = rec
                    run["names"] += 1
                    run["matched"] += bool(a)
                    state["done"] = i + 1
                    looked.notify()
        except BaseException as e:
            state["errors"].append(e)
        finally:
            with looked:
                state["done"] = len(order) + 1  # finished: the other thread stops waiting
                looked.notify()

    def wanted():
        asked = set()
        for i, k in enumerate(order):
            with looked:
                looked.wait_for(lambda: state["done"] > i)
                mbid = _mbid(names, k)
            if mbid and (mbid, alg) not in similar and mbid not in asked:
                asked.add(mbid)
                yield mbid

    def neighbours():
        try:
            mbids = itertools.islice(wanted(), budget) if budget else wanted()
            for mbid, sim in _windowed(mbids, neighbours_of, width):
                spent["lb"] += 1
                rec = {"mbid": mbid, "algorithm": alg, "similar": sim, "at": now()}
                similar[(mbid, alg)] = rec
                _append(root / "lb_similar.jsonl", rec)
                run["similar"] += 1
        except BaseException as e:
            state["errors"].append(e)

    threads = [threading.Thread(target=f, daemon=True) for f in (lookups, neighbours)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    if state["errors"]:
        raise state["errors"][0]
    return {"run": {k: v for k, v in run.items() if v}, "requests": spent["mb"] + spent["lb"],
            "cache": {"names": len(names), "matched": sum(1 for k in names if _mbid(names, k)),
                      "artistsWithSimilar": sum(1 for (_, a) in similar if a == alg)}}


MAP_FILE, SIMILAR_FILE = "map_artists.jsonl.gz", "similar_artists.jsonl.gz"


def _write_gz(path, rows):
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as raw:
        with gzip.open(raw, "wt", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        raw.flush()
        os.fsync(raw.fileno())
    tmp.replace(path)


def export(s, catalogue_path):
    """OctantVector's two input files from the caches, written under paths.artists (None when nothing was looked up
    yet). -> {"maps": versions with IDs, "artists": IDs with a similar-artist list, "folder"}."""
    root = Path(s["paths"]["artists"])
    if not (root / "mb_names.jsonl").exists():
        return None
    names = _load(root / "mb_names.jsonl", lambda r: r["name"])
    similar = _load(root / "lb_similar.jsonl", lambda r: (r["mbid"], r["algorithm"]))
    by_map, _ = catalogue_artists(catalogue_path)
    maps = []
    for h, raw in sorted(by_map.items()):
        ids = map_artist_ids(raw, names)
        if ids:
            maps.append({"hash": h, "mbids": ids})
    alg = ALGORITHM
    recs = sorted((r for (_, a), r in similar.items() if a == alg), key=lambda r: r["mbid"])
    fetched = max((r["at"] for r in recs), default=None)
    rows = [{"algorithm": alg, "fetchedAt": fetched}]
    rows += [{"mbid": r["mbid"], "similar": [[x["mbid"], x["score"]] for x in r["similar"]
                                            if x["mbid"] not in SPECIAL_MBIDS and x.get("score") is not None]}
             for r in recs if r["mbid"] not in SPECIAL_MBIDS]
    _write_gz(root / MAP_FILE, maps)
    _write_gz(root / SIMILAR_FILE, rows)
    return {"maps": len(maps), "artists": len(rows) - 1, "folder": str(root)}
