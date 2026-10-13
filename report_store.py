"""Where reports live, what they are called, and which one already answers
a question.

Reports are saved by the web UI and the CLI into the configured report
folder (usually a share) with the app folder as fallback. This module names
them, finds them again, lists the recent ones, and - before an audit spends
its API calls - looks for a saved report that already covers an assembly at
its current revision.
"""

from __future__ import annotations

import re
import secrets
import sys
from datetime import datetime
from pathlib import Path

import config
from onshape_client import ET_ASSEMBLY, OnshapeError, current_revision
from report import load_report, report_name_part, save_report

HERE = Path(__file__).resolve().parent

# A report is stored as revaudit-<timestamp>-<assemblies>-<random token>.html
# - the finished page with the audit data embedded in it - and re-rendered
# from that data whenever someone opens /report/<that id>. The token (exactly
# 16 url-safe chars, always last) is what makes a report link safe to hand
# out without also handing out the site password: nobody can guess it, so
# holding the link is proof enough that you were given it. The same id with
# .json appended returns just the data. Names from before the assemblies
# were in them (revaudit-<timestamp>-<token>) still fit.
TOKEN_CHARS = 16                                  # secrets.token_urlsafe(12)
SHARED_REPORT_RE = (r"revaudit-\d{8}-\d{6}-(?:[A-Za-z0-9.+-]+-)?[A-Za-z0-9_-]{16}"
                    r"(?:\.json|\.html)?")

# revaudit-<timestamp>[_<assemblies>] with no token - reports the CLI writes
# into the shared folder, and web reports from before link sharing existed.
# They carry no secret of their own, so they still need the site password
# rather than being reachable by anyone who finds the filename. The '_' right
# after the timestamp is what keeps a CLI name from ever looking like a
# tokened one: a shared name always has '-' there.
LEGACY_REPORT_RE = r"revaudit-\d{8}-\d{6}(?:_[A-Za-z0-9.+-]+)?(?:\.json|\.html)?"

# .json: files from the few hours the data was stored bare; still readable
REPORT_EXTS = (".html", ".json")


def new_report_id(part_numbers=()):
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    token = secrets.token_urlsafe(12)
    assert len(token) == TOKEN_CHARS
    return f"revaudit-{stamp}-{report_name_part(part_numbers)}-{token}"


def report_id(name):
    """revaudit-...json / .html -> the bare report id used in /report/ links."""
    for ext in REPORT_EXTS:
        if name.endswith(ext):
            return name[:-len(ext)]
    return name


def report_label(name):
    """Short label for the 'recent reports' list: the assemblies and the
    time, never the random token, which would be meaningless clutter."""
    rid = report_id(name)
    m = re.fullmatch(r"revaudit-(\d{8}-\d{6})(?:-(.+))?-[A-Za-z0-9_-]{16}", rid)
    if not m:
        m = re.fullmatch(r"revaudit-(\d{8}-\d{6})(?:_(.+))?", rid)
    if not m:
        return name
    stamp, pns = m.group(1), m.group(2)
    when = f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]} {stamp[9:11]}:{stamp[11:13]}"
    return f"{pns.replace('+', ' + ')} \u00b7 {when}" if pns else when


def report_dirs():
    """Every folder a report might live in: the configured one (usually a K:
    share, so the audit history is browsable by anyone), plus this app's own
    folder - where older reports sit, and where writes fall back if the
    configured folder is unreachable."""
    dirs = []
    try:
        configured = Path(config.report_dir())
        if configured.resolve() != HERE:
            dirs.append(configured)
    except (OSError, ValueError):
        pass
    dirs.append(HERE)
    return dirs


def write_report(rid, data):
    """Save a report as <rid>.html (data embedded), preferring the configured
    folder, falling back to the app folder if that can't be written. Returns
    the Path actually used. Raises OSError when no folder takes it."""
    last = None
    for target_dir in report_dirs():
        try:
            return save_report(target_dir / f"{rid}.html", data)
        except OSError as exc:
            last = exc
            print(f"  report folder {target_dir} not writable ({exc}); trying next",
                  file=sys.stderr)
    raise OSError(f"could not write the report to any folder: {last}")


def find_report(name):
    """The file behind `name` - a full revaudit-*.html / .json filename, or a
    bare report id (.html preferred) - wherever it lives, or None."""
    names = [name] if name.endswith(REPORT_EXTS) else [name + e for e in REPORT_EXTS]
    for d in report_dirs():
        for candidate_name in names:
            candidate = d / candidate_name
            try:
                if candidate.is_file() and candidate.parent == d:
                    return candidate
            except OSError:
                continue
    return None


def _mtime(path):
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def newest_first(paths, limit=None):
    """paths sorted newest first; one that vanished meanwhile sorts last
    instead of raising."""
    ordered = sorted(paths, key=_mtime, reverse=True)
    return ordered if limit is None else ordered[:limit]


def list_reports(limit=5):
    """The most recent reports across every report folder, newest first, one
    entry per report id (the JSON-file interlude left .json and .html twins)."""
    seen = {}
    for d in report_dirs():
        for ext in REPORT_EXTS:
            try:
                for p in d.glob(f"revaudit-*{ext}"):
                    rid = report_id(p.name)
                    if rid not in seen or _mtime(p) > _mtime(seen[rid]):
                        seen[rid] = p
            except OSError:
                continue
    return newest_first(seen.values(), limit)


