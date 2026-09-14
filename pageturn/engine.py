"""Orchestration: parse -> roadmap -> timing -> pagination -> reports."""
from __future__ import annotations

import hashlib

from . import analyzer
from .metadata import normalize_metadata
from .model import Issue
from .parser import get_marks, parse_musicxml
from .roadmap import expand


def source_digest(musicxml: str) -> str:
    return hashlib.sha256(musicxml.encode("utf-8")).hexdigest()


def run_analysis(musicxml: str, metadata: dict | None = None) -> dict:
    parts, bars = parse_musicxml(musicxml)
    part_ids = [p.id for p in parts]
    meta = normalize_metadata(metadata, part_ids)

    # metadata tempo supplements (measure number -> event)
    for item in meta["tempos"]:
        mnum, evt = item["measure"], item["event"]
        for b in bars:
            if b.number == mnum and evt not in b.tempo_events:
                b.tempo_events.append(evt)

    rm = expand(bars)

    # repeated Segno/Coda marks are structural ambiguity: attach part, pass
    # and measure at every D.S./al Coda directive for every affected part
    ds_issues = _segno_ambiguity_issues(parts, bars, rm)

    # pagination: explicit metadata overrides, otherwise <print new-page>.
    # MusicXML semantics: new-page="yes" on measure N starts a page AT N, so
    # the player turns after the preceding measure N-1.
    pagination: dict[str, dict] = {}
    for part in parts:
        explicit = meta["pagination"].get(part.id, {})
        if explicit.get("breaks"):
            breaks = explicit["breaks"]
        else:
            breaks = []
            for pm in part.measures:
                if pm.print_new_page and pm.index > 0:
                    breaks.append(part.measures[pm.index - 1].number)
        locked = explicit.get("locked", [])
        pagination[part.id] = {"breaks": breaks, "locked": locked}

    part_reports = []
    issues = list(rm.issues) + ds_issues
    timing_data: dict[str, dict] = {}
    for part in parts:
        cfg = meta["parts"][part.id]
        pr, part_issues, data = analyzer.analyze_part(part, bars, rm, cfg)
        pag = pagination[part.id]
        pr = analyzer.evaluate_pages(part, bars, rm, cfg, data,
                                     pag["breaks"], pag["locked"])
        part_reports.append(pr)
        issues.extend(part_issues)
        timing_data[part.id] = data

    return {
        "roadmapOk": rm.ok,
        "terminated": rm.terminated,
        "route": rm.route_dicts(),
        "totalPasses": len(rm.route),
        "measures": [b.number for b in bars],
        "issues": _dedupe_issues(issues),
        "parts": [_part_dict(p) for p in part_reports],
        "pagination": {pid: v for pid, v in pagination.items()},
        "_internal": {
            "parts": parts, "bars": bars, "roadmap": rm,
            "timing": timing_data, "meta": meta,
            "structural_issues": ds_issues,
        },
    }


def evaluate_pagination(full: dict, pagination: dict) -> dict:
    """Recompute part reports for an editor-supplied pagination."""
    internal = full["_internal"]
    parts = internal["parts"]
    bars = internal["bars"]
    rm = internal["roadmap"]
    meta = internal["meta"]
    out_parts = []
    structural = internal.get("structural_issues", [])
    all_issues = list(rm.issues) + structural
    for part in parts:
        cfg = meta["parts"][part.id]
        data = internal["timing"][part.id]
        pag = pagination.get(part.id, {"breaks": [], "locked": []})
        pr = analyzer.evaluate_pages(
            part, bars, rm, cfg, data, pag.get("breaks", []), pag.get("locked", []))
        out_parts.append(_part_dict(pr))
    return {
        "roadmapOk": rm.ok,
        "terminated": rm.terminated,
        "route": rm.route_dicts(),
        "totalPasses": len(rm.route),
        "measures": [b.number for b in bars],
        "issues": [i.to_dict() for i in all_issues],
        "parts": out_parts,
        "pagination": pagination,
        "feasible": all(p["feasible"] for p in out_parts) and rm.ok,
    }


