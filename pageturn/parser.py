"""MusicXML reader (partwise) built on xml.etree.ElementTree.

Extracts notes/tuplets/ties, tempo changes, repeat/jump markings, existing
pagination and per-part configuration. Metadata supplied through the API may
override or supplement anything that is missing from the paper score.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET

from .model import (
    Bar, NoteEvent, ParsedPart, PartMeasure, TempoEvt, TimeSig,
)


def _text(el: ET.Element | None) -> str:
    return (el.text or "").strip() if el is not None else ""


def _bool(v: str | None) -> bool:
    return (v or "").lower() in ("yes", "true", "1")


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _localname(c.tag) == name:
            return c
    return None


def _children(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _localname(c.tag) == name]


def _find(el: ET.Element, path_names: list[str]) -> ET.Element | None:
    """Find first descendant by chain of local names."""
    cur = el
    for name in path_names:
        nxt = _child(cur, name)
        if nxt is None:
            return None
        cur = nxt
    return cur


# ---------------------------------------------------------------------------

def parse_musicxml(xml_text: str) -> tuple[list[ParsedPart], list[Bar]]:
    root = ET.fromstring(xml_text)
    if _localname(root.tag) != "score-partwise":
        raise ValueError("仅支持 partwise（按小节）结构的 MusicXML")

    part_names = _read_part_list(root)
    parts: list[ParsedPart] = []
    for pel in _children(root, "part"):
        pid = pel.get("id", "")
        name, _ = part_names.get(pid, (pid, "generic"))
        parts.append(_parse_part(pel, pid, name))

    bars = _merge_bars(parts)
    return parts, bars


def _read_part_list(root: ET.Element) -> dict[str, tuple[str, str]]:
    plist = _child(root, "part-list")
    out: dict[str, tuple[str, str]] = {}
    if plist is None:
        return out
    for sp in _children(plist, "score-part"):
        pid = sp.get("id", "")
        name_el = _child(sp, "part-name")
        name = _text(name_el) or pid
        out[pid] = (name, _guess_kind(name))
    return out


def _guess_kind(name: str) -> str:
    low = name.lower()
    if any(k in low for k in ("piano", "harp", "hpschd", "harpsichord", "organ", "celesta", "keyboard")):
        return "keyboard"
    if any(k in low for k in ("flute", "oboe", "clarinet", "bassoon", "trumpet", "horn",
                              "trombone", "tuba", "saxophone", "wind", "cor")):
        return "wind"
    if any(k in low for k in ("violin", "viola", "cello", "bass", "strings", "harp")):
        return "strings"
    if "percussion" in low or "timpani" in low:
        return "percussion"
    return "generic"


# --- per-part measure parsing -------------------------------------------------

def _parse_part(pel: ET.Element, pid: str, name: str) -> ParsedPart:
    part = ParsedPart(id=pid, name=name, kind=_guess_kind(name))
    divisions = 1.0
    timesig = TimeSig()
    staves = 1
    for idx, mel in enumerate(_children(pel, "measure")):
        mnum = mel.get("number", str(idx + 1))
        pm = PartMeasure(number=mnum, index=idx, divisions=divisions,
                         timesig=TimeSig(timesig.beats, timesig.beat_type))
        voice_pos: dict[str, float] = {}
        global_pos = 0.0

        for el in mel:
            tag = _localname(el.tag)
            if tag == "attributes":
                d = _child(el, "divisions")
                if d is not None and _text(d):
                    divisions = max(float(_text(d)), 0.0001)
                    pm.divisions = divisions
                    part.default_divisions = divisions
                t = _child(el, "time")
                if t is not None:
                    b = _text(_child(t, "beats"))
                    bt = _text(_child(t, "beat-type"))
                    if b.isdigit() and bt.isdigit():
                        timesig = TimeSig(int(b), int(bt))
                        pm.timesig = timesig
                st = _child(el, "staves")
                if st is not None and _text(st).isdigit():
                    staves = int(_text(st))
                part.staves = max(part.staves, staves)
            elif tag == "print" and _bool(el.get("new-page")):
                pm.print_new_page = True
            elif tag == "backup":
                dur = _float(_child(el, "duration"), 0.0)
                global_pos -= dur / divisions
            elif tag == "forward":
                dur = _float(_child(el, "duration"), 0.0)
                global_pos += dur / divisions
            elif tag == "direction":
                _read_direction(el, pm, divisions)
            elif tag == "barline":
                _read_barline(el, pm)
            elif tag == "sound":
                _read_sound(el, pm, divisions, offset_q=0.0)
            elif tag == "note":
                global_pos = _read_note(el, pm, divisions, staves, voice_pos, global_pos)
        part.measures.append(pm)
    return part


def _read_direction(el: ET.Element, pm: PartMeasure, divisions: float) -> None:
    time_only_el = _find(el, ["direction-type", "time-only"])
    time_only = _text(time_only_el) or None
    # direction/offset precedes direction-type in the schema
    offset_q = 0.0
    off = _child(el, "offset")
    if off is not None and _text(off):
        offset_q = float(_text(off)) / divisions
        if off.get("sound") == "no":
            offset_q = 0.0
    for dt in _children(el, "direction-type"):
        for sub in dt:
            name = _localname(sub.tag)
            if name == "words":
                _classify_words(_text(sub), pm)
            elif name == "segno":
                pm_ensure_marks(pm)
            elif name == "coda":
                _set_mark(pm, "coda", sub.get("name") or "coda")
            elif name == "fine":
                _set_mark(pm, "fine", True)
    _read_sound(el, pm, divisions, offset_q, time_only)
    ic = _find(el, ["direction-type", "instrument-change", "instrument-sound"])
    if ic is not None:
        pm.inst_change = _text(ic) or "instrument"


def pm_ensure_marks(pm: PartMeasure) -> None:
    # segno lives on the merged bar; stash via a parallel attribute mechanism
    _set_mark(pm, "segno", "segno")


def _set_mark(pm: PartMeasure, key: str, value) -> None:
    # PartMeasure has no dedicated marking fields; use __dict__ extras.
    marks = pm.__dict__.setdefault("_marks", {})
    marks[key] = value


def get_marks(pm: PartMeasure) -> dict:
    return pm.__dict__.get("_marks", {})


def _classify_words(text: str, pm: PartMeasure) -> None:
    if not text:
        return
    t = " ".join(text.lower().split())
    marks = pm.__dict__.setdefault("_marks", {})
    if t.startswith(("d.s.", "d.s ", "dal segno")):
        marks["ds"] = True
        if "coda" in t and "fine" not in t:
            marks["d_coda"] = True
        if "fine" in t:
            marks["fine"] = True
    elif t.startswith(("d.c.", "d.c ", "da capo")):
        marks["dc"] = True
        if "coda" in t and "fine" not in t:
            marks["d_coda"] = True
        if "fine" in t:
            marks["fine"] = True
    elif t.startswith("to coda") or t in ("coda",):
        marks["to_coda"] = True
    elif t == "fine" or t.endswith(" alla fine"):
        marks["fine"] = True


def _read_barline(el: ET.Element, pm: PartMeasure) -> None:
    marks = pm.__dict__.setdefault("_marks", {})
    location = el.get("location", "right")
    ending = _child(el, "ending")
    if ending is not None:
        marks.setdefault("endings", []).append(
            (ending.get("number", "1"), ending.get("type", "start"),
             _text(_child(ending, "time-only")) or ending.get("time-only"),
             ending.get("location", location)))
    rep = _child(el, "repeat")
    if rep is not None:
        times = int(rep.get("times", "2"))
        if rep.get("direction") == "forward":
            marks["forward_repeat"] = times
        elif rep.get("direction") == "backward":
            marks["backward_repeat"] = times
    for name in ("segno", "coda", "fine"):
        if _child(el, name) is not None:
            if name == "segno":
                marks["segno"] = "segno"
            elif name == "fine":
                marks["fine"] = True
            else:
                marks["coda"] = "coda"


def _read_sound(el: ET.Element, pm: PartMeasure, divisions: float,
                offset_q: float = 0.0, time_only: str | None = None) -> None:
    snd = _child(el, "sound") if _localname(el.tag) != "sound" else el
    if snd is None:
        return
    if snd.get("tempo"):
        try:
            qpm = float(snd.get("tempo"))
        except ValueError:
            return
        off = offset_q
        if snd.get("time-only"):
            time_only = snd.get("time-only")
        pm.tempos.append(TempoEvt(qpm=qpm, offset_q=off, time_only=time_only))
    if snd.get("segno"):
        pm.__dict__.setdefault("_marks", {})["segno"] = snd.get("segno") or "segno"
    if snd.get("coda"):
        pm.__dict__.setdefault("_marks", {})["coda"] = snd.get("coda") or "coda"
    if _bool(snd.get("fine")):
        pm.__dict__.setdefault("_marks", {})["fine"] = True
    if _bool(snd.get("dacapo")):
        pm.__dict__.setdefault("_marks", {})["dc"] = True
    if snd.get("dalsegno"):
        pm.__dict__.setdefault("_marks", {})["ds"] = True


def _float(el: ET.Element | None, default: float) -> float:
    try:
        return float(_text(el))
    except ValueError:
        return default


def _read_note(el: ET.Element, pm: PartMeasure, divisions: float, staves: int,
               voice_pos: dict[str, float], global_pos: float) -> float:
    voice = _text(_child(el, "voice")) or "1"
    staff_el = _child(el, "staff")
    staff = int(_text(staff_el)) if staff_el is not None and _text(staff_el).isdigit() else 1
    is_chord = _child(el, "chord") is not None
    is_rest = _child(el, "rest") is not None
    is_grace = _child(el, "grace") is not None

    start = voice_pos.get(voice, global_pos) if not is_chord else None
    if is_chord and pm.notes:
        # attach to last event of the same voice
        prev = next((n for n in reversed(pm.notes) if n.voice == voice), pm.notes[-1])
        pitch = _pitch(el)
        if pitch:
            prev.chord_notes.append(pitch)
        return global_pos

    nominal_div = _float(_child(el, "duration"), 0.0)
    duration_q = nominal_div / divisions if not is_grace else 0.0
    tm = _find(el, ["time-modification"])
    if tm is not None and duration_q:
        actual = _float(_child(tm, "actual-notes"), 0.0)
        normal = _float(_child(tm, "normal-notes"), 0.0)
        if actual > 0 and normal > 0:
            duration_q = duration_q * normal / actual

    tie_in = tie_out = False
    for tied in el.iter():
        if _localname(tied.tag) == "tied":
            if tied.get("type") == "start":
                tie_out = True
            if tied.get("type") in ("stop", "continue"):
                tie_in = True
    # <tie> elements mirror the same info
    for t in _children(el, "tie"):
        if t.get("type") == "start":
            tie_out = True
        elif t.get("type") in ("stop",):
            tie_in = True

    inst = None
    ic = _find(el, ["instrument-change", "instrument-sound"])
    if ic is not None:
        inst = _text(ic) or "instrument"
    if _child(el, "instrument") is not None and not is_rest:
        inst = inst or _child(el, "instrument").get("id") or "instrument"

    ne = NoteEvent(
        start_q=start if start is not None else global_pos,
        duration_q=duration_q, is_rest=is_rest, is_grace=is_grace,
        voice=voice, staff=staff, staves=staves,
        chord_notes=[], tie_in=tie_in, tie_out=tie_out, inst_change=inst)
    pitch = _pitch(el)
    if pitch and not is_rest:
        ne.chord_notes.append(pitch)
    pm.notes.append(ne)
    if not is_grace:
        voice_pos[voice] = (start if start is not None else global_pos) + duration_q
        global_pos = max(global_pos, voice_pos[voice])
    return global_pos


def _pitch(el: ET.Element) -> tuple[str, int, int] | None:
    p = _child(el, "pitch")
    if p is None:
        return None
    step = _text(_child(p, "step")) or "C"
    alter = int(_float(_child(p, "alter"), 0.0))
    octv = int(_float(_child(p, "octave"), 4))
    return (step, alter, octv)


# --- merge into global bars ---------------------------------------------------

def _merge_bars(parts: list[ParsedPart]) -> list[Bar]:
    n = max((len(p.measures) for p in parts), default=0)
    bars: list[Bar] = []
    for i in range(n):
        number = next((p.measures[i].number for p in parts
                       if i < len(p.measures)), str(i + 1))
        length_q = max(
            (p.measures[i].length_q for p in parts if i < len(p.measures)),
            default=4.0)
        b = Bar(index=i, number=number, length_q=length_q)
        for p in parts:
            if i >= len(p.measures):
                continue
            pm = p.measures[i]
            marks = get_marks(pm)
            if "forward_repeat" in marks:
                b.forward_repeat = marks["forward_repeat"]
            if "backward_repeat" in marks:
                b.backward_repeat = marks["backward_repeat"]
            if marks.get("segno"):
                b.segno = marks["segno"]
                if p.id not in b.segno_parts:
                    b.segno_parts.append(p.id)
            if marks.get("coda"):
                b.coda = marks["coda"]
                if p.id not in b.coda_parts:
                    b.coda_parts.append(p.id)
            for key in ("d_coda", "ds", "dc", "fine", "to_coda"):
                if marks.get(key):
                    setattr(b, key, True)
            for e in marks.get("endings", []):
                if e not in b.endings:
                    b.endings.append(e)
            for te in pm.tempos:
                if te not in b.tempo_events:
                    b.tempo_events.append(te)
        bars.append(b)
    return bars
