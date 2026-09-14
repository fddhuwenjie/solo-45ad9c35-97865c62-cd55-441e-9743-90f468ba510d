"""Tiny SVG diff of the existing vs proposed pagination (stdlib strings)."""
from __future__ import annotations

from xml.sax.saxutils import escape

from .model import PartReport


def render_svg(part_id: str, report: PartReport, baseline: list[str],
               proposed: dict[str, str]) -> str:
    rows = report.pages
    row_h, w = 34, 920
    h = 70 + row_h * max(1, len(rows))
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" font-family="Helvetica,Arial,sans-serif" font-size="13">',
        f'<rect width="100%" height="100%" fill="#ffffff"/>',
        f'<text x="16" y="26" font-size="16" font-weight="bold">'
        f'{escape(report.name or part_id)} — 分页差异</text>',
        '<line x1="16" y1="36" x2="904" y2="36" stroke="#888"/>',
        _cell(16, 40, 60, "页", bold=True),
        _cell(90, 40, 200, "现有断点 (小节)", bold=True),
        _cell(330, 40, 200, "建议移动到", bold=True),
        _cell(560, 40, 110, "最短空档(秒)", bold=True),
        _cell(700, 40, 180, "判定", bold=True),
    ]
    for i, p in enumerate(rows):
        y = 40 + (i + 1) * row_h
        color = "#1a7f37" if p.feasible else "#c0392b"
        mark = "✓ 可演奏" if p.feasible else "✗ 不可演奏"
        if p.is_tail:
            mark = "末页容量" + (" ✓" if p.feasible else " ✗")
        if p.locked:
            mark += "（锁定）"
        move = p.move_to or "—"
        if p.move_to:
            move = f"{p.move_to} ◀"
        gap = "—" if p.gap_seconds is None else f"{p.gap_seconds:.2f}"
        break_label = "末页" if p.is_tail or p.break_after is None else p.break_after
        parts += [
            f'<line x1="16" y1="{y + row_h - 8}" x2="904" y2="{y + row_h - 8}" '
            'stroke="#e3e3e3"/>',
            _cell(16, y, 60, str(p.page)),
            _cell(90, y, 200, escape(break_label)),
            _cell(330, y, 200, escape(move),
                  fill="#0b63ce" if p.move_to else None, bold=bool(p.move_to)),
            _cell(560, y, 110, gap),
            _cell(700, y, 200, mark, fill=color),
        ]
    parts.append("</svg>")
    return "\n".join(parts)


def _cell(x: int, y: int, _w: int, text: str, fill: str | None = None,
          bold: bool = False) -> str:
    fw = ' font-weight="bold"' if bold else ""
    fc = f' fill="{fill}"' if fill else ""
    return (f'<text x="{x}" y="{y + 18}"{fw}{fc}>'
            f'{escape(text)}</text>')
