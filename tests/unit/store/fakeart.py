"""A hermetic fake Artifactory (OSS-like) for the store-index tests: in-process, no network.

    fa = FakeArtifactory(items=[...], builds={...}, files={...}, token="t0k")
    client = StoreClient("http://fake.invalid/artifactory", "t0k", transport=fa.transport())   # httpx MockTransport
    with fa.serve() as url: ...                                                               # a real local HTTP server

What it answers (paths relative to the API base `/artifactory/`):
    GET  api/system/ping, api/system/version
    POST api/search/aql            items.find with an optional repo filter; `.include("property")` gives properties
    GET  api/build, api/build/<name>, api/build/<name>/<number>[?started=]
    GET  api/storage/<repo>        404 when the repo is not in `repos`
    GET  <repo>/<path>             a file's bytes from `files`
Feature switches mimic a server without a feature: aql=False (400, as a Pro-only endpoint answers),
builds=False (400). Every request is logged; a Basic Authorization header is answered 401 and counted in
`basic_attempts` (on the real server it is a failed admin login). Any non-GET/HEAD other than the AQL POST is
counted in `writes` and answered 405, so a test can prove the client never sent one.
"""
from __future__ import annotations

import contextlib
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx

BASE = "/artifactory/"


def item(repo: str, path: str, name: str, *, size: int = 10, created: str = "2026-09-01T10:00:00.000Z",
         md5: str = "0" * 32, sha1: str | None = None, props: dict | None = None, created_by: str = "publisher"):
    return {"repo": repo, "path": path, "name": name, "size": size, "created": created, "created_by": created_by,
            "actual_md5": md5, "actual_sha1": sha1 or ("1" * 40), "type": "file",
            "properties": [{"key": k, "value": v} for k, v in (props or {}).items()]}


