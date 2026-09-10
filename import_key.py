#!/usr/bin/env python3
"""
Import Onshape credentials from a text file into .env.

    py -3.13 import_key.py                # reads access.txt, then env.txt
    py -3.13 import_key.py env.txt        # or name the file

Handles either shape:

  KEY=VALUE lines            merged into .env, keeping anything already there
    ONSHAPE_OAUTH_CLIENT_ID=...
    ONSHAPE_OAUTH_CLIENT_SECRET=...

  two bare keys              access key first, secret second
    <access key>
    <secret key>

Secret values are never printed - only their lengths. Non-secret settings such
as the redirect URI are shown so you can check them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENV = HERE / ".env"

TOKEN = r"[A-Za-z0-9+/=_\-]{16,}"

KNOWN = [
    "ONSHAPE_ACCESS_KEY",
    "ONSHAPE_SECRET_KEY",
    "ONSHAPE_OAUTH_CLIENT_ID",
    "ONSHAPE_OAUTH_CLIENT_SECRET",
    "ONSHAPE_OAUTH_REDIRECT_URI",
    "ONSHAPE_OAUTH_SCOPE",
    "ONSHAPE_BASE_URL",
    "ONSHAPE_COMPANY_ID",
    "REVAUDIT_PASSWORD",
    "REVAUDIT_INDEX_TOKEN",
    "REVAUDIT_DXF_DIR",
]

# printed in full - knowing these leaks nothing
PUBLIC = {
    "ONSHAPE_OAUTH_REDIRECT_URI",
    "ONSHAPE_OAUTH_SCOPE",
    "ONSHAPE_BASE_URL",
    "ONSHAPE_COMPANY_ID",
    "REVAUDIT_DXF_DIR",
}


def parse_pairs(text):
    """KEY=VALUE lines for settings we recognise."""
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().upper()
        if key in KNOWN:
            found[key] = value.strip().strip('"').strip("'")
    return found


def parse_bare_keys(text):
    """Two bare tokens: access key first, secret key second."""
    access = secret = None
    for label, value in re.findall(
        r"^\s*([A-Za-z_ ]*?(?:access|secret)[A-Za-z_ ]*?)\s*[:=]\s*(" + TOKEN + r")\s*$",
        text, re.IGNORECASE | re.MULTILINE,
    ):
        if "secret" in label.lower():
            secret = value
        elif "access" in label.lower():
            access = value
    if access and secret:
        return {"ONSHAPE_ACCESS_KEY": access, "ONSHAPE_SECRET_KEY": secret}

    bare = [l.strip() for l in text.splitlines() if re.fullmatch(TOKEN, l.strip())]
    if len(bare) >= 2:
        return {"ONSHAPE_ACCESS_KEY": bare[0], "ONSHAPE_SECRET_KEY": bare[1]}
    return {}


def merge_env(values):
    """Write values into .env, replacing those keys and keeping everything else."""
    lines = ENV.read_text(encoding="utf-8").splitlines() if ENV.is_file() else []
    remaining = dict(values)
    out = []
    for line in lines:
        match = re.match(r"^\s*([A-Z_]+)\s*=", line)
        key = match.group(1) if match else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    for key, value in remaining.items():
        out.append(f"{key}={value}")
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")


def main(argv):
    if len(argv) > 1:
        sources = [Path(argv[1])]
    else:
        sources = [p for p in (HERE / "access.txt", HERE / "env.txt") if p.is_file()]
    sources = [p for p in sources if p.is_file()]
    if not sources:
        print("Nothing to import. Pass a file, or put access.txt / env.txt here.",
              file=sys.stderr)
        return 2

    collected = {}
    for source in sources:
        text = source.read_text(encoding="utf-8", errors="replace")
        values = parse_pairs(text) or parse_bare_keys(text)
        if not values:
            print(f"Could not find any recognised settings in {source.name}.",
                  file=sys.stderr)
            continue
        print(f"{source.name}:")
        for key in sorted(values):
            shown = values[key] if key in PUBLIC else f"<{len(values[key])} chars>"
            print(f"  {key} = {shown}")
        collected.update(values)

    if not collected:
        return 1

    merge_env(collected)
    print(f"\nMerged into {ENV}")

    if "ONSHAPE_OAUTH_CLIENT_ID" in collected:
        print("\nStart the OAuth app:  py -3.13 oauth_app.py --open")
    elif "ONSHAPE_ACCESS_KEY" in collected:
        print("\nTest it:  py -3.13 revaudit.py ASM-12345")
    print("Then delete " + ", ".join(p.name for p in sources)
          + " - the values now live in .env.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
