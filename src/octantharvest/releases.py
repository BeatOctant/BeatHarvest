"""The daily run, approval and publishing (OctantSpecs constitution.md, principle 4b; contracts/, contract 1 (OctantHarvest to OctantVector) and contract 5 (OctantHarvest to OctantCompass)).

OctantVector does the computing (its contract 1 generation functions); OctantHarvest fetches the models it reads,
decides when, records what was written and what the maintainer approved, and puts the files to upload in one folder:

  <models>/               the model files of the settings' views (hf/<repo>/<revision>/) and models.json, their hashes
  <releases>/state.json   every version OctantVector wrote: its full, what it publishes (diff or full), approval
  <releases>/daily.jsonl  one JSON line per daily run: crawl, models, compute counts, drift, release, or the error
  <publish>/              the split files and index.json of the approved versions (uploading is done by hand)
  <publish>/handoff/      the full of an approved version as one tar, to hand over without an index (`handoff`)

A call OctantVector could not do as asked (its GenerationError) stops the command with OctantVector's message, without a
traceback; the daily run records it as the run's error first.

Nothing is published without approval: the index holds only versions approved with `octantharvest approve`, which
signs the version with the maintainer's key (settings signing.key; OctantSpecs contracts/distribution.md, 署名).
"""
import base64
import binascii
import contextlib
import hashlib
import json
import os
import sys
import traceback
from importlib.metadata import version as installed
from pathlib import Path

from . import crawl, now

# the contract 1 generation API this OctantHarvest uses (OctantSpecs contracts/engine-generation.md, 版の付け方): the same
# major version, and at least the minor version that has every function called here
GENERATION_API = (1, 1)


def _bv():
    """OctantVector's generation functions, once their API version is one this OctantHarvest can use (OctantSpecs
    contracts/README.md, 契約の版と食い違いの知らせ方)."""
    import octantvector.generation as bv  # imported late: the settings and crawl commands work without it
    version = getattr(bv, "API_VERSION", None)
    try:
        major, minor = (int(x) for x in version.split("."))
    except (AttributeError, ValueError):
        major = minor = None
    if major != GENERATION_API[0] or minor < GENERATION_API[1]:
        need = ".".join(map(str, GENERATION_API))
        raise SystemExit(f"engine_incompatible: OctantVector {installed('octantvector')} has generation API {version}, this "
                         f"OctantHarvest needs {GENERATION_API[0]}.x from {need}; install the matching tag (README.md)")
    return bv


@contextlib.contextmanager
def _generation():
    """OctantVector's generation functions (_bv); a call they could not do as asked stops with their message."""
    bv = _bv()
    try:
        yield bv
    except bv.GenerationError as ex:
        raise SystemExit(f"OctantVector: {ex}") from None


def _views(s):
    """The settings' views: there is no default list, the DB's maker names them."""
    if not s["views"]:
        raise SystemExit("set `views` in the settings: the OctantVector views (id or id@version) to compute and put in "
                         "the DB (OctantSpecs octantharvest/config.md)")
    return s["views"]


def _sha256(path):
    with open(path, "rb") as f:
        return "sha256:" + hashlib.file_digest(f, "sha256").hexdigest()


def _hf_download(repo, revision, name, folder):
    from huggingface_hub import hf_hub_download  # imported late: only `models` talks to Hugging Face
    hf_hub_download(repo, name, revision=revision, local_dir=folder)


