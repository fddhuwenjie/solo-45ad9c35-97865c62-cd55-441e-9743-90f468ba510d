"""Helpers for building small partwise MusicXML documents in tests."""
from __future__ import annotations


def note(dur: int = 1, step: str = "C", octv: int = 4, *,
         tie: str | None = None, rest: bool = False, grace: bool = False,
         voice: str = "1", staff: int | None = None,
         tuplet: tuple[int, int] | None = None, inst: str | None = None,
         chord: bool = False, divisions: int = 1) -> str:
    """dur in division units; tie: start|stop|both; tuplet=(actual,normal)."""
    a = []
    if chord:
        a.append("<chord/>")
    if rest:
        a.append(f'<rest/><duration>{dur}</duration>')
    elif grace:
        a.append("<grace/>")
        a.append(f'<pitch><step>{step}</step><octave>{octv}</octave></pitch>')
    else:
        a.append(f'<pitch><step>{step}</step><octave>{octv}</octave></pitch>')
        a.append(f'<duration>{dur}</duration>')
    a.append(f'<voice>{voice}</voice>')
    if staff is not None:
        a.append(f'<staff>{staff}</staff>')
    tie_types = []
    if tie in ("start", "both"):
        tie_types.append("start")
    if tie in ("stop", "both"):
        tie_types.append("stop")
    for t in tie_types:
        a.append(f'<tie type="{t}"/>')
    if tuplet:
        actual, normal = tuplet
        a.append("<time-modification>"
                 f"<actual-notes>{actual}</actual-notes>"
                 f"<normal-notes>{normal}</normal-notes>"
                 "</time-modification>")
    if inst:
        a.append(f'<instrument-change><instrument-sound>{inst}</instrument-sound>'
                 "</instrument-change>")
    return "<note>" + "".join(a) + "</note>"


def rest(dur: int = 1) -> str:
    return note(dur=dur, rest=True)


def measure(num, body: str = "", *, tempo: float | None = None,
            new_page: bool = False, forward: int | None = None,
            backward: int | None = None, ending: tuple[str, str] | None = None,
            segno: bool = False, coda: bool = False, fine: bool = False,
            ds: bool = False, dc: bool = False, d_coda: bool = False,
            to_coda: bool = False, divisions: int = 1, beats: int = 4,
            timesig: bool = False, time_only: str | None = None) -> str:
    pre = ""
    if new_page:
        pre += '<print new-page="yes"/>'
    if timesig:
        pre += (f'<attributes><divisions>{divisions}</divisions>'
                f'<time><beats>{beats}</beats><beat-type>4</beat-type></time>'
                '</attributes>')
    elif num == 1:
        pre += f'<attributes><divisions>{divisions}</divisions></attributes>'
    if tempo is not None:
        pre += (f'<direction><direction-type><words>tempo</words></direction-type>'
                f'<sound tempo="{tempo}"/></direction>')
    words = None
    if ds:
        words = "D.S." + (" al Coda" if d_coda else (" al Fine" if fine else ""))
    elif dc:
        words = "D.C." + (" al Coda" if d_coda else (" al Fine" if fine else ""))
    elif to_coda:
        words = "To Coda"
    elif fine and not (ds or dc):
        words = "Fine"
    if words:
        dto = f'<time-only>{time_only}</time-only>' if time_only else ""
        pre += (f'<direction><direction-type><words>{words}</words>{dto}'
                f'</direction-type></direction>')
    if segno:
        pre += ('<barline location="left"><segno/></barline>'
                if False else '<direction><direction-type><segno/></direction-type>'
                '</direction>')
    if coda:
        pre += '<direction><direction-type><coda/></direction-type></direction>'
    post = ""
    bl = []
    if ending:
        number, etype = ending
        bl.append(f'<ending number="{number}" type="{etype}"/>')
    if forward is not None:
        bl.append(f'<repeat direction="forward" times="{forward}"/>')
    if backward is not None:
        bl.append(f'<repeat direction="backward" times="{backward}"/>')
    if bl:
        post = f'<barline location="right-lower">{"".join(bl)}</barline>'
    if fine and (ds or dc):
        # fine marking appears later as its own measure normally; nothing extra
        pass
    return f'<measure number="{num}">{pre}{body}{post}</measure>'


def part(pid: str, measures: list[str], name: str | None = None) -> str:
    pname = name or pid
    return (f'<part id="{pid}">{"".join(measures)}</part>')


def score(parts: list[str], names: dict[str, str] | None = None) -> str:
    names = names or {}
    plist = []
    for p in parts:
        pid = p.split('id="', 1)[1].split('"', 1)[0]
        pname = names.get(pid, pid)
        plist.append(f'<score-part id="{pid}"><part-name>{pname}</part-name>'
                     '</score-part>')
    return ('<?xml version="1.0"?>'
            '<score-partwise version="3.1"><part-list>'
            + "".join(plist) + '</part-list>' + "".join(parts) +
            '</score-partwise>')


def ending_stop(num: str) -> str:
    return f'<barline location="right-lower"><ending number="{num}" type="stop"/></barline>'
