"""Look of the dashboard: colors for both themes, and small HTML builders.

Tables that carry underwriting figures are written as HTML, because that is the only way to
put an estimate in italics and hang its source on it as a tooltip. Every piece of listing text
that goes into HTML is escaped first.

Colors follow one validated palette. Grades use a single blue ramp, darker (lighter in the
dark theme) for better, so order reads without relying on hue.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from typing import Literal

Mode = Literal["light", "dark"]

INK = {
    "light": {
        "ink": "#0b0b0b",
        "secondary": "#52514e",
        "muted": "#898781",
        "line": "#e1e0d9",
        "axis": "#c3c2b7",
        "surface": "#fcfcfb",
        "series": "#2a78d6",
        "track": "#cde2fb",
        "gray": "#c3c2b7",
    },
    "dark": {
        "ink": "#f0efec",
        "secondary": "#c3c2b7",
        "muted": "#898781",
        "line": "#2c2c2a",
        "axis": "#383835",
        "surface": "#1a1a19",
        "series": "#3987e5",
        "track": "#0d366b",
        "gray": "#52514e",
    },
}
# Grade -> (fill, text on the fill). One hue; the step furthest from the surface is the best.
GRADES = {
    "light": {
        "A": ("#104281", "#ffffff"),
        "B": ("#1c5cab", "#ffffff"),
        "C": ("#2a78d6", "#ffffff"),
        "D": ("#5598e7", "#0b0b0b"),
        "F": ("#86b6ef", "#0b0b0b"),
    },
    "dark": {
        "A": ("#b7d3f6", "#0b0b0b"),
        "B": ("#86b6ef", "#0b0b0b"),
        "C": ("#5598e7", "#0b0b0b"),
        "D": ("#2a78d6", "#ffffff"),
        "F": ("#184f95", "#ffffff"),
    },
}
# Reserved for state, always shown with a word beside them.
STATUS = {"good": "#0ca30c", "warning": "#fab219", "serious": "#ec835a", "critical": "#d03b3b"}
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'
LEGEND = (
    "<em>Italic</em> figures are estimates; upright ones are actual, from the listing or your "
    "own figure. Rest the pointer on a number to see where it came from."
)


def esc(text: object) -> str:
    return html.escape("" if text is None else str(text), quote=True)


def css(mode: Mode) -> str:
    ink = INK[mode]
    return f"""<style>
.df {{ --ink:{ink["ink"]}; --secondary:{ink["secondary"]}; --muted:{ink["muted"]};
  --line:{ink["line"]}; --series:{ink["series"]}; --track:{ink["track"]};
  color:var(--ink); font-family:{FONT}; }}
.df table {{ border-collapse:collapse; width:100%; font-size:0.875rem;
  font-variant-numeric:tabular-nums; }}
.df th {{ text-align:right; font-weight:600; color:var(--secondary); padding:6px 10px;
  border-bottom:1px solid var(--line); white-space:nowrap; }}
.df td {{ text-align:right; padding:6px 10px; border-bottom:1px solid var(--line);
  vertical-align:top; white-space:nowrap; }}
.df .scroll {{ overflow-x:auto; }}
.df th.l, .df td.l {{ text-align:left; }}
.df td.wrap {{ white-space:normal; min-width:16rem; color:var(--secondary); }}
.df td.l.wrap {{ text-align:left; }}
.df tr.total td {{ font-weight:600; border-top:1px solid var(--muted); }}
.df em {{ font-style:italic; }}
.df [title] {{ cursor:help; }}
.df .note {{ color:var(--secondary); font-size:0.8125rem; margin:0 0 6px 0; }}
.df .pill {{ display:inline-block; min-width:1.6em; padding:1px 7px; border-radius:999px;
  font-weight:600; text-align:center; }}
.df .flag {{ color:var(--secondary); font-size:0.8125rem; }}
.df .meter {{ display:grid; grid-template-columns:minmax(9rem,16rem) minmax(6rem,1fr) 5.5rem;
  gap:4px 14px; align-items:center; margin:2px 0 10px 0; }}
.df .meter .name {{ color:var(--ink); }}
.df .meter .bar {{ height:10px; border-radius:5px; background:var(--track); overflow:hidden; }}
.df .meter .bar span {{ display:block; height:100%; background:var(--series);
  border-radius:5px 4px 4px 5px; }}
.df .meter .figure {{ text-align:right; font-variant-numeric:tabular-nums; }}
.df .meter .detail {{ grid-column:1 / -1; color:var(--secondary); font-size:0.8125rem;
  margin-bottom:6px; }}