class FakeArtifactory:
    def __init__(self, *, items=(), builds=None, files=None, repos=None, token="t0k", aql=True, builds_api=True,
                 version="7.0.0", license="Artifactory OSS"):
        self.items = list(items)
        self.builds = builds or {}             # name -> [buildInfo dict, ...] (several may share a number)
        self.files = dict(files or {})         # "repo/path/name" -> bytes
        self.repos = set(repos if repos is not None else {i["repo"] for i in self.items})
        self.token, self.aql_on, self.builds_on = token, aql, builds_api
        self.version, self.license = version, license
        self.log: list[tuple[str, str]] = []
        self.basic_attempts = 0
        self.writes: list[tuple[str, str]] = []
        self.auth_seen: set[str] = set()
        self.down = False

    # --- the one answer function ------------------------------------------------------------------------------
    def answer(self, method: str, target: str, headers: dict, body: bytes) -> tuple[int, bytes, str]:
        u = urllib.parse.urlsplit(target)
        path = urllib.parse.unquote(u.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        self.log.append((method, path + (("?" + u.query) if u.query else "")))
        auth = headers.get("authorization") or ""
        self.auth_seen.add(auth.split(" ", 1)[0] if auth else "")
        if auth.lower().startswith("basic"):
            self.basic_attempts += 1
            return self._j(401, {"errors": [{"status": 401, "message": "Basic auth refused by the fake"}]})
        if path == BASE + "api/system/ping":                   # anonymous on the real server too
            return 200, b"OK", "text/plain"
        if auth != "Bearer %s" % self.token:
            return self._j(401, {"errors": [{"status": 401, "message": "Bad credentials"}]})
        if not path.startswith(BASE):
            return self._j(404, {"errors": [{"status": 404, "message": "Not Found"}]})
        rel = path[len(BASE):]
        if method == "POST" and rel == "api/search/aql":
            return self._aql(body.decode())
        if method not in ("GET", "HEAD"):
            self.writes.append((method, rel))
            return self._j(405, {"errors": [{"status": 405, "message": "Method not allowed"}]})
        if rel == "api/system/ping":
            return 200, b"OK", "text/plain"
        if rel == "api/system/version":
            return self._j(200, {"version": self.version, "license": self.license, "addons": []})
        if rel.startswith("api/build"):
            return self._build(rel, q)
        if rel.startswith("api/storage/"):
            repo = rel[len("api/storage/"):].split("/", 1)[0]
            if repo not in self.repos:
                return self._j(404, {"errors": [{"status": 404, "message": "Unable to find item"}]})
            return self._j(200, {"repo": repo, "path": "/", "children": []})
        if rel in self.files:
            return 200, self.files[rel], "application/octet-stream"
        return self._j(404, {"errors": [{"status": 404, "message": "File not found."}]})

    @staticmethod
    def _j(status: int, doc: Any) -> tuple[int, bytes, str]:
        return status, json.dumps(doc).encode(), "application/json"

    def _aql(self, q: str) -> tuple[int, bytes, str]:
        if not self.aql_on:
            return self._j(400, {"errors": [{"status": 400, "message": "This REST API is available only in Artifactory Pro"}]})
        m = re.match(r"^items\.find\((\{.*\})\)\.include\((.*)\)\s*$", q.strip(), re.S)
        if not m:
            return self._j(400, {"errors": [{"status": 400, "message": "Failed to parse query"}]})
        crit = json.loads(m.group(1))
        repos = {o["repo"] for o in crit.get("$or", [])} if "$or" in crit else None
        rows = []
        for it in self.items:
            if repos is not None and it["repo"] not in repos:
                continue
            r = {k: v for k, v in it.items() if k not in ("type", "properties")}
            if '"property"' in m.group(2) and it.get("properties"):
                r["properties"] = it["properties"]
            rows.append(r)
        return self._j(200, {"results": rows, "range": {"start_pos": 0, "end_pos": len(rows), "total": len(rows)}})

    def _build(self, rel: str, q: dict) -> tuple[int, bytes, str]:
        if not self.builds_on:
            return self._j(400, {"errors": [{"status": 400, "message": "This REST API is available only in Artifactory Pro"}]})
        parts = rel.split("/")[2:]
        if not parts or parts == [""]:
            if not self.builds:
                return self._j(404, {"errors": [{"status": 404, "message": "No builds were found"}]})
            return self._j(200, {"builds": [{"uri": "/" + n} for n in sorted(self.builds)]})
        name = parts[0]
        recs = self.builds.get(name)
        if recs is None:
            return self._j(404, {"errors": [{"status": 404, "message": "No build was found for build name: %s" % name}]})
        if len(parts) == 1:
            return self._j(200, {"buildsNumbers": [{"uri": "/" + b["number"], "started": b["started"]} for b in recs]})
        same = [b for b in recs if b["number"] == parts[1]]
        if q.get("started"):
            same = [b for b in same if b["started"] == q["started"]]
        if not same:
            return self._j(404, {"errors": [{"status": 404, "message": "No build was found"}]})
        return self._j(200, {"buildInfo": same[-1]})

    # --- adapters -----------------------------------------------------------------------------------------------
    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if self.down:
                raise httpx.ConnectError("fake is down", request=request)
            target = request.url.raw_path.decode()
            status, data, ctype = self.answer(request.method, target, {k.lower(): v for k, v in request.headers.items()},
                                              request.content)
            return httpx.Response(status, content=b"" if request.method == "HEAD" else data,
                                  headers={"content-type": ctype})
        return httpx.MockTransport(handle)

    @contextlib.contextmanager
    def serve(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                status, data, ctype = outer.answer(self.command, self.path,
                                                   {k.lower(): v for k, v in self.headers.items()}, body)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(data)

            do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_PATCH = _do

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            yield "http://127.0.0.1:%d/artifactory" % srv.server_address[1]
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(5)


# ---------------------------------------------------------------------------------------------------------------
# A small synthetic store: two projects, one with a build record and a release manifest, one with neither.
# ---------------------------------------------------------------------------------------------------------------
RAW_A = "a" * 32
RAW_B = "b" * 32
ZST_A = "c" * 32
COMMIT = "1234567890abcdef1234567890abcdef12345678"
BUILD_REV = "fedcba0987654321fedcba0987654321fedcba09"
DOC_BYTES = b"# Errata\n\nnone yet\n"


def sample(configuration: str = "demo-si", **kw) -> FakeArtifactory:
    import hashlib
    manifest = json.dumps({
        "schema": 1, "kind": "tapeout-record", "tapeout_id": "demo-tapeout-1", "project": "chipa", "version": "1.0.0",
        "chip": "demo", "silicon_revision": "r0p0", "configuration": configuration, "status": "accepted",
        "foundry": {"foundry": "foundry-x", "shuttle": "mpw-1"}, "submission_date": "2026-09-01",
        "foundry_verdict": {"verdict": "accepted"},
        "source": {"repo": {"host": "github", "path": "Demo-Org/Demo-Chip"}, "tags": ["v1.0.0"], "commit": COMMIT,
                   "pins": {"ip/demo-ip": "ab" * 20}},
        "gds": {"raw_md5": RAW_A, "layout_hash": None, "store_path": "asic-candidate/chipa/blk/run1/stream/a.gds.zst",
                "stored_sha256": None}}, sort_keys=True).encode()
    doc_sha1 = hashlib.sha1(DOC_BYTES).hexdigest()
    man_sha1 = hashlib.sha1(manifest).hexdigest()
    items = [
        item("asic-candidate", "chipa/blk/run1/stream", "a.gds.zst", size=100, md5=ZST_A,
             props={"pnr.raw_md5": RAW_A, "run.tag": "run1", "build.name": "chipa-blk", "build.number": "run1"}),
        item("asic-candidate", "chipb/blk/run9/stream", "b.gds.zst", size=90, md5="d" * 32,
             props={"submitted.raw_md5": RAW_B, "submitted.ships": "yes", "release.commit": "9" * 40,
                    "release.tag": "chipb-rc"}),
        item("asic-record", "chipa/blk/run1/evidence", "evidence_report.json", props={"pnr.raw_md5": RAW_A}),
        item("asic-record", "chipa/blk/run1/source", "build_soc.tar.gz", props={"chipa.commit": "abcdef1"}),
        item("asic-record", "chipa/blk/_unbound/foundry/Archive_X", "report.rpt"),
        item("asic-record", "chipa/blk/_unbound/foundry/Archive_X/sub", "drc.rpt"),
        item("asic-record", "chipa/blk/run1/docs", "ERRATA.md", size=len(DOC_BYTES), sha1=doc_sha1),
        item("asic-record", "chipa/blk/run1/docs", "secret_manual.pdf", size=50,
             props={"confidentiality": "VENDOR_CONFIDENTIAL"}),
        item("asic-record", "chipa/blk/run1/recipe", "recipe.tar.gz"),
        item("asic-release", "chipa/1.0.0", "tapeout-demo.json", size=len(manifest), sha1=man_sha1),
        item("asic-release", "chipa/1.0.0", "tapeout-demo.json.sig"),
    ]
    builds = {"chipa-blk": [
        {"name": "chipa-blk", "number": "run1", "started": "2026-08-30T10:00:00.000+0000", "agent": {"name": "pub"},
         "vcs": [{"revision": BUILD_REV, "branch": "main", "url": "git@github.com:Demo-Org/Demo-Chip.git"}],
         "properties": {"vcs.worktree_dirty": "yes", "pnr.raw_md5": RAW_A},
         "modules": [{"id": "run1", "artifacts": [{"md5": ZST_A, "name": "a.gds.zst"}]}]},
        {"name": "chipa-blk", "number": "run1", "started": "2026-08-30T11:00:00.000+0000", "agent": {"name": "pub"},
         "vcs": [{"revision": COMMIT, "branch": "main", "url": "git@github.com:Demo-Org/Demo-Chip.git"}],
         "properties": {"vcs.worktree_dirty": "no", "pnr.raw_md5": RAW_A}, "modules": []},
    ]}
    files = {"asic-release/chipa/1.0.0/tapeout-demo.json": manifest,
             "asic-record/chipa/blk/run1/docs/ERRATA.md": DOC_BYTES}
    kw.setdefault("repos", {"asic-candidate", "asic-record", "asic-release"})
    return FakeArtifactory(items=items, builds=builds, files=files, **kw)