def models(s, log=sys.stderr, download=_hf_download):
    """Fetch the model files the settings' views read (OctantVector's `models(views)`) into paths.models, which
    OctantVector reads them from (models_dir) and never downloads itself. <models>/models.json records each file's
    sha256: a recorded file that is there with that hash is not fetched again, and one whose hash differs (changed on
    disk, or fetched again with other content) or that is there unrecorded stops the run; it is never replaced
    silently.
    -> {"<repo>@<revision>": "present" | "fetched"}."""
    with _generation() as bv:
        wanted = bv.models(_views(s))
    root = Path(s["paths"]["models"])
    rec_path = root / "models.json"
    record = json.loads(rec_path.read_text()) if rec_path.exists() else {}
    out = {}
    for m in wanted:
        key, folder = f"{m['repo']}@{m['revision']}", root / m["path"]
        entry = record.setdefault(key, {"files": {}, "fetchedAt": None})
        out[key] = "present"
        for name in m["files"]:
            p, known = folder / name, entry["files"].get(name)
            if p.is_file():
                if known is None:  # copied in by hand, or the record was lost: nobody checked its content
                    raise SystemExit(f"{p} is not recorded in {rec_path}; it is never replaced silently: "
                                     "remove it to fetch it again")
                if _sha256(p) != known:
                    raise SystemExit(f"{p} does not match its hash in {rec_path}; it is never replaced silently: "
                                     "remove it to fetch it again")
                continue
            print(f"models: {key} {name}", file=log)
            download(m["repo"], m["revision"], name, folder)
            got = _sha256(p)
            if known and got != known:
                raise SystemExit(f"{key} {name} was fetched again with another hash than {rec_path} records")
            entry["files"][name], entry["fetchedAt"], out[key] = got, now(), "fetched"
            _write_json(rec_path, record)  # after each file: an interrupted run keeps what it fetched
    return out


def check(s):
    """Whether each of the settings' views can be computed, without computing or fetching anything: OctantVector has an
    encoder for it, and paths.models holds the model files it reads as `models` recorded them (their hashes are
    checked by `models`, which daily and first-release run first).
    -> {view: "ready" | "models missing (`octantharvest models`): <repo>@<revision>/<file>, ..." | "<why not>"}, or
    {"octantvector": "<why not>"} when OctantVector cannot be used."""
    if not s["views"]:
        return {}
    try:
        bv = _bv()
    except (ImportError, SystemExit) as ex:
        return {"octantvector": str(ex)}
    root = Path(s["paths"]["models"])
    rec_path = root / "models.json"
    record = json.loads(rec_path.read_text()) if rec_path.exists() else {}
    out = {}
    for view in s["views"]:
        try:
            wanted = bv.models([view])
        except bv.GenerationError as ex:
            out[view] = str(ex)
            continue
        missing = [f"{m['repo']}@{m['revision']}/{name}" for m in wanted for name in m["files"]
                   if not (root / m["path"] / name).is_file()
                   or name not in record.get(f"{m['repo']}@{m['revision']}", {}).get("files", {})]
        out[view] = f"models missing (`octantharvest models`): {', '.join(missing)}" if missing else "ready"
    return out