.df .legend {{ display:flex; flex-wrap:wrap; gap:6px 16px; color:var(--secondary);
  font-size:0.8125rem; margin:6px 0; }}
.df .legend i {{ display:inline-block; width:10px; height:10px; border-radius:50%;
  margin-right:6px; }}
.df .status {{ display:inline-block; width:8px; height:8px; border-radius:50%;
  margin-right:6px; }}
</style>"""


def value(text: str, *, estimated: bool, tip: str = "", flagged: bool = False) -> str:
    """One figure: italic when it is an estimate, with its source as the tooltip."""
    inner = f"<em>{esc(text)}</em>" if estimated else esc(text)
    mark = ' <span title="unknown input: pessimistic stand-in used">!</span>' if flagged else ""
    title = f' title="{esc(tip)}"' if tip else ""
    return f"<span{title}>{inner}</span>{mark}"


def grade_pill(grade: str | None, mode: Mode) -> str:
    if not grade:
        return ""
    fill, text = GRADES[mode].get(grade, GRADES[mode]["F"])
    return f'<span class="pill" style="background:{fill};color:{text}">{esc(grade)}</span>'


def table(
    headers: Sequence[str | tuple[str, str]],
    rows: Sequence[Sequence[str]],
    *,
    left: Sequence[int] = (0,),
    wrap: Sequence[int] = (),
    total_row: int | None = None,
) -> str:
    """An HTML table. Cells are HTML already (build them with ``value`` or ``esc``).
    A header may be (text, tooltip). ``left`` lists the left-aligned columns."""
    head = []
    for index, header in enumerate(headers):
        text, tip = header if isinstance(header, tuple) else (header, "")
        cls = ' class="l"' if index in left else ""
        title = f' title="{esc(tip)}"' if tip else ""
        head.append(f"<th{cls}{title}>{esc(text)}</th>")
    body = []
    for number, row in enumerate(rows):
        cells = []
        for index, cell in enumerate(row):
            classes = [name for name, on in (("l", index in left), ("wrap", index in wrap)) if on]
            cls = f' class="{" ".join(classes)}"' if classes else ""
            cells.append(f"<td{cls}>{cell}</td>")
        row_class = ' class="total"' if number == total_row else ""
        body.append(f"<tr{row_class}>{''.join(cells)}</tr>")
    return (
        f'<div class="scroll"><table><thead><tr>{"".join(head)}</tr></thead>'
        f"<tbody>{''.join(body)}</tbody></table></div>"
    )


def block(mode: Mode, *parts: str) -> str:
    """Wrap HTML parts with the stylesheet, ready for ``st.html``."""
    return f'{css(mode)}<div class="df">{"".join(parts)}</div>'


def note(text_html: str) -> str:
    return f'<p class="note">{text_html}</p>'


def meter_list(items: Sequence[tuple[str, float, float, str]]) -> str:
    """Rows of (name, points, out of, detail), each a bar against its own maximum."""
    parts = ['<div class="meter">']
    for name, points, out_of, detail in items:
        share = 0.0 if out_of <= 0 else max(0.0, min(1.0, points / out_of))
        parts.append(
            f'<div class="name">{esc(name)}</div>'
            f'<div class="bar" title="{esc(detail)}"><span style="width:{share:.1%}"></span></div>'
            f'<div class="figure">{points:.1f} / {out_of:g}</div>'
            f'<div class="detail">{esc(detail)}</div>'
        )
    parts.append("</div>")
    return "".join(parts)


def grade_legend(mode: Mode) -> str:
    items = "".join(
        f'<span><i style="background:{fill}"></i>Grade {grade}</span>'
        for grade, (fill, _) in GRADES[mode].items()
    )
    return f'<div class="legend">{items}</div>'


def status_dot(level: str) -> str:
    color = {"red": STATUS["critical"], "warning": STATUS["warning"]}.get(level, "#898781")
    word = {"red": "Red flag", "warning": "Warning"}.get(level, "Note")
    return f'<span class="status" style="background:{color}"></span><strong>{word}:</strong> '


def money(amount: float | None, *, cents: bool = False) -> str:
    if amount is None:
        return "n/a"
    text = f"${abs(amount):,.2f}" if cents else f"${abs(amount):,.0f}"
    return f"-{text}" if amount < 0 and text.strip("$0,.") else text


def percent(share: float | None, digits: int = 1) -> str:
    return "n/a" if share is None else f"{share * 100:.{digits}f}%"


def ratio(number: float | None, digits: int = 2) -> str:
    return "n/a" if number is None else f"{number:.{digits}f}"
