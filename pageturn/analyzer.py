"""Per-part timing and page-turn analysis on top of an expanded route."""
from __future__ import annotations

from dataclasses import dataclass

from .model import (
    Bar, Crossing, Issue, PageReview, PartConfig, PartMeasure, PartReport,
    ParsedPart,
)
from .roadmap import Roadmap


@dataclass
class Segment:
    """One visit to a printed measure with resolved timing context."""
    visit: object
    pm: PartMeasure
    bar: Bar
    start_sec: float
    duration_sec: float | None       # None when tempo unknown
    # piece-relative seconds at quarter offsets where tempo changes (incl. 0)
    quarter_clock: list[tuple[float, float]]

    def clock(self, q: float) -> float:
        """Piece-relative seconds for quarter offset q in this visit."""
        ticks = self.quarter_clock
        lo = ticks[0]
        hi = ticks[-1]
        for k in range(len(ticks) - 1):
            if ticks[k][0] <= q <= ticks[k + 1][0] + 1e-9:
                lo, hi = ticks[k], ticks[k + 1]
                break
        if hi[0] - lo[0] < 1e-9:
            return lo[1]
        frac = (q - lo[0]) / (hi[0] - lo[0])
        return lo[1] + frac * (hi[1] - lo[1])


def analyze_part(part: ParsedPart, bars: list[Bar], rm: Roadmap,
                 cfg: PartConfig) -> tuple[PartReport, list[Issue], dict]:
    issues: list[Issue] = []
    segments = _build_segments(part, bars, rm, issues)
    busy = _busy_intervals(part, segments, cfg)
    tie_boundaries = _tie_boundaries(part)
    return (PartReport(part_id=part.id, name=part.name, kind=part.kind),
            issues,
            {"segments": segments,
             "busy": busy,
             "tie_boundaries": tie_boundaries})


# --- timing -------------------------------------------------------------------

def _build_segments(part: ParsedPart, bars: list[Bar], rm: Roadmap,
                    issues: list[Issue]) -> list[Segment]:
    segments: list[Segment] = []
    current_qpm: float | None = None
    missing_reported: set[int] = set()
    clock = 0.0

    for v in rm.route:
        if v.index >= len(part.measures):
            continue
        pm = part.measures[v.index]
        bar = bars[v.index]

        events = _tempo_events(bar, pm, v.occurrence)
        for off, qpm in events:
            if off <= 0.0:
                current_qpm = qpm

        missing = current_qpm is None
        if missing and v.index not in missing_reported:
            missing_reported.add(v.index)
            issues.append(Issue(
                "MISSING_TEMPO", "error", part.id, v.pass_no, bar.number,
                f"第 {bar.number} 小节（遍次 {v.pass_no}）缺少速度，无法换算时长",
                break_after=bar.number))

        length_q = bar.length_q
        cuts = sorted({0.0, length_q}
                      | {min(length_q, max(0.0, off)) for off, _ in events if off > 0})
        ticks: list[tuple[float, float]] = [(0.0, clock)]
        duration = 0.0 if not missing else None
        qstart = 0.0
        for qcut in cuts[1:]:
            span = qcut - qstart
            if not missing:
                duration += 60.0 * span / current_qpm
                ticks.append((qcut, clock + duration))
            for off, qpm in events:
                if abs(off - qcut) < 1e-9:
                    current_qpm = qpm
            qstart = qcut
        if missing:
            ticks = [(0.0, clock), (length_q, clock)]

        seg = Segment(visit=v, pm=pm, bar=bar, start_sec=clock,
                      duration_sec=duration, quarter_clock=ticks)
        segments.append(seg)
        clock += duration if duration is not None else 0.0
    return segments


def _tempo_events(bar: Bar, pm: PartMeasure, occurrence: int):
    events: list[tuple[float, float]] = []
    for te in bar.tempo_events:
        if te.visible_in(occurrence):
            events.append((te.offset_q, te.qpm))
    for te in pm.tempos:
        if te.visible_in(occurrence):
            off = te.offset_q / pm.divisions if pm.divisions else te.offset_q
            events.append((off, te.qpm))
    events.sort(key=lambda e: e[0])
    return events


