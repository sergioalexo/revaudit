"""Small filesystem helpers shared by the exports, reports and index uploads."""

from __future__ import annotations

import os
import threading
from pathlib import Path


def write_atomic(path, data):
    """Write bytes or text to path so a reader only ever sees the old file or
    the complete new one - never half of it.

    Matters most on the shared drive: the exports skip any file that is
    already there, so a PDF cut short by a dropped connection would otherwise
    be trusted as finished forever. The data goes to a temporary file in the
    same folder first, then os.replace() swaps it in, which is atomic on the
    same volume (NTFS and SMB shares included)."""
    path = Path(path)
    if isinstance(data, str):
        data = data.encode("utf-8")
    tmp = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return path