def public_report(full: dict) -> dict:
    d = {k: v for k, v in full.items() if k != "_internal"}
    d["feasible"] = all(p["feasible"] for p in d["parts"]) and d["roadmapOk"]
    return d


def confirmed_bundle(full: dict, revision_pagination: dict, digest: str) -> dict:
    """Frozen confirmation: source digest, route, pagination, table + recompute."""
    report = evaluate_pagination(full, revision_pagination)
    return {
        "sourceDigest": digest,
        "frozen": True,
        "route": report["route"],
        "pagination": revision_pagination,
        "roadmapOk": report["roadmapOk"],
        "feasible": report["feasible"],
        "parts": report["parts"],
        "issues": report["issues"],
    }


# --- helpers ------------------------------------------------------------------

def _segno_ambiguity_issues(parts, bars, rm) -> list[Issue]:
    """Locate ambiguous D.S./al Coda decisions as (part, pass, measure)."""
    out: list[Issue] = []
    ambiguous_segno = {name: idxs for name, idxs in rm.segnos.items() if len(idxs) > 1}
    ambiguous_coda = {name: idxs for name, idxs in rm.codas.items() if len(idxs) > 1}
    if not ambiguous_segno and not ambiguous_coda:
        return out

    # pass number(s) at which a directive bar is visited on the route
    pass_at: dict[int, list[int]] = {}
    for v in rm.route:
        pass_at.setdefault(v.index, []).append(v.pass_no)

    part_with_ds = {p.id: any(get_marks(pm).get("ds") or get_marks(pm).get("d_coda")
                              for pm in p.measures) for p in parts}
    for part in parts:
        if not part_with_ds[part.id]:
            continue
        for b in bars:
            marks = get_marks(part.measures[b.index])
            if not marks.get("ds"):
                continue
            # the printed directive fires once: at its first route visit
            pnos = pass_at.get(b.index, [])
            pno = pnos[0] if pnos else None
            if ambiguous_segno:
                for name, idxs in ambiguous_segno.items():
                    out.append(Issue(
                        "REPEAT_AMBIGUOUS", "error", part.id, pno, b.number,
                        f"声部 {part.name} 遍次 {pno} 小节 {b.number} 的 D.S. "
                        f"对应多个 Segno（小节 "
                        f"{', '.join(bars[x].number for x in idxs)}），"
                        "反复出口多解，该分页断点不得判为可演奏",
                        break_after=b.number))
    return out


def _part_dict(pr) -> dict:
    return {
        "partId": pr.part_id,
        "name": pr.name,
        "kind": pr.kind,
        "feasible": pr.feasible,
        "measureDurationsSeconds": pr.durations_seconds,
        "pages": [
            {
                "page": p.page,
                "breakAfter": p.break_after,
                "isTail": p.is_tail,
                "locked": p.locked,
                "feasible": p.feasible,
                "reason": p.reason,
                "gapSeconds": p.gap_seconds,
                "measureCount": p.measure_count,
                "pageSeconds": p.page_seconds,
                "moveTo": p.move_to,
                "moveCandidates": p.move_candidates,
                "crossings": [
                    {
                        "pass": c.pass_no,
                        "measure": c.measure,
                        "occurrence": c.occurrence,
                        "windowSeconds": c.window_seconds,
                        "gapSeconds": c.gap_seconds,
                        "feasible": c.feasible,
                        "reason": c.reason,
                    } for c in p.crossings
                ],
            } for p in pr.pages
        ],
    }


def _dedupe_issues(issues: list[Issue]) -> list[dict]:
    seen: set[tuple] = set()
    out = []
    for i in issues:
        key = (i.code, i.part_id, i.pass_no, i.measure, i.break_after)
        if key in seen:
            continue
        seen.add(key)
        out.append(i.to_dict())
    return out