# --- occupied time ------------------------------------------------------------

def _busy_intervals(part: ParsedPart, segments: list[Segment],
                    cfg: PartConfig) -> dict[int, list[tuple[float, float]]]:
    busy: dict[int, list[tuple[float, float]]] = {}
    for s in segments:
        if s.duration_sec is None:
            busy[s.visit.pass_no] = []
            continue
        iv: list[tuple[float, float]] = []
        for ne in s.pm.notes:
            if ne.is_grace or ne.is_rest:
                continue
            st = s.clock(ne.start_q)
            en = s.clock(ne.end_q)
            iv.append((st - cfg.attack_margin, en + cfg.attack_margin))
            if ne.inst_change:
                iv.append((st, st + cfg.instrument_change_seconds))
        busy[s.visit.pass_no] = _union(iv)
    return busy


def _tie_boundaries(part: ParsedPart) -> set[int]:
    """Printed boundary indices i that at least one tie crosses (i -> i+1)."""
    out: set[int] = set()
    open_ties: dict[tuple, int] = {}        # pitch key -> printed start index
    for idx, pm in enumerate(part.measures):
        carried: dict[tuple, int] = {}
        for ne in pm.notes:
            if ne.is_rest or ne.is_grace:
                continue
            for pitch in (ne.chord_notes or [("X", 0, ne.staff)]):
                key = (ne.voice, ne.staff, pitch)
                start = open_ties.get(key, idx) if ne.tie_in else idx
                if ne.tie_out:
                    carried[key] = start
        # every chain still open from an earlier printed measure crosses idx-1
        for start in open_ties.values():
            if start < idx:
                out.add(idx - 1)
        open_ties = carried
    return out