def _write_json(path, obj):
    """Write through a temporary file, flushed to disk before the swap, so an interrupted write or a power cut leaves
    the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        f.write(json.dumps(obj, indent=1))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def _state_path(s):
    return Path(s["paths"]["releases"]) / "state.json"


def state(s):
    p = _state_path(s)
    return json.loads(p.read_text()) if p.exists() else {"versions": {}}


def _save(s, st):
    _write_json(_state_path(s), st)


def _record(s, r, why):
    st = state(s)
    st["versions"][r["dbVersion"]] = {"full": str(r["full"]), "published": str(r["published"]),
                                      "kind": "diff" if r["diff"] else "full", "writtenAt": now(),
                                      "why": why, "approvedAt": None}
    _save(s, st)


def base(s):
    """Folder of the newest full version written (None before the first release)."""
    vs = list(state(s)["versions"].values())  # in the order they were written
    return Path(vs[-1]["full"]) if vs else None


def _input(s, catalogue_path, log=sys.stderr):
    """The crawl for OctantVector, with the artist files exported fresh from the caches when there are any (the
    relation view `listenbrainz_artists`)."""
    inp = {"archive": s["paths"]["archive"], "catalogue": catalogue_path}
    out = crawl.artists.export(s, catalogue_path)
    if out is not None:
        print(f"artists: {out['maps']} map versions with IDs, {out['artists']} IDs with similar artists", file=log)
        inp["artists"] = out["folder"]
    return inp


def _notice(s):
    """The DB's terms and credits (the file the setting `notice` names) for the manifest; None keeps the previous
    version's. Each view's credits come from its encoder."""
    return Path(s["notice"]).read_text(encoding="utf-8") if s["notice"] else None


def _compute(s, bv, inp, log):
    """Compute what the store lacks over the distribution pool (the views' models are fetched before: `models`)."""
    return bv.compute(inp, s["paths"]["store"], _views(s), s["octantvector"], models_dir=s["paths"]["models"], log=log)


def _complete(counts, again):
    """Stop before a version is written while maps the GPU had no room for are left uncomputed: the version would lack
    them. OctantVector does not record them, so running `again` computes them."""
    if counts["deferred"]:
        views = ", ".join(f"{v} {c['deferred']}" for v, c in counts["views"].items() if c.get("deferred"))
        raise SystemExit(f"no version written: the GPU had no room for {counts['deferred']} maps ({views}); free GPU "
                         f"memory (e.g. close the VR server) and run `{again}` to compute them and write it")


def first(s, built_at=None, log=sys.stderr):
    """The first version: compute and calibrate the settings' views over the distribution pool and write a full."""
    with _generation() as bv:
        views = _views(s)
        if base(s) is not None:
            raise SystemExit("there is already a release; the daily run makes the next ones")
        models(s, log)  # before anything long: a view this OctantVector cannot compute stops here
        inp, p = _input(s, crawl.latest_catalogue(s), log), s["octantvector"]
        _complete(_compute(s, bv, inp, log), "octantharvest first-release")
        cal = bv.calibrate(s["paths"]["store"], views, p)
        r = bv.release(inp, s["paths"]["store"], views, cal, None, s["paths"]["releases"], p, built_at=built_at,
                       log=log, presets=s["presets"], notice=_notice(s))
    _record(s, r, "first release")
    return r


def daily(s, fetch=True, built_at=None, log=sys.stderr):
    """models -> crawl (unless fetch is False) -> compute -> drift -> (calibrate) -> release; one line in daily.jsonl.
    -> the log record. A run that fails still writes its line, with the error, before stopping; so does one that leaves
    maps the GPU had no room for (no version is written until they are computed)."""
    rec = {"at": now()}
    try:
        with _generation() as bv:  # before crawling: a OctantVector that does not match stops the run at once
            _daily(s, bv, rec, fetch, built_at, log)
        return rec
    except BaseException as ex:  # SystemExit included: the reason goes to the log
        rec["error"] = "".join(traceback.format_exception_only(type(ex), ex)).strip()
        raise
    finally:
        path = Path(s["paths"]["releases"]) / "daily.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")


def _daily(s, bv, rec, fetch, built_at, log):
    views = _views(s)
    # before crawling: a view no installed encoder computes (a plugin left out) or a model that cannot be fetched
    # stops the run at once, not after the crawl
    rec["models"] = models(s, log)
    if fetch:
        cat = crawl.catalogue(s, log=log)
        rec["crawl"] = {"catalogue": str(cat), "archive": crawl.download(s, cat, log=log)}
    else:
        cat = crawl.latest_catalogue(s)
        rec["crawl"] = {"catalogue": str(cat), "archive": "not run"}
    store, p = s["paths"]["store"], s["octantvector"]
    inp = _input(s, cat, log)
    rec["compute"] = _compute(s, bv, inp, log)
    _complete(rec["compute"], "octantharvest daily --no-crawl")
    b = base(s)
    if b is None:
        rec["release"] = "skipped: no first release yet (run `octantharvest first-release` once)"
        return
    rec["drift"] = bv.drift(store, b, p)
    over = {k: d for k, d in rec["drift"].items() if d > s["recalibrateDrift"]}
    cal = None
    if over:
        cal = bv.calibrate(store, views, p)
        rec["recalibrated"] = {"id": cal["id"], "because": over}
    r = bv.release(inp, store, views, cal, b, s["paths"]["releases"], p, full=bool(over), built_at=built_at,
                   log=log, presets=s["presets"], notice=_notice(s))
    why = f"drift over {s['recalibrateDrift']}: {', '.join(over)}" if over else "daily"
    _record(s, r, why)
    rec["release"] = {"version": r["dbVersion"], "published_as": "diff" if r["diff"] else "full"}


