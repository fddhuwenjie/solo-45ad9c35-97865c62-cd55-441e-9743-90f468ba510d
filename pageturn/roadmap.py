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
    segnos: dict[str, list[int]] = field(default_factory=dict)
    codas: dict[str, list[int]] = field(default_factory=dict)
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
    # 1-based pass currently performed inside each repeat frame
    current_pass: dict[int, int] = {}
    # pending pass parked by printed bar (may cross a skipped volta block)
    pending_at_bar: dict[int, int] = {}

    def span_after(span: EndingSpan) -> int:
        """Bar after an ending: honour a later ending starting where this one stops."""
        later = [s.start for s in rm.endings
                 if s.start >= span.end and s.start > span.start]
        return min(later, default=span.end)

    while 0 <= i < n:
        if pass_no >= MAX_STEPS:
            rm.terminated = False
            rm.issues.append(Issue(
                "JUMP_CYCLE", "error", None, pass_no, rm.mnum(i),
                f"跳转成环：超过 {MAX_STEPS} 个遍次仍未终止",
                break_after=rm.mnum(i)))
            break

        b = bars[i]

        # a repeat jump may land exactly here (including inside an ending)
        landed = pending_at_bar.pop(i, None)

        # frame-start arrival: a pending repeat jump wins, otherwise pass 1
        for f in rm.frames:
            if f.start == i:
                if landed is not None:
                    current_pass[id(f)] = landed
                elif id(f) not in current_pass:
                    current_pass[id(f)] = 1

        # volta gate. On the very first forward arrival the frame is unknown,
        # so ending 1 plays; after a repeat jump the frame's pending pass
        # decides which ending is taken.
        span = _ending_starting_at(rm.endings, i)
        if span is not None:
            frame = _frame_for_ending(rm.frames, span)
            if frame is not None:
                fid = id(frame)
                if landed is not None:
                    current_pass[fid] = landed
                if fid in current_pass:
                    entry = current_pass[fid]
                    if not _ending_matches(span, entry):
                        if landed is not None:
                            pending_at_bar[span_after(span)] = landed
                        i = span_after(span)
                        continue
                else:
                    current_pass[fid] = 1
            else:
                entry = counts[i] + 1
                if not _ending_matches(span, entry):
                    i = span_after(span)
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
                    pending_at_bar[frame.start] = cur + 1
                    i = frame.start
                    continue
            i += 1
            continue

        # DC / DS — each printed directive fires at most once; on a later
        # visit (e.g. while travelling from Segno toward To Coda) fall
        # through so the To Coda / Fine logic below can still run
        if (b.dc or b.ds) and i not in used_jumps:
            kind = "ds" if b.ds else "dc"
            segno_targets = rm.segnos.get("segno", [])
            if kind == "ds" and not segno_targets:
                rm.issues.append(Issue(
                    "JUMP_TARGET_MISSING", "error", None, pass_no, b.number,
                    "D.S. 找不到 Segno 记号", break_after=b.number))
                i += 1
                continue
            if kind == "ds" and len(segno_targets) > 1:
                rm.issues.append(Issue(
                    "REPEAT_AMBIGUOUS", "error", None, pass_no, b.number,
                    f"反复出口多解：遍次 {pass_no} 的 D.S. 对应多个 Segno"
                    f"（小节 {', '.join(rm.mnum(t) for t in segno_targets)}），"
                    "跳转目标不唯一",
                    break_after=b.number))
            if b.d_coda and not rm.codas.get("coda"):
                rm.issues.append(Issue(
                    "JUMP_TARGET_MISSING", "error", None, pass_no, b.number,
                    "al Coda 找不到 Coda 记号", break_after=b.number))
                i += 1
                continue
            used_jumps.add(i)
            after_jump = True
            if b.d_coda:
                to_coda_target = rm.codas["coda"][0]
            i = segno_targets[0] if kind == "ds" else 0
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
            rm.segnos.setdefault(b.segno, []).append(b.index)
        if b.coda:
            rm.codas.setdefault(b.coda, []).append(b.index)
    # structural ambiguity that exists independently of the traversal
    for name, targets in rm.segnos.items():
        if len(targets) > 1:
            for t in targets:
                b = bars[t]
                rm.issues.append(Issue(
                    "REPEAT_AMBIGUOUS", "error", None, None, b.number,
                    f"Segno 记号（{name}）在小节 "
                    f"{', '.join(bars[x].number for x in targets)} 重复出现，"
                    "D.S. 跳转目标多解"))
    for name, targets in rm.codas.items():
        if len(targets) > 1:
            for t in targets:
                b = bars[t]
                rm.issues.append(Issue(
                    "REPEAT_AMBIGUOUS", "error", None, None, b.number,
                    f"Coda 记号（{name}）在小节 "
                    f"{', '.join(bars[x].number for x in targets)} 重复出现，"
                    "al Coda 跳转目标多解"))
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
    """Ending spans are half-open: a stop barline belongs to the next section."""
    stop_bars: dict[str, int] = {}
    open_stack: list[EndingSpan] = []
    for b in bars:
        # left-located stops in this measure end the span before this measure
        for number, etype, time_only, location in b.endings:
            if etype in ("stop", "discontinue") and location.startswith("left"):
                for sp in reversed(open_stack):
                    if sp.number == number:
                        sp.end = b.index
                        open_stack.remove(sp)
                        stop_bars[number] = b.index
                        break
        for number, etype, time_only, location in b.endings:
            if etype == "start":
                sp = EndingSpan(number=number, start=b.index, end=b.index,
                                time_only=time_only)
                open_stack.append(sp)
                rm.endings.append(sp)
            elif etype in ("stop", "discontinue") and not location.startswith("left"):
                # stop on a right barline: the measure belongs to the ending
                for sp in reversed(open_stack):
                    if sp.number == number:
                        sp.end = b.index + 1
                        open_stack.remove(sp)
                        break
    for sp in open_stack:
        sp.end = len(bars)
    seen: dict[str, tuple[int, int]] = {}
    for sp in rm.endings:
        prev = seen.get(sp.number)
        if prev is not None and sp.start < prev[1]:
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
    # the section is the latest repeat frame beginning at/before this ending
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
