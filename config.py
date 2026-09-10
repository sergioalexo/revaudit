"""revaudit.conf loader - plain-text operational settings, editable in Notepad.

Layered lookup for every value: revaudit.conf  ->  environment variable  ->
built-in default. So an existing setup with everything in .env keeps working
untouched; revaudit.conf just gives non-secret settings (ports, folders, the
advertised IP) a friendlier home than environment variables.

Secrets (Onshape keys, the site password) stay in .env - never here.
"""

from __future__ import annotations

import configparser
import os
import socket
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONF_PATH = HERE / "revaudit.conf"

# No built-in paths: the folders are site-specific, so they come from
# revaudit.conf (or the REVAUDIT_*_DIR env vars). Blank = that check/export
# is off until configured.
_FOLDER_DEFAULTS = {"dxf": "", "sat": "", "pdf": "", "step": ""}

_parser = None


def _load():
    global _parser
    if _parser is None:
        _parser = configparser.ConfigParser()
        if CONF_PATH.is_file():
            try:
                _parser.read(CONF_PATH, encoding="utf-8")
            except configparser.Error:
                pass  # a broken conf file falls back to env/defaults, not a crash
    return _parser


def get(section, key, env_var=None, default=None):
    """revaudit.conf [section] key, else the env var, else default. Blank
    values in the conf file count as 'not set' and fall through."""
    parser = _load()
    if parser.has_option(section, key):
        value = parser.get(section, key).strip()
        if value:
            return value
    if env_var:
        value = os.environ.get(env_var)
        if value:
            return value.strip()
    return default


def folder(name):
    """Production-file folder for 'dxf' / 'sat' / 'pdf' / 'step'."""
    return get("folders", name, f"REVAUDIT_{name.upper()}_DIR", _FOLDER_DEFAULTS[name])


def report_dir():
    """Where HTML reports are saved. An explicitly blank line in the conf
    means 'keep them in the app folder' (not 'off'), so it is handled
    separately from folder() - which treats blank as 'skip'."""
    parser = _load()
    if parser.has_option("folders", "report"):
        raw = parser.get("folders", "report").strip()
        return raw if raw else str(HERE)
    env = os.environ.get("REVAUDIT_REPORT_DIR")
    if env:
        return env.strip()
    return str(HERE)          # not configured -> keep reports in the app folder


def bind_host():
    """What the web server listens on. 0.0.0.0 = the LAN (a password in .env
    is then required); 127.0.0.1 = this machine only."""
    return get("network", "bind_host", "REVAUDIT_BIND_HOST", "0.0.0.0")


def port():
    raw = get("network", "port", "REVAUDIT_PORT", "8000")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 8000


def advertised_address():
    """The address to show people in the startup banner. None = auto-detect
    and list every candidate, so a changed DHCP lease is obvious."""
    return get("network", "advertised_address", "REVAUDIT_ADVERTISED_ADDRESS", None)


def detect_lan_ips():
    """Best-guess LAN IPv4 addresses of this machine, for the startup banner."""
    ips = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))          # no packet sent, just picks a route
        ips.append(probe.getsockname()[0])
        probe.close()
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def ensure_from_example():
    """Copy revaudit.conf.example -> revaudit.conf if the real one is missing,
    so `launch.bat` alone (without running install.bat first) still works.
    Returns True if it created the file."""
    example = HERE / "revaudit.conf.example"
    if CONF_PATH.is_file() or not example.is_file():
        return False
    CONF_PATH.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    return True
