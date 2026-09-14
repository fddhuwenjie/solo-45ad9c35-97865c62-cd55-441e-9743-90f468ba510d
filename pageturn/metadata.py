"""External metadata: tempo supplements, existing pagination, locks, tuning."""
from __future__ import annotations

from .model import PartConfig, TempoEvt


DEFAULTS = PartConfig()


def normalize_metadata(meta: dict | None, part_ids: list[str]) -> dict:
    meta = meta or {}
    out_parts: dict[str, PartConfig] = {}
    raw_parts = meta.get("parts", {})
    for pid in part_ids:
        cfg = PartConfig()
        pmeta = raw_parts.get(pid, {})
        cfg.turn_seconds = float(pmeta.get("turnSeconds", meta.get("turnSeconds", 2.0)))
        cfg.attack_margin = float(pmeta.get("attackMargin", meta.get("attackMargin", 0.25)))
        cfg.instrument_change_seconds = float(pmeta.get(
            "instrumentChangeSeconds", meta.get("instrumentChangeSeconds", 2.0)))
        if pmeta.get("maxMeasuresPerPage", meta.get("maxMeasuresPerPage")) is not None:
            cfg.max_measures_per_page = int(
                pmeta.get("maxMeasuresPerPage", meta.get("maxMeasuresPerPage")))
        if pmeta.get("maxPageSeconds", meta.get("maxPageSeconds")) is not None:
            cfg.max_page_seconds = float(
                pmeta.get("maxPageSeconds", meta.get("maxPageSeconds")))
        imm = list(pmeta.get("immovable", [])) + [
            m for m in meta.get("immovable", []) if True]
        cfg.immovable = [str(m) for m in dict.fromkeys(imm)]
        out_parts[pid] = cfg

    return {
        "parts": out_parts,
        "pagination": _normalize_pagination(meta.get("pagination", {}), part_ids),
        "tempos": _normalize_tempos(meta.get("tempoChanges", [])),
    }


def _normalize_pagination(pag: dict, part_ids: list[str]) -> dict[str, dict]:
    """{partId: {"breaks": [m,..], "locked": [m,..]}} or {"breaks":[..]} global."""
    out: dict[str, dict] = {}
    gbreaks = pag.get("breaks") if isinstance(pag, dict) else None
    glocked = pag.get("locked") if isinstance(pag, dict) else None
    for pid in part_ids:
        entry = pag.get(pid, {}) if isinstance(pag, dict) else {}
        breaks = entry.get("breaks", gbreaks if gbreaks is not None else None)
        locked = entry.get("locked", glocked if glocked is not None else [])
        out[pid] = {
            "breaks": [str(b) for b in (breaks or [])],
            "locked": [str(b) for b in (locked or [])],
        }
    return out


def _normalize_tempos(items: list) -> list[dict]:
    """[{'measure': '3', 'qpm': 120, 'offsetQuarters': 0, 'timeOnly': '1'}]"""
    out: list[dict] = []
    for it in items:
        try:
            out.append({
                "measure": str(it["measure"]),
                "event": TempoEvt(
                    qpm=float(it["qpm"]),
                    offset_q=float(it.get("offsetQuarters", 0.0)),
                    time_only=str(it["timeOnly"]) if it.get("timeOnly") else None,
                    source="metadata"),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return out
