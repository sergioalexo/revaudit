"""Onshape API-call usage over time, for the chart on the main page.

Every audit run appends one line to usage.jsonl next to the app:
{"ts": "2026-10-13T14:02:11", "calls": 188, "audit": 163, "exports": 25}.
The first time the stats are read and the file doesn't exist, it is seeded
from the usage already stored in saved reports, so history isn't lost.
"""

from __future__ import annotations

import html
import json
import re
import sys
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PATH = HERE / "usage.jsonl"
DAYS = 30                       # days shown in the chart
_lock = threading.Lock()


def _append(rows):
    with open(PATH, "a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def record(api, when=None):
    """Append one run's usage (the dict from report.api_usage). Never raises:
    stats must not be the reason an audit fails."""
    try:
        row = {"ts": (when or datetime.now()).strftime("%Y-%m-%dT%H:%M:%S"),
               "calls": int(api.get("calls", 0)),
               "audit": int(api.get("auditCalls", api.get("calls", 0))),
               "exports": int(api.get("exportCalls", 0))}
        if "stepCalls" in api:
            row["pdf"] = int(api["pdfCalls"])
            row["step"] = int(api["stepCalls"])
        with _lock:
            _append([row])
    except Exception as exc:
        print(f"  usage stats not recorded: {exc}", file=sys.stderr)


def _seed_from_reports():
    from report import load_report
    from report_store import list_reports
    rows = []
    for p in list_reports(None):
        m = re.match(r"revaudit-(\d{8})-(\d{6})", p.name)
        if not m:
            continue
        try:
            api = load_report(p).get("api") or {}
            when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except (OSError, ValueError):
            continue
        if api.get("calls") is None:
            continue
        rows.append({"ts": when.strftime("%Y-%m-%dT%H:%M:%S"), "calls": int(api["calls"]),
                     "audit": int(api.get("auditCalls", api["calls"])),
                     "exports": int(api.get("exportCalls", 0))})
    rows.sort(key=lambda r: r["ts"])
    return rows


def load():
    """All recorded runs, oldest first."""
    with _lock:
        if not PATH.is_file():
            try:
                _append(_seed_from_reports())
                PATH.touch()
            except Exception as exc:
                print(f"  usage stats seed failed: {exc}", file=sys.stderr)
                return []
        rows = []
        try:
            for line in PATH.read_text(encoding="utf-8").splitlines():
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
        except OSError:
            pass
    return rows


def _fmt_day(iso):
    d = date.fromisoformat(iso)
    return f"{d:%a} {d.day} {d:%b}"


def _split(row):
    """(audit, pdf, step, unsplit exports) for one run. Runs recorded before
    the PDF/STEP split existed keep all their export calls in 'unsplit'."""
    exports = int(row.get("exports", 0))
    if "pdf" in row:
        return int(row.get("audit", 0)), int(row["pdf"]), int(row.get("step", 0)), 0
    return int(row.get("audit", row.get("calls", 0))), 0, 0, exports


# bottom-to-top stacking order, CSS class, legend label
KINDS = (("e", "pdf", "Drawing PDF exports"), ("s", "step", "STEP exports"),
         ("u", "other", "Exports (not split)"), ("a", "audit", "Audit lookups"))


def stats_html(rows=None, today=None):
    """The 'API usage' block: totals by kind, plus a stacked bar per day for
    the last DAYS days. Hovering a bar lists that day's numbers."""
    rows = load() if rows is None else rows
    if not rows:
        return ""
    today = today or date.today()
    per_day = {}
    for r in rows:
        d = per_day.setdefault(str(r.get("ts", ""))[:10],
                               {"audit": 0, "pdf": 0, "step": 0, "other": 0, "runs": 0})
        audit, pdf, step, other = _split(r)
        d["audit"] += audit
        d["pdf"] += pdf
        d["step"] += step
        d["other"] += other
        d["runs"] += 1
    empty = {"audit": 0, "pdf": 0, "step": 0, "other": 0, "runs": 0}
    sums = {k: sum(d[k] for d in per_day.values()) for k in ("audit", "pdf", "step", "other")}
    grand = sum(sums.values())
    first = min(per_day)
    days = [(today - timedelta(days=i)).isoformat() for i in range(DAYS - 1, -1, -1)]

    def day_total(d):
        return d["audit"] + d["pdf"] + d["step"] + d["other"]

    peak = max((day_total(per_day.get(d, empty)) for d in days), default=0) or 1
    bars = []
    for day in days:
        d = per_day.get(day, empty)
        total = day_total(d)
        if total:
            lines = "".join(f"<i class='k {cls}'></i>{label} {d[key]:,}<br>"
                            for cls, key, label in reversed(KINDS) if d[key])
            tip = (f"<span class='tip'><b>{html.escape(_fmt_day(day))}</b><br>"
                   f"{total:,} calls &middot; {d['runs']} run{'s' if d['runs'] != 1 else ''}<br>"
                   f"{lines}</span>")
        else:
            tip = f"<span class='tip'><b>{html.escape(_fmt_day(day))}</b><br>no calls</span>"
        segs = "".join(f"<i class='{cls}' style='height:{d[key] / peak * 100:.1f}%'></i>"
                       for cls, key, _ in KINDS if d[key])
        bars.append(f"<div class='bar'>{segs}{tip}</div>")
    last = sum(day_total(per_day.get(d, empty)) for d in days)
    legend = "".join(f"<span><i class='k {cls}'></i>{label} <b>{sums[key]:,}</b></span>"
                     for cls, key, label in reversed(KINDS) if sums[key])
    return (
        "<div class='usage'><b>Onshape API usage</b>"
        "<div class='usage-totals'>"
        f"<span><b>{grand:,}</b> calls total since {html.escape(first)}</span>"
        f"<span><b>{last:,}</b> in the last {DAYS} days</span>"
        f"<span><b>{len(rows):,}</b> runs</span></div>"
        f"<div class='bars'>{''.join(bars)}</div>"
        f"<div class='usage-axis'><span>{html.escape(days[0])}</span>"
        f"<span>peak {peak:,}/day</span><span>today</span></div>"
        f"<div class='usage-legend'>{legend}"
        f"<span class='sum'>Total <b>{grand:,}</b></span></div></div>")
