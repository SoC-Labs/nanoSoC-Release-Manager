"""A hermetic Artifactory OSS stand-in for the publish tests: a threaded HTTP server on 127.0.0.1 (so a job's child
process reaches it too), answering the calls the publish unit makes, in the shapes measured on the lab's OSS
7.161.17 by read-only calls on 2026-10-14:

- `GET api/storage/<repo>/<path>`: {repo, path, created, createdBy, size (a STRING), checksums {sha1, md5, sha256},
  originalChecksums, uri}; `?properties`: {properties: {key: [value]}, uri}, 404 for no properties as for no path;
- `HEAD <repo>/<path>`: 200 with X-Checksum-Md5/Sha1/Sha256 and Content-Length, or 404;
- `GET api/build/<name>/<number>`: {buildInfo: {...}, uri}; 404 `{"errors": [{"status": 404, "message": "No build was
  found for build name: ..."}]}`;
- deploy `PUT <repo>/<path>;k=v;...` (matrix parameters URL-decoded; X-Checksum-* headers checked, 409 on mismatch;
  an existing path is OVERWRITTEN, as on the real store: refusing that is the client's job), `PUT api/build`,
  `POST api/build/promote/<name>/<number>` (appends to buildInfo.statuses).

Auth: a Bearer token only. Basic auth anywhere is 401 and counted (on the real store, Basic on /access/api counts as a
failed admin login); /access/... always 401. Every request is recorded in `requests` (method, path, auth kind)."""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeArtifactory:
    def __init__(self, token: str = "test-token-not-a-secret"):
        self.token = token
        self.files: dict[str, dict] = {}          # "<repo>/<path>" -> {data, props, created}
        self.builds: dict[tuple[str, str], dict] = {}
        self.requests: list[tuple[str, str, str]] = []
        self.basic = 0
        self.overwrites = 0
        self.fail_put: set[str] = set()           # paths whose PUT answers 500 (a store that fails mid-publish)
        self.serve_wrong: set[str] = set()        # paths whose GET returns other bytes (a store that lies)
        self.store_wrong: set[str] = set()        # paths whose PUT passes the checksum check, then stores other bytes
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):            # quiet
                pass

            def _auth(self) -> str:
                h = self.headers.get("Authorization", "")
                if h.startswith("Basic "):
                    fake.basic += 1
                    return "basic"
                return "bearer" if h == "Bearer " + fake.token else ("bearer-wrong" if h.startswith("Bearer") else "none")

            def _send(self, code, body=b"", headers=None, ctype="application/json"):
                if isinstance(body, (dict, list)):
                    body = json.dumps(body, indent=2).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _handle(self):
                raw = self.path
                split = urllib.parse.urlsplit(raw)
                path, query = split.path, split.query
                auth = self._auth()
                with fake.lock:
                    fake.requests.append((self.command, raw, auth))
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                if path.startswith("/access/") or auth != "bearer":
                    return self._send(401, {"errors": [{"status": 401, "message": "Bad credentials"}]})
                if not path.startswith("/artifactory/"):
                    return self._send(404, {"errors": [{"status": 404, "message": "Not Found"}]})
                rel = path[len("/artifactory/"):]
                return fake.answer(self, self.command, rel, query, body)

            do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = do_PATCH = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:%d/artifactory" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # -- state helpers ---------------------------------------------------------------------------------------
    def seed(self, path: str, data: bytes, props: dict | None = None):
        self.files[path] = {"data": data, "props": {k: [v] for k, v in (props or {}).items()}, "created": "seeded"}

    def writes(self):
        return [(m, p) for m, p, _ in self.requests if m not in ("GET", "HEAD")]

    @staticmethod
    def _sums(data: bytes) -> dict:
        return {"sha1": hashlib.sha1(data).hexdigest(), "md5": hashlib.md5(data).hexdigest(),
                "sha256": hashlib.sha256(data).hexdigest()}

    # -- answers -------------------------------------------------------------------------------------------------
    def answer(self, h, method, rel, query, body):
        rel_path, _, matrix = rel.partition(";")
        rel_path = urllib.parse.unquote(rel_path)
        if rel_path.startswith("api/storage/") and method == "GET":
            p = rel_path[len("api/storage/"):]
            f = self.files.get(p)
            if "properties" in urllib.parse.parse_qs(query, keep_blank_values=True):
                if f is None or not f["props"]:
                    return h._send(404, {"errors": [{"status": 404, "message": "No properties could be found."}]})
                return h._send(200, {"properties": f["props"], "uri": "x"})
            if f is None:
                if any(k.startswith(p + "/") for k in self.files) or p.count("/") == 0:
                    kids = sorted({k[len(p) + 1:].split("/")[0] for k in self.files if k.startswith(p + "/")})
                    return h._send(200, {"repo": p.split("/")[0], "path": "/", "children": [
                        {"uri": "/" + k, "folder": False} for k in kids]})
                return h._send(404, {"errors": [{"status": 404, "message": "Unable to find item"}]})
            s = self._sums(f["data"])
            return h._send(200, {"repo": p.split("/")[0], "path": "/" + p.split("/", 1)[1], "created": f["created"],
                                 "createdBy": "token", "size": str(len(f["data"])), "checksums": s,
                                 "originalChecksums": {"sha256": s["sha256"]}, "uri": "x"})
        if rel_path.startswith("api/build/promote/") and method == "POST":
            name, number = rel_path[len("api/build/promote/"):].split("/", 1)
            b = self.builds.get((name, number))
            if b is None:
                return h._send(404, {"errors": [{"status": 404, "message": "Cannot find build"}]})
            req = json.loads(body or b"{}")
            b.setdefault("statuses", []).append({"status": req.get("status"), "comment": req.get("comment"),
                                                 "repository": None, "timestamp": req.get("timestamp"),
                                                 "user": "token", "ciUser": req.get("ciUser")})
            return h._send(200, {"messages": []})
        if rel_path == "api/build" and method == "PUT":
            doc = json.loads(body)
            self.builds[(doc["name"], doc["number"])] = doc
            return h._send(204)
        if rel_path.startswith("api/build/") and method == "GET":
            parts = rel_path.split("/")
            b = self.builds.get((parts[2], parts[3])) if len(parts) == 4 else None
            if b is None:
                return h._send(404, {"errors": [{"status": 404, "message": "No build was found for build name: %s, "
                                                 "build number: %s " % tuple((parts + ["", "", "", ""])[2:4])}]})
            return h._send(200, {"buildInfo": b, "uri": "x"})
        if rel_path.startswith("api/"):
            return h._send(405, {"errors": [{"status": 405, "message": "not in the fake"}]})
        if method in ("GET", "HEAD"):
            f = self.files.get(rel_path)
            if f is None:
                return h._send(404, {"errors": [{"status": 404, "message": "Not Found"}]})
            data = f["data"] if rel_path not in self.serve_wrong else f["data"] + b"tampered"
            s = self._sums(f["data"])
            return h._send(200, data, {"X-Checksum-Md5": s["md5"], "X-Checksum-Sha1": s["sha1"],
                                       "X-Checksum-Sha256": s["sha256"]}, ctype="application/octet-stream")
        if method == "PUT":
            if rel_path in self.fail_put:
                return h._send(500, {"errors": [{"status": 500, "message": "disk full"}]})
            s = self._sums(body)
            for alg in ("sha1", "md5", "sha256"):
                want = h.headers.get("X-Checksum-%s" % alg.capitalize())
                if want and want != s[alg]:
                    return h._send(409, {"errors": [{"status": 409, "message": "Checksum mismatch"}]})
            props = {}
            for kv in [x for x in matrix.split(";") if x]:
                k, _, v = kv.partition("=")
                props[urllib.parse.unquote(k)] = [urllib.parse.unquote(v)]
            if rel_path in self.files:
                self.overwrites += 1
            if rel_path in self.store_wrong:
                body = body + b"corrupted at rest"
            self.files[rel_path] = {"data": body, "props": props, "created": "now"}
            return h._send(201, {"repo": rel_path.split("/")[0], "path": "/" + rel_path.split("/", 1)[1],
                                 "size": str(len(body)), "checksums": s, "originalChecksums": s, "uri": "x"})
        return h._send(405, {"errors": [{"status": 405, "message": "Method Not Allowed"}]})


def basic_header(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()