def _union(iv: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not iv:
        return []
    iv = sorted(iv)
    out: list[list[float]] = [list(iv[0])]
    for a, b in iv[1:]:
        if a <= out[-1][1] + 1e-9:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


# --- pagination ---------------------------------------------------------------

def evaluate_pages(part: ParsedPart, bars: list[Bar], rm: Roadmap,
                   cfg: PartConfig, data: dict,
                   breaks: list[str], locked: list[str]) -> PartReport:
    segments: list[Segment] = data["segments"]
    busy = data["busy"]
    tie_boundaries: set[int] = data["tie_boundaries"]
    num2idx = {b.number: i for i, b in enumerate(bars)}
    break_idx = sorted({num2idx[m] for m in breaks if m in num2idx})
    locked_set = {num2idx[m] for m in locked if m in num2idx}
    immovable = {num2idx[m] for m in cfg.immovable if m in num2idx}

    crossings: dict[int, list[Crossing]] = {}
    for k in range(len(segments) - 1):
        s1, s2 = segments[k], segments[k + 1]
        if s2.visit.index != s1.visit.index + 1:
            continue  # only physically consecutive paper edges need a turn
        crossings.setdefault(s1.visit.index, []).append(
            _evaluate_crossing(s1, s2, busy, tie_boundaries, bars, cfg))

    status: dict[int, tuple[bool, str | None, float | None]] = {}
    for bi, crs in crossings.items():
        bad = next((c for c in crs if not c.feasible), None)
        gap_vals = [c.gap_seconds for c in crs if c.gap_seconds is not None]
        status[bi] = (
            bad is None,
            None if bad is None else bad.reason or "NO_GAP",
            min(gap_vals) if gap_vals else None,
        )

    report = PartReport(part_id=part.id, name=part.name, kind=part.kind)
    durations: dict[str, float] = {}
    for s in segments:
        if s.duration_sec is not None:
            durations[s.bar.number] = round(s.duration_sec, 3)
    report.durations_seconds = durations

    for page_no, bi in enumerate(break_idx, start=1):
        feasible, reason, gap = status.get(bi, (True, None, None))
        pr = PageReview(page=page_no, break_after=bars[bi].number,
                        locked=bi in locked_set,
                        crossings=crossings.get(bi, []),
                        feasible=feasible, reason=reason,
                        gap_seconds=round(gap, 3) if gap is not None else None)
        _capacity(pr, bi, break_idx, segments, cfg)
        if not pr.locked and not pr.feasible:
            _propose_move(pr, bi, break_idx, bars, status, locked_set, immovable, cfg)
        report.pages.append(pr)
    report.feasible = all(p.feasible for p in report.pages)
    return report


def _evaluate_crossing(s1: Segment, s2: Segment, busy, tie_boundaries: set[int],
                       bars: list[Bar], cfg: PartConfig) -> Crossing:
    bi = s1.visit.index
    base = Crossing(
        pass_no=s2.visit.pass_no, measure=bars[bi].number,
        occurrence=s1.visit.occurrence, gap_seconds=None, feasible=False,
        reason=None, after_visit=s1.visit.pass_no,
        before_visit=s2.visit.pass_no, busy=0.0, window_seconds=None)

    if bi in tie_boundaries:
        base.reason = "CROSS_PAGE_SUSTAIN"
        base.gap_seconds = 0.0
        base.window_seconds = 0.0
        return base
    if s1.duration_sec is None or s2.duration_sec is None:
        base.reason = "MISSING_TEMPO"
        return base

    b1 = busy.get(s1.visit.pass_no, [])
    b2 = busy.get(s2.visit.pass_no, [])
    last = max((e for _, e in b1
                if e <= s1.start_sec + s1.duration_sec + 0.5),
               default=s1.start_sec)
    first = min((a for a, _ in b2 if a >= s2.start_sec - 0.5),
                default=s2.start_sec + s2.duration_sec)
    last = max(last, s1.start_sec)
    first = min(first, s2.start_sec + s2.duration_sec)
    window = max(0.0, first - last)
    gap = window
    base.window_seconds = round(window, 3)
    base.gap_seconds = round(gap, 3)
    base.feasible = gap + 1e-9 >= cfg.turn_seconds
    base.reason = None if base.feasible else "NO_GAP"
    return base


def _capacity(pr: PageReview, bi: int, break_idx: list[int],
              segments: list[Segment], cfg: PartConfig) -> None:
    pos = break_idx.index(bi)
    start = break_idx[pos - 1] + 1 if pos > 0 else 0
    pr.measure_count = bi - start + 1  # type: ignore[attr-defined]
    if cfg.max_measures_per_page and pr.measure_count > cfg.max_measures_per_page:
        pr.feasible = False
        pr.reason = pr.reason or "PAGE_CAPACITY"


def _propose_move(pr: PageReview, bi: int, break_idx: list[int], bars,
                  status: dict, locked_set: set[int], immovable: set[int],
                  cfg: PartConfig) -> None:
    pos = break_idx.index(bi)
    prev_bound = break_idx[pos - 1] if pos > 0 else -1
    next_bound = break_idx[pos + 1] if pos + 1 < len(break_idx) else len(bars)
    cands: list[dict] = []
    for ci in range(prev_bound + 1, next_bound):
        if ci == bi or ci in locked_set:
            continue
        # no break right before/after a locked measure
        if ci in immovable or (ci + 1) in immovable:
            continue
        count_left = ci - prev_bound
        count_right = next_bound - 1 - ci
        if cfg.max_measures_per_page and (
                count_left > cfg.max_measures_per_page
                or count_right > cfg.max_measures_per_page):
            continue
        feasible, reason, gap = status.get(ci, (True, None, None))
        cands.append({
            "breakAfter": bars[ci].number,
            "feasible": feasible,
            "reason": reason,
            "gapSeconds": round(gap, 3) if gap is not None else None,
        })
    good = [c for c in cands if c["feasible"]]
    good.sort(key=lambda c: -(c["gapSeconds"] or 0.0))
    pr.move_candidates = cands
    if good:
        pr.move_to = good[0]["breakAfter"]