def _key(s):
    k = s["signing"]["key"]
    if not k:
        raise SystemExit("no signing key: set signing.key in the settings (`octantharvest keygen` makes one)")
    return k


def keygen(s):
    """A new signing key at signing.key (never overwritten). -> {"publicKey", "keyId"}: signing.publicKey takes the
    public key, so that publish indexes what it signs; the receivers trust it (OctantCompass's trusted_keys, or
    OctantVector's bundled list by a OctantVector release)."""
    k = Path(_key(s))
    if k.exists():
        raise SystemExit(f"{k} already exists; a key is never overwritten")
    k.parent.mkdir(parents=True, exist_ok=True)
    with _generation() as bv:
        return bv.keygen(k)


def approve(s, version):
    """Approve and sign what `version` publishes (its diff or its full)."""
    st = state(s)
    if version not in st["versions"]:
        raise SystemExit(f"no version {version}; written: {', '.join(sorted(st['versions'])) or 'none'}")
    if not (s["signing"]["allowUnsigned"] and not s["signing"]["key"]):
        with _generation() as bv:
            bv.sign(st["versions"][version]["published"], _key(s))
    st["versions"][version]["approvedAt"] = now()
    _save(s, st)
    return st["versions"][version]


def handoff(s, version):
    """The full of an approved `version` as one tar in <publish>/handoff, to hand to an invited player without an
    index (OctantSpecs contracts/distribution.md, 非公開（探求中）の扱い). The receiver needs the full, so a version
    published as a diff has its full signed here as well. -> path of the tar."""
    st = state(s)
    if version not in st["versions"]:
        raise SystemExit(f"no version {version}; written: {', '.join(sorted(st['versions'])) or 'none'}")
    v = st["versions"][version]
    if not v["approvedAt"]:
        raise SystemExit(f"{version} is not approved yet (`octantharvest approve {version}`)")
    with _generation() as bv:
        if Path(v["full"]) != Path(v["published"]) and not (s["signing"]["allowUnsigned"] and not s["signing"]["key"]):
            bv.sign(v["full"], _key(s))
        return bv.pack(v["full"], Path(s["paths"]["publish"]) / "handoff")


def _trusted(s):
    """{keyId: public key} of signing.publicKey, the author's key, which OctantVector may not bundle ({}: unset).
    keyId: the first 16 hex of the SHA-256 of the 32-byte key (OctantSpecs contracts/distribution.md, 署名)."""
    pub = s["signing"]["publicKey"]
    if not pub:
        return {}
    try:
        raw = base64.b64decode(pub, validate=True)
    except binascii.Error:
        raise SystemExit("signing.publicKey is not base64: set it to the publicKey `octantharvest keygen` "
                         "printed") from None
    # The same sum as OctantVector's common/signing.py key_id, written here because OctantHarvest imports only
    # octantvector.generation (OctantSpecs contracts/engine-generation.md, 契約1 生成側)
    return {hashlib.sha256(raw).hexdigest()[:16]: pub}


def publish(s):
    """The contract 5 index and the split files of the approved versions in <publish>. OctantVector indexes a version
    only when it is signed by a key it bundles or by signing.publicKey. -> path of index.json."""
    approved = [v["published"] for v in state(s)["versions"].values() if v["approvedAt"]]
    if not approved:
        raise SystemExit("nothing is approved yet (`octantharvest approve <version>`)")
    with _generation() as bv:
        return bv.build_index(approved, s["paths"]["publish"],
                              {"part_size_mb": s["publish"]["partSizeMb"], "publish_url": s["publish"]["url"],
                               "trusted_keys": _trusted(s), "allow_unsigned": bool(s["signing"]["allowUnsigned"])})
