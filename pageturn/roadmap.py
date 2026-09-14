"""Expand the printed bars into the actual performed route.

Handles forward/backward repeats with volta endings, D.C./D.S. (al Fine / al
Coda), To Coda jumps and explicit Fine. The classic convention is applied:
after a D.C./D.S. jump repeats are skipped and no jump is taken twice.
Structural problems are returned as issues and the route is best-effort;
callers must never treat a breakpoint as playable when `ok` is false.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .model import Bar, EndingSpan, Issue, RepeatFrame, Visit


MAX_STEPS = 100_000


@dataclass
class Roadmap:
    route: list[Visit] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    frames: list[RepeatFrame] = field(default_factory=list)
    endings: list[EndingSpan] = field(default_factory=list)
    segnos: dict[str, int] = field(default_factory=dict)
    codas: dict[str, int] = field(default_factory=dict)
    terminated: bool = True
    numbers: dict[int, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.terminated and not any(i.severity == "error" for i in self.issues)

    def mnum(self, idx: int) -> str:
        return self.numbers.get(idx, str(idx + 1))

    def route_dicts(self) -> list[dict]:
        return [{"pass": v.pass_no, "measure": self.mnum(v.index),
                 "occurrence": v.occurrence} for v in self.route]


def expand(bars: list[Bar]) -> Roadmap:
    rm = Roadmap(numbers={b.index: b.number for b in bars})
    if not bars:
        return rm

    _index_marks(bars, rm)
    _pair_frames(bars, rm)
    _build_ending_spans(bars, rm)

    n = len(bars)
    frame_by_end = {f.end: f for f in rm.frames}
    piece_has_jump = any(b.dc or b.ds for b in bars)

    counts = [0] * n
    visits: list[Visit] = []
    i = 0
    pass_no = 0
    after_jump = False                # repeats suppressed after DC/DS
    used_jumps: set[int] = set()
    to_coda_target: int | None = None
    state_seen: set[tuple] = set()
    # current 1-based pass of each repeat frame. Initialised once at frame
    # start; a repeat jump advances it through `next_pass`.
    current_pass: dict[int, int] = {}
    next_pass: dict[int, int] = {}
    started: set[int] = set()

    while 0 <= i < n:
        if pass_no >= MAX_STEPS:
            rm.terminated = False
            rm.issues.append(Issue(
                "JUMP_CYCLE", "error", None, pass_no, rm.mnum(i),
                f"跳转成环：超过 {MAX_STEPS} 个遍次仍未终止",
                break_after=rm.mnum(i)))
            break

        b = bars[i]

        # set the pass of every frame starting at this printed bar, but only
        # the first arrival unless a repeat jump queued a new pass
        for f in rm.frames:
            if f.start == i:
                fid = id(f)
                if fid in next_pass:
                    current_pass[fid] = next_pass.pop(fid)
                elif fid not in started:
                    current_pass[fid] = 1
                    started.add(fid)

        # volta gate: play only the ending matching the frame's current pass
        span = _ending_starting_at(rm.endings, i)
        if span is not None:
            frame = _frame_for_ending(rm.frames, span)
            entry = current_pass.get(id(frame), 1) if frame is not None else counts[i] + 1
            if not _ending_matches(span, entry):
                i = span.end + 1
                continue

        pass_no += 1
        counts[i] += 1
        visits.append(Visit(index=i, occurrence=counts[i], pass_no=pass_no))

        state = (i, counts[i], after_jump, tuple(sorted(used_jumps)), to_coda_target)
        if state in state_seen:
            rm.terminated = False
            rm.issues.append(Issue(
                "JUMP_CYCLE", "error", None, pass_no, b.number,
                "跳转成环：控制状态重复出现，路线无法终止",
                break_after=b.number))
            break
        state_seen.add(state)

        # Fine ends the piece only after a DC/DS (or in a score without jumps)
        if b.fine and (after_jump or not piece_has_jump):
            break

        # backward repeat (suppressed after DC/DS)
        if b.backward_repeat is not None and not after_jump:
            frame = frame_by_end.get(i)
            if frame is not None:
                fid = id(frame)
                cur = current_pass.get(fid, 1)
                if cur < frame.times:
                    next_pass[fid] = cur + 1
                    started.add(fid)
                    i = frame.start
                    continue
            i += 1
            continue

        # DC / DS — each printed directive fires at most once
        if (b.dc or b.ds) and i not in used_jumps:
            kind = "ds" if b.ds else "dc"
            if kind == "ds" and "segno" not in rm.segnos:
                rm.issues.append(Issue(
                    "JUMP_TARGET_MISSING", "error", None, pass_no, b.number,
                    "D.S. 找不到 Segno 记号", break_after=b.number))
                i += 1
                continue
            if b.d_coda and "coda" not in rm.codas:
                rm.issues.append(Issue(
                    "JUMP_TARGET_MISSING", "error", None, pass_no, b.number,
                    "al Coda 找不到 Coda 记号", break_after=b.number))
                i += 1
                continue
            used_jumps.add(i)
            after_jump = True
            if b.d_coda:
                to_coda_target = rm.codas["coda"]
            i = rm.segnos["segno"] if kind == "ds" else 0
            continue

        # To Coda after an al Coda jump
        if b.to_coda and after_jump and to_coda_target is not None:
            i = to_coda_target
            to_coda_target = None
            continue

        i += 1

    rm.route = visits
    return rm


# --- preprocessing ------------------------------------------------------------

def _index_marks(bars: list[Bar], rm: Roadmap) -> None:
    for b in bars:
        if b.segno:
            if b.segno in rm.segnos:
                rm.issues.append(Issue(
                    "REPEAT_AMBIGUOUS", "error", None, None, b.number,
                    f"重复的 Segno 记号（{b.segno}），跳转目标多解"))
            rm.segnos[b.segno] = b.index
        if b.coda:
            if b.coda in rm.codas:
                rm.issues.append(Issue(
                    "REPEAT_AMBIGUOUS", "error", None, None, b.number,
                    f"重复的 Coda 记号（{b.coda}），跳转目标多解"))
            rm.codas[b.coda] = b.index
    for b in bars:
        if b.ds and not rm.segnos:
            rm.issues.append(Issue(
                "JUMP_TARGET_MISSING", "error", None, None, b.number,
                "D.S. 找不到 Segno 记号"))
        if b.d_coda and not rm.codas:
            rm.issues.append(Issue(
                "JUMP_TARGET_MISSING", "error", None, None, b.number,
                "al Coda 找不到 Coda 记号"))
        if (b.dc or b.ds) and b.fine:
            if not any(x.fine for x in bars[b.index + 1:]):
                rm.issues.append(Issue(
                    "JUMP_TARGET_MISSING", "error", None, None, b.number,
                    "al Fine 找不到后续 Fine 记号"))


def _pair_frames(bars: list[Bar], rm: Roadmap) -> None:
    stack: list[int] = []
    for b in bars:
        if b.forward_repeat is not None:
            stack.append(b.index)
        if b.backward_repeat is not None:
            times = b.backward_repeat
            if stack:
                start = stack.pop()
                if bars[start].forward_repeat is not None:
                    times = bars[start].forward_repeat
            else:
                start = 0  # unpaired backward repeat starts at piece beginning
            rm.frames.append(RepeatFrame(start=start, end=b.index, times=times))


def _build_ending_spans(bars: list[Bar], rm: Roadmap) -> None:
    open_stack: list[EndingSpan] = []
    for b in bars:
        for number, etype, time_only in b.endings:
            if etype == "start":
                sp = EndingSpan(number=number, start=b.index, end=b.index,
                                time_only=time_only)
                open_stack.append(sp)
                rm.endings.append(sp)
            elif etype in ("stop", "discontinue"):
                for sp in reversed(open_stack):
                    if sp.number == number:
                        sp.end = b.index
                        open_stack.remove(sp)
                        break
    for sp in open_stack:
        sp.end = len(bars) - 1
    seen: dict[str, tuple[int, int]] = {}
    for sp in rm.endings:
        prev = seen.get(sp.number)
        if prev is not None and sp.start <= prev[1]:
            rm.issues.append(Issue(
                "REPEAT_AMBIGUOUS", "error", None, None,
                bars[sp.start].number,
                f"第 {sp.number} 房子重叠或未闭合，反复出口多解"))
        seen[sp.number] = (sp.start, sp.end)


def _ending_starting_at(spans: list[EndingSpan], i: int) -> EndingSpan | None:
    for s in spans:
        if s.start == i:
            return s
    return None


def _frame_for_ending(frames: list[RepeatFrame], span: EndingSpan) -> RepeatFrame | None:
    # the section is the latest repeat frame beginning at/before this ending;
    # volta endings may sit just outside the frame's backward-repeat bar
    cands = [f for f in frames if f.start <= span.start]
    return max(cands, key=lambda f: f.start, default=None)


def _ending_matches(span: EndingSpan, entry: int) -> bool:
    if span.time_only:
        wanted: set[int] = set()
        for chunk in span.time_only.replace(" ", "").split(","):
            if "-" in chunk:
                a, z = chunk.split("-", 1)
                wanted.update(range(int(a), int(z) + 1))
            elif chunk:
                wanted.add(int(chunk))
        return entry in wanted
    return any(t.strip().isdigit() and int(t) == entry
               for t in span.number.split(","))
