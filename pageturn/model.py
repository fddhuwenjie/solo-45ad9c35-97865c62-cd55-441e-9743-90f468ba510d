"""Core domain objects for the page-turn review engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# --- parsed MusicXML ----------------------------------------------------------

@dataclass
class TimeSig:
    beats: int = 4
    beat_type: int = 4


@dataclass
class NoteEvent:
    """One played note/chord or rest inside a measure (one advance step)."""
    start_q: float                       # onset in quarters from measure start
    duration_q: float                   # duration in quarters (performed)
    is_rest: bool = False
    is_grace: bool = False
    voice: str = "1"
    staff: int = 1
    staves: int = 1
    chord_notes: list[tuple[str, int, int]] = field(default_factory=list)
    tie_in: bool = False
    tie_out: bool = False
    inst_change: Optional[str] = None   # instrument-sound label

    @property
    def end_q(self) -> float:
        return self.start_q + self.duration_q


@dataclass
class TempoEvt:
    """A tempo (quarters per minute) at a quarter offset inside a measure."""
    qpm: float
    offset_q: float = 0.0
    time_only: Optional[str] = None
    source: str = "score"               # score | metadata

    def visible_in(self, occurrence: int) -> bool:
        if not self.time_only:
            return True
        return occurrence in _parse_time_only(self.time_only)


def _parse_time_only(token: str) -> set[int]:
    out: set[int] = set()
    for chunk in token.replace(" ", "").split(","):
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif chunk:
            out.add(int(chunk))
    return out


@dataclass
class PartMeasure:
    number: str
    index: int                          # 0-based printed order
    notes: list[NoteEvent] = field(default_factory=list)
    tempos: list[TempoEvt] = field(default_factory=list)
    divisions: float = 1.0
    timesig: TimeSig = field(default_factory=TimeSig)
    print_new_page: bool = False
    inst_change: Optional[str] = None

    @property
    def length_q(self) -> float:
        return 4.0 * self.timesig.beats / self.timesig.beat_type


@dataclass
class ParsedPart:
    id: str
    name: str
    kind: str = "generic"               # generic | keyboard | wind | strings | percussion
    measures: list[PartMeasure] = field(default_factory=list)
    staves: int = 1
    default_divisions: float = 1.0


# --- roadmap markings (merged across parts) -----------------------------------

@dataclass
class Bar:
    """A printed measure merged from every part."""
    index: int
    number: str
    length_q: float = 4.0               # quarters, from a global time signature
    forward_repeat: Optional[int] = None   # total plays
    backward_repeat: Optional[int] = None
    endings: list[tuple[str, str, Optional[str], str]] = field(default_factory=list)
    # (number, start|stop|discontinue, time_only, barline location)
    segno: Optional[str] = None         # usually "segno"
    segno_parts: list[str] = field(default_factory=list)
    coda: Optional[str] = None
    coda_parts: list[str] = field(default_factory=list)
    d_coda: bool = False                # "al Coda" directive at/after this bar
    ds: bool = False                    # D.S.
    dc: bool = False                    # D.C.
    fine: bool = False
    to_coda: bool = False
    tempo_events: list[TempoEvt] = field(default_factory=list)


@dataclass
class RepeatFrame:
    start: int
    end: int
    times: int                          # total plays
    current_pass: int = 1
    pending_pass: int = 0               # queued by a repeat jump, consumed at start


@dataclass
class EndingSpan:
    number: str
    start: int
    end: int                            # inclusive
    time_only: Optional[str] = None


@dataclass
class Visit:
    """One occurrence of a printed measure on the performed route."""
    index: int
    occurrence: int                    # 1-based count for this printed measure
    pass_no: int                       # 1-based overall route step (遍次)


@dataclass
class Issue:
    code: str                           # see CODES below
    severity: str                       # error | warning
    part_id: Optional[str]
    pass_no: Optional[int]
    measure: Optional[str]
    message: str
    break_after: Optional[str] = None   # printed measure number the problem sits on

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": self.severity,
            "partId": self.part_id,
            "pass": self.pass_no,
            "measure": self.measure,
            "breakAfter": self.break_after,
            "message": self.message,
        }


CODES = {
    "JUMP_CYCLE": "跳转成环：演奏路线无法终止",
    "REPEAT_AMBIGUOUS": "反复出口多解：缺少配对反复记号或遍次标记",
    "MISSING_TEMPO": "速度缺失：无法把小节换算为秒",
    "CROSS_PAGE_SUSTAIN": "跨页持续音：延音线越过翻页点",
    "NO_GAP": "没有可用空档：翻页双手被占用",
    "JUMP_TARGET_MISSING": "跳转目标（Segno/Coda/Fine）缺失",
    "PAGE_CAPACITY": "页容量超限（警告）",
}


# --- analysis results ---------------------------------------------------------

@dataclass
class Crossing:
    """One moment the performed route crosses the boundary after a printed bar."""
    pass_no: int
    measure: str                        # printed number before boundary
    occurrence: int
    gap_seconds: Optional[float]        # None when timing unknown
    feasible: bool
    reason: Optional[str]               # code when not feasible
    after_visit: int
    before_visit: int
    busy: float = 0.0                   # seconds occupied inside the window
    window_seconds: Optional[float] = None


@dataclass
class PageReview:
    page: int
    break_after: Optional[str] = None   # None for the final (tail) page
    locked: bool = False
    crossings: list[Crossing] = field(default_factory=list)
    feasible: bool = True
    reason: Optional[str] = None
    gap_seconds: Optional[float] = None
    move_to: Optional[str] = None       # proposal
    move_candidates: list[dict] = field(default_factory=list)
    measure_count: int = 0
    page_seconds: Optional[float] = None
    is_tail: bool = False               # final page after the last break


@dataclass
class PartConfig:
    turn_seconds: float = 2.0
    attack_margin: float = 0.25
    instrument_change_seconds: float = 2.0
    max_measures_per_page: Optional[int] = None
    max_page_seconds: Optional[float] = None
    immovable: list[str] = field(default_factory=list)


@dataclass
class PartReport:
    part_id: str
    name: str
    kind: str
    pages: list[PageReview] = field(default_factory=list)
    feasible: bool = True
    durations_seconds: dict[str, float] = field(default_factory=dict)
