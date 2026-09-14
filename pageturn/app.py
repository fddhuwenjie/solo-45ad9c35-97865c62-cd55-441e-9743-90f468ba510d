"""WSGI application (wsgiref) exposing the page-turn review API."""
from __future__ import annotations

import json
import mimetypes
import urllib.parse
from wsgiref.simple_server import make_server

from . import db as store
from .engine import (
    confirmed_bundle, evaluate_pagination, public_report, run_analysis,
    source_digest,
)
from .parser import parse_musicxml
from .svg import render_svg


DEFAULT_DB = "pageturn.db"


class App:
    def __init__(self, db_path: str = DEFAULT_DB):
        self.db_path = db_path
        self.conn = store.connect(db_path)

    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = urllib.parse.unquote(environ.get("PATH_INFO", "/")).rstrip("/") or "/"
        try:
            return self.route(method, path, environ, start_response)
        except HTTPError as e:
            return e.emit(start_response)
        except ValueError as e:
            return http_json(start_response, 400, {"error": "BAD_REQUEST",
                                                 "message": str(e)})
        except Exception as e:  # pragma: no cover - defensive
            return http_json(start_response, 500, {"error": "INTERNAL",
                                                  "message": str(e)})

    # -- routing -------------------------------------------------------------

    def route(self, method, path, environ, start_response):
        m, body_json = self._read(environ)
        routes = [
            ("GET", "/health", self.h_health),
            ("POST", "/scores", self.h_create_score),
            ("GET", "/scores", self.h_list_scores),
            ("GET", "/scores/{}", self.h_get_score),
            ("POST", "/scores/{}/analysis", self.h_analyze),
            ("GET", "/scores/{}/analysis", self.h_latest_analysis),
            ("GET", "/analyses/{}", self.h_get_analysis),
            ("POST", "/scores/{}/revisions", self.h_create_revision),
            ("GET", "/scores/{}/revisions", self.h_list_revisions),
            ("GET", "/revisions/{}", self.h_get_revision),
            ("POST", "/revisions/{}/confirm", self.h_confirm),
            ("GET", "/confirmations/{}", self.h_get_confirmation),
        ]
        for rm, pattern, handler in routes:
            if rm != method:
                continue
            args = match(pattern, path)
            if args is not None:
                return handler(environ, body_json, *args, sr=start_response)
        raise HTTPError(404, "NOT_FOUND", f"无此端点: {method} {path}")

    def _read(self, environ):
        method = environ["REQUEST_METHOD"]
        try:
            length = int(environ.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        raw = environ["wsgi.input"].read(length) if length else b""
        ctype = environ.get("CONTENT_TYPE", "")
        body_json = None
        if raw:
            if "application/json" in ctype:
                body_json = json.loads(raw.decode("utf-8"))
            else:
                body_json = raw.decode("utf-8")
        return method, body_json

    # -- health --------------------------------------------------------------

    def h_health(self, env, body, sr):
        return http_json(sr, 200, {"status": "ok"})

    # -- scores --------------------------------------------------------------

    def h_create_score(self, env, body, sr):
        if isinstance(body, str):
            musicxml, title, metadata = body, "uploaded.musicxml", {}
        elif isinstance(body, dict):
            musicxml = body.get("musicxml") or body.get("musicXml") or ""
            title = body.get("title", "未命名分谱")
            metadata = body.get("metadata", {})
        else:
            raise HTTPError(400, "BAD_REQUEST", "需要 musicxml 与 metadata")
        if not musicxml.strip():
            raise HTTPError(400, "BAD_REQUEST", "musicxml 为空")
        parse_musicxml(musicxml)  # fail fast on unreadable scores
        digest = source_digest(musicxml)
        sid = store.create_score(self.conn, title, musicxml, metadata, digest)
        return http_json(sr, 201, {"scoreId": sid, "title": title,
                                   "sourceDigest": digest})

    def h_list_scores(self, env, body, sr):
        rows = store.list_scores(self.conn)
        return http_json(sr, 200, {"scores": [dict(r) for r in rows]})

    def _score(self, sid: str):
        try:
            row = store.get_score(self.conn, int(sid))
        except ValueError:
            row = None
        if row is None:
            raise HTTPError(404, "NOT_FOUND", f"乐谱 {sid} 不存在")
        return row

    def h_get_score(self, env, body, sid, sr):
        row = self._score(sid)
        return http_json(sr, 200, {
            "scoreId": row["id"], "title": row["title"],
            "sourceDigest": row["digest"],
            "metadata": json.loads(row["metadata_json"]),
            "createdAt": row["created_at"],
        })

    # -- analysis ------------------------------------------------------------

    def _do_analysis(self, sid: str, metadata_override=None):
        row = self._score(sid)
        metadata = json.loads(row["metadata_json"])
        if metadata_override:
            metadata = _deep_merge(metadata, metadata_override)
        full = run_analysis(row["musicxml"], metadata)
        report = public_report(full)
        aid = store.create_analysis(self.conn, int(sid), report)
        self._cache_full(int(sid), aid, full)
        return aid, report, full

    def _cache_full(self, sid, aid, full):
        self.conn.__dict__.setdefault("_full_cache", {})[(sid, aid)] = full

    def h_analyze(self, env, body, sid, sr):
        override = body.get("metadata") if isinstance(body, dict) else None
        aid, report, _ = self._do_analysis(sid, override)
        return http_json(sr, 201, {"analysisId": aid, "report": report})

    def h_latest_analysis(self, env, body, sid, sr):
        self._score(sid)
        row = store.latest_analysis(self.conn, int(sid))
        if row is None:
            raise HTTPError(404, "NOT_FOUND", "该乐谱尚未分析")
        return http_json(sr, 200, {"analysisId": row["id"],
                                   "report": json.loads(row["report_json"])})

    def h_get_analysis(self, env, body, aid, sr):
        row = store.get_analysis(self.conn, int(aid))
        if row is None:
            raise HTTPError(404, "NOT_FOUND", f"分析 {aid} 不存在")
        return http_json(sr, 200, {"analysisId": row["id"],
                                   "scoreId": row["score_id"],
                                   "report": json.loads(row["report_json"])})

    # -- revisions -----------------------------------------------------------

    def h_create_revision(self, env, body, sid, sr):
        row = self._score(sid)
        if not isinstance(body, dict) or "pagination" not in body:
            raise HTTPError(400, "BAD_REQUEST", "需要 pagination {partId:[小节...]}")
        full = self._load_full(int(sid), body.get("analysisId"))
        pagination = _normalize_input_pagination(body["pagination"], full)
        recomputed = evaluate_pagination(full, pagination)
        note = body.get("note", "")
        aid = body.get("analysisId")
        rid = store.create_revision(self.conn, int(sid), pagination, note, aid,
                                    recomputed)
        return http_json(sr, 201, {"revisionId": rid, "report": recomputed})

    def h_list_revisions(self, env, body, sid, sr):
        self._score(sid)
        rows = store.list_revisions(self.conn, int(sid))
        return http_json(sr, 200, {"revisions": [dict(r) for r in rows]})

    def h_get_revision(self, env, body, rid, sr):
        row = store.get_revision(self.conn, int(rid))
        if row is None:
            raise HTTPError(404, "NOT_FOUND", f"修订 {rid} 不存在")
        out = {
            "revisionId": row["id"], "scoreId": row["score_id"],
            "analysisId": row["analysis_id"], "note": row["note"],
            "pagination": json.loads(row["pagination_json"]),
            "createdAt": row["created_at"],
        }
        if env.get("HTTP_ACCEPT", "").startswith("image/svg"):
            return self._revision_svg(row, sr)
        if row["report_json"]:
            out["report"] = json.loads(row["report_json"])
        return http_json(sr, 200, out)

    def _revision_svg(self, row, sr):
        score = self._score(str(row["score_id"]))
        full = run_analysis(score["musicxml"], json.loads(score["metadata_json"]))
        internal = full["_internal"]
        pagination = json.loads(row["pagination_json"])
        svgs = {}
        proposed = {}
        for part in internal["parts"]:
            pag = pagination.get(part.id, {"breaks": [], "locked": []})
            pr_data = evaluate_pagination(full, {part.id: pag})["parts"][0]
            from .model import PartReport, PageReview, Crossing
            pr = _hydrate_report(part.id, pr_data)
            baseline = internal["timing"]  # not needed for render
            svgs[part.id] = render_svg(part.id, pr, pag["breaks"], proposed)
        if len(svgs) == 1:
            data = next(iter(svgs.values())).encode("utf-8")
        else:
            data = ("<svg xmlns='http://www.w3.org/2000/svg'>" +
                    "".join(f"<g>{s[s.index('<svg'):]}" for s in svgs.values())
                    ).encode("utf-8")
        sr("200 OK", [("Content-Type", "image/svg+xml; charset=utf-8")])
        return [data]

    # -- confirmations -------------------------------------------------------

    def h_confirm(self, env, body, rid, sr):
        rev = store.get_revision(self.conn, int(rid))
        if rev is None:
            raise HTTPError(404, "NOT_FOUND", f"修订 {rid} 不存在")
        score = store.get_score(self.conn, rev["score_id"])
        full = run_analysis(score["musicxml"], json.loads(score["metadata_json"]))
        pagination = json.loads(rev["pagination_json"])
        bundle = confirmed_bundle(full, pagination, score["digest"])
        if not bundle["feasible"]:
            raise HTTPError(422, "UNPLAYABLE",
                            "存在不可演奏断点，不能固定为确认版",
                            {"recompute": bundle})
        cid = store.create_confirmation(
            self.conn, score["id"], int(rid), score["digest"],
            {"route": bundle["route"]}, pagination, bundle)
        return http_json(sr, 201, {"confirmationId": cid, "recompute": bundle})

    def h_get_confirmation(self, env, body, cid, sr):
        row = store.get_confirmation(self.conn, int(cid))
        if row is None:
            raise HTTPError(404, "NOT_FOUND", f"确认 {cid} 不存在")
        accept = env.get("HTTP_ACCEPT", "")
        bundle = json.loads(row["report_json"])
        if "application/json" in accept or "text/html" not in accept and "svg" not in accept:
            return http_json(sr, 200, {
                "confirmationId": row["id"],
                "sourceDigest": row["digest"],
                "route": json.loads(row["route_json"]),
                "pagination": json.loads(row["pagination_json"]),
                "recompute": bundle,
                "createdAt": row["created_at"],
            })
        sr("200 OK", [("Content-Type", "application/json")])
        return [json.dumps(bundle, ensure_ascii=False).encode("utf-8")]

    # -- helpers -------------------------------------------------------------

    def _load_full(self, sid: int, analysis_id):
        if analysis_id is not None:
            cached = self.conn.__dict__.get("_full_cache", {}).get((sid, int(analysis_id)))
            if cached is not None:
                return cached
            row = store.get_analysis(self.conn, int(analysis_id))
            if row is None or row["score_id"] != sid:
                raise HTTPError(404, "NOT_FOUND", f"分析 {analysis_id} 不属于该乐谱")
        score = store.get_score(self.conn, sid)
        return run_analysis(score["musicxml"], json.loads(score["metadata_json"]))


# ---------------------------------------------------------------------------

class HTTPError(Exception):
    def __init__(self, status: int, code: str, message: str, extra=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra or {}

    def emit(self, start_response):
        payload = {"error": self.code, "message": self.message, **self.extra}
        return http_json(start_response, self.status, payload)


def http_json(start_response, status: int, payload) -> list[bytes]:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    start_response(f"{status} {_reason(status)}", [
        ("Content-Type", "application/json; charset=utf-8"),
        ("Content-Length", str(len(body))),
    ])
    return [body]


def _reason(status: int) -> str:
    return {200: "OK", 201: "Created", 400: "Bad Request", 404: "Not Found",
            409: "Conflict", 422: "Unprocessable Entity",
            500: "Internal Server Error"}.get(status, "OK")


def match(pattern: str, path: str):
    pp, qp = pattern.strip("/").split("/"), path.strip("/").split("/")
    if len(pp) != len(qp):
        return None
    args = []
    for a, b in zip(pp, qp):
        if a == "{}":
            args.append(b)
        elif a != b:
            return None
    return args


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _normalize_input_pagination(pagination, full) -> dict:
    parts = full["_internal"]["parts"]
    ids = {p.id for p in parts}
    out: dict[str, dict] = {}
    if isinstance(pagination, dict) and "breaks" in pagination:
        pagination = {p.id: pagination for p in parts}
    for pid, entry in (pagination or {}).items():
        if pid not in ids:
            raise HTTPError(400, "BAD_REQUEST", f"未知声部 {pid}")
        if isinstance(entry, list):
            entry = {"breaks": entry}
        out[pid] = {
            "breaks": [str(x) for x in entry.get("breaks", [])],
            "locked": [str(x) for x in entry.get("locked", [])],
        }
    return out


def _hydrate_report(part_id: str, data: dict):
    from .model import PartReport, PageReview, Crossing
    pages = []
    for p in data["pages"]:
        crs = [Crossing(pass_no=c["pass"], measure=c["measure"],
                        occurrence=c["occurrence"],
                        gap_seconds=c["gapSeconds"], feasible=c["feasible"],
                        reason=c["reason"], after_visit=0, before_visit=0,
                        window_seconds=c["windowSeconds"])
               for c in p["crossings"]]
        pages.append(PageReview(page=p["page"], break_after=p["breakAfter"],
                                locked=p["locked"], crossings=crs,
                                feasible=p["feasible"], reason=p["reason"],
                                gap_seconds=p["gapSeconds"],
                                move_to=p["moveTo"],
                                move_candidates=p["moveCandidates"]))
    return PartReport(part_id=part_id, name=data["name"], kind=data["kind"],
                      pages=pages, feasible=data["feasible"])


def main(host: str = "127.0.0.1", port: int = 8080, db_path: str = DEFAULT_DB):
    app = App(db_path)
    httpd = make_server(host, port, app)
    print(f"Page-turn review API on http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    main()