# --------------------------------------------------------------------------
# "this was already audited" check
# --------------------------------------------------------------------------
#
# An audit is an Onshape call per part plus a few per assembly, and it is
# usually asked for again long before anything it looks at has moved. So before running, RevAudit asks one question
# ("what revision is this assembly at?") and looks for a report that already
# covers that exact revision. If it finds one, it offers that report instead
# of spending the calls, and the person decides which they want.

def report_pn_parts(rid):
    """(the assembly numbers written into a report id, whether any were left
    out of the name). A report name holds at most three, then '+<n>more' -
    and a report from before names carried assemblies holds none at all,
    which counts as "left out": such a report might cover anything."""
    m = re.fullmatch(r"revaudit-\d{8}-\d{6}(?:-(.+))?-[A-Za-z0-9_-]{16}", rid)
    if not m:
        m = re.fullmatch(r"revaudit-\d{8}-\d{6}(?:_(.+))?", rid)
    if not m or not m.group(1):
        return [], True
    parts = m.group(1).split("+")
    truncated = bool(re.fullmatch(r"\d+more", parts[-1]))
    return (parts[:-1] if truncated else parts), truncated


def report_may_cover(rid, fragment):
    """Could this report hold an audit of `fragment` (a part number as
    report_name_part writes it)? A filename test only - cheap enough to run
    over a whole folder, and it is what keeps the real check down to a
    handful of files instead of every report ever written."""
    parts, truncated = report_pn_parts(rid)
    return truncated or fragment in parts


def result_checks(result):
    """The optional extras one audited assembly in a saved report came with -
    'DXF', 'SAT', 'drawing PDFs', 'STEP' - so a previous report can say what
    it did and did not cover."""
    names = list((result.get("findings") or {}).get("files") or {})
    if result.get("drawingExport"):
        names.append("drawing PDFs")
    if result.get("stepExport"):
        names.append("STEP")
    return names


# Reports live on a share and carry their whole data block, so each one is
# parsed once and kept by (mtime, size) - a saved report is never edited in
# place, so that pair changing means it really is a different file.
_coverage_cache = {}
_COVERAGE_CACHE_MAX = 500


def report_coverage(path):
    """{assembly part number: {"revision", "generated", "checks"}} for one
    saved report. An assembly that never resolved is left out - nothing was
    audited, so nothing is covered. An unreadable file gives {}."""
    try:
        stat = path.stat()
    except OSError:
        return {}
    stamp = (stat.st_mtime, stat.st_size)
    hit = _coverage_cache.get(str(path))
    if hit and hit[0] == stamp:
        return hit[1]
    try:
        data = load_report(path)
    except (OSError, ValueError):          # unreadable, or not a report at all
        data = None
    coverage = {}
    for result in (data or {}).get("results") or []:
        pn = result.get("partNumber")
        revision = (result.get("assembly") or {}).get("revision")
        if not pn or not revision:
            continue
        coverage[str(pn)] = {
            "revision": str(revision),
            "generated": (data or {}).get("generated") or "",
            "checks": result_checks(result),
        }
    if len(_coverage_cache) > _COVERAGE_CACHE_MAX:
        _coverage_cache.clear()
    _coverage_cache[str(path)] = (stamp, coverage)
    return coverage


def find_prior_report(part_number, revision, allowed_ids=None, scan_limit=40):
    """The newest saved report that already audited this assembly at exactly
    this revision - {"id", "label", "path", "revision", "generated",
    "checks"} - or None.

    allowed_ids, when given, restricts the search to those report ids: the
    OAuth app only ever shows someone their own reports, so it must not
    offer one it would then refuse to open.
    """
    if not revision:
        return None
    fragment = report_name_part([part_number])
    allowed = None if allowed_ids is None else set(allowed_ids)
    newest = {}
    for d in report_dirs():
        for ext in REPORT_EXTS:
            try:
                candidates = list(d.glob(f"revaudit-*{ext}"))
            except OSError:
                continue
            for path in candidates:
                rid = report_id(path.name)
                if allowed is not None and rid not in allowed:
                    continue
                if not report_may_cover(rid, fragment):
                    continue
                if rid not in newest or _mtime(path) > _mtime(newest[rid]):
                    newest[rid] = path
    for path in newest_first(newest.values(), scan_limit):
        entry = report_coverage(path).get(str(part_number))
        if entry and entry["revision"] == str(revision):
            return {"id": report_id(path.name), "label": report_label(path.name),
                    "path": str(path), **entry}
    return None


def current_assembly_revision(client, company_id, part_number):
    """The revision an audit would report on, from the single revision
    lookup that audit starts with anyway - the client remembers it, so
    asking here and then running costs no extra call. A lookup that has
    nothing for the part number gives None."""
    revs = client.revisions(company_id, part_number, ET_ASSEMBLY)
    if not revs:
        return None
    return (current_revision(revs) or revs[0]).get("revision")


def prior_runs(client, company_id, names, allowed_ids=None):
    """One entry per assembly: {"name", "revision", "prior", "error"}. Costs
    a single (memoised) revision lookup each - no BOM, no per-part sweep."""
    entries = []
    for name in names:
        try:
            revision = current_assembly_revision(client, company_id, name)
        except OnshapeError as exc:
            # the shortcut check must never be what stops a requested run
            entries.append({"name": name, "revision": None, "prior": None,
                            "error": str(exc)})
            continue
        entries.append({
            "name": name,
            "revision": revision,
            "prior": find_prior_report(name, revision, allowed_ids),
            "error": None,
        })
    return entries
