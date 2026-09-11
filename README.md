# RevAudit

Command-line tool that runs the release / drawing / DXF checks against Onshape's REST
API and writes a self-contained HTML report.

For each assembly part number it reports:

| Check | Question it answers |
|---|---|
| **Parts with no drawing** | Was a part released without any drawing at all? |
| **Drawing behind part** | Was the part revised without re-releasing its drawing? |
| **Mixed-revision part numbers** | Does one part number appear twice in the BOM at two different revisions? |
| **Obsolete BOM lines** | Which lines are obsolete, and which have no newer line to replace them? |
| **Stale BOM revision** | Does the BOM pin a revision older than the current part? |
| **Lines with no released revision** | Which lines aren't revision-managed at all? |
| **Workspace drift** | Has the live workspace moved away from the released version? |
| **DXF coverage** | Does every sheet part have a DXF in the production folder? |

## Setup

Needs Python 3.9+ and nothing else — standard library only, no `pip install`.

### Windows: run `install.bat`

Double-click **`install.bat`** (and once more as administrator, if you want other
computers on the network to reach it). It:

- finds your Python and checks the version
- creates `revaudit.conf` (plain-text settings) and `.env` (secrets) from templates
- puts a **RevAudit** shortcut on your Desktop
- asks whether RevAudit should **start with Windows** (a shortcut in your Startup
  folder, so it comes back after a reboot) — change your mind any time with
  `autostart.bat` (`on` / `off` / `status`, or double-click it to toggle)
- opens the firewall for port 8000 (the admin part)
- opens `revaudit.conf` in Notepad so you can set the address and folders

Then put your Onshape API key into `.env` — `py -3.13 import_key.py` if you saved it
to a file, or paste it into `.env` by hand.

### Staying up to date

If this folder is a `git clone`, `launch.bat` checks GitHub on every start (and every
reboot, via the Startup shortcut). When `origin` is one or more commits ahead it
fast-forwards to the latest code and relaunches itself before starting the server.
`revaudit.conf` and `.env` are never touched — they're git-ignored, so your local
settings survive every update.

- It only ever fast-forwards. If you've edited a tracked file locally the pull is
  skipped and your version keeps running.
- Offline or GitHub unreachable: it says so and starts the version you have.
- Set the environment variable `REVAUDIT_NO_UPDATE` to any value to turn it off.

### Windows: install with Scoop

```powershell
scoop bucket add sergioalexo https://github.com/sergioalexo/scoop-bucket
scoop install revaudit
```

This pulls in Python if you don't have it and puts four commands on your PATH:
`revaudit` (CLI), `revaudit-serve` (single-user web UI), `revaudit-oauth`
(multi-user app), and `revaudit-launch` (the double-click launcher).

Your `.env` and `revaudit.conf` are **persisted** by Scoop — `scoop update revaudit`
replaces the code but leaves your API key, folders and port alone. First install
seeds both files from the templates and points `[folders] report` at a persisted
folder so audit history survives updates too.

```powershell
scoop prefix revaudit   # open this folder, put your Onshape key in .env
revaudit-serve
```

The git-clone method above and the Scoop package are independent — pick one per
machine. Under Scoop, `scoop update` is the update path (`launch.bat`'s own
git auto-update notices there's no `.git` and skips itself).

### Any OS: by hand

1. Create an API key at <https://dev-portal.onshape.com> → **API keys** → **Create new
   API key**. Read scopes are sufficient; this tool never writes to Onshape.
2. Copy `.env.example` to `.env` and paste your key in; copy `revaudit.conf.example`
   to `revaudit.conf`.

```bash
cp .env.example .env
cp revaudit.conf.example revaudit.conf
```

3. `python3 serve.py` (or `py -3.13 serve.py` on Windows).

> Onshape rejects write calls that ride on a browser session cookie, and read calls from
> a cookie only work inside the browser. An API key is the only way to do this from a
> script. The `.env` file stays on your machine — nothing is transmitted anywhere except
> to Onshape itself.

### `revaudit.conf` — the settings you'll actually change

Plain text, safe to read, opens in Notepad. Restart RevAudit after editing.

| Setting | What it does |
|---|---|
| `advertised_address` | The address shown in the startup banner. **Blank = auto-detect** and list every candidate, so a changed DHCP lease is obvious. Set it to a fixed value once this machine has a DHCP reservation or static IP. |
| `bind_host` | `0.0.0.0` = reachable from the LAN (needs a password in `.env`); `127.0.0.1` = this machine only. |
| `port` | Default `8000`. |
| `[folders]` dxf / sat / pdf / step | Where the production files live. Prefer a UNC path (`\\server\share\...`) over a mapped drive (`K:`) if RevAudit runs at logon — drive letters only exist while you're signed in. |
| `[folders]` report | Where finished HTML reports are saved, so anyone with drive access can browse the audit history. Blank = keep them in the app folder. If the folder is unreachable when a report is written, that one falls back to the app folder. |

Every value also accepts an environment variable (`REVAUDIT_BIND_HOST`, `REVAUDIT_DXF_DIR`, …)
and falls back to a built-in default, so an old `.env`-only setup keeps working untouched.

> **"It stopped working for coworkers"** — check `revaudit.conf`'s address against what
> RevAudit's own window shows on startup. DHCP hands this machine a new IP now and then;
> that's the usual cause.

## Usage

Audit one assembly, check DXFs, open the report:

```bash
py -3.13 revaudit.py ASM-12345 --dxf-dir "<share>\DXF FILES" --open
```

Audit several at once into a single report:

```bash
py -3.13 revaudit.py ASM-12345 ASM-12346 ASM-12347 --dxf-dir "<share>\DXF FILES" -o audit.html
```

Skip the DXF check entirely (Onshape checks only):

```bash
py -3.13 revaudit.py ASM-12345
```

### Options

| Flag | Meaning |
|---|---|
| `--dxf-dir PATH` | Folder to check for DXF files. Omit to skip the DXF check. |
| `--dxf-recursive` | Search that folder's subfolders too. Off by default. |
| `--dxf-ext .dxf,.dwg` | Extensions to index. Default `.dxf`. |
| `--material-regex` | Which materials require a DXF. Default `SHEET`. |
| `--sat-dir PATH` | Folder to check for SAT files. Omit to skip the SAT check. |
| `--sat-recursive` | Search that folder's subfolders too. Off by default. |
| `--sat-ext .sat` | Extensions to index. Default `.sat`. |
| `--sat-material-regex` | Which materials require a SAT file. Default `TUBE`. |
| `--export-drawings [DIR]` | Export a PDF of every released drawing (see below). Omit to skip. |
| `--drawing-workers N` | Parallel PDF exports, default 4. |
| `--export-step [DIR]` | Export each assembly's 3D geometry as STEP (see below). Omit to skip. |
| `-o, --output FILE` | Report path. Without it, a timestamped file goes to `[folders] report` in `revaudit.conf`, or the app folder if that's unreachable or unset. |
| `--open` | Open the finished report in your browser. |
| `--base-url URL` | Onshape URL, if not set in `.env`. |
| `--company-id ID` | Skip company auto-detection. |
| `--workers N` | Parallel API lookups, default 6. Lower it if you hit rate limits. |
| `--no-deep` | Skip the extra element-type sweep on parts that appear to have no drawing. |
| `-v` | Log every API call. |

## How the checks work

**"Released" means a non-obsolete revision exists** in the company revision registry. In
Onshape a revision is only created when a release completes, so the presence of one *is*
the released state. The `STATE` column in a BOM is not sufficient on its own — it
describes the revision the BOM happens to reference, not whether a drawing exists.

**Part revisions and drawing revisions are queried separately** (`elementType=0` and
`elementType=2`). This is what surfaces a part sitting at rev C whose drawing never moved
past rev B — a mismatch the BOM view will not show you.

**Parts that appear to have no drawing get a second pass** across the other element types
(assembly, blob, application) before being reported, so a drawing filed unusually isn't
mistaken for a missing one. Turn this off with `--no-deep`.

**DXF matching is case-insensitive with digit boundaries**, so `PRT-12` never matches
`PRT-123.dxf`. A part is only expected to have a DXF when its material matches
`--material-regex`; wood, tube and bar stock are reported separately rather than as gaps.

**A part matching more than one file gets its own list** — `PRT-1234.dxf` plus a
`PRT-1234-X4.dxf` nest, or a `-FIXTURE` companion drawing. Not flagged as a problem:
the extra is usually a real variant. But a stale duplicate from an old revision looks
the same until someone reads the filenames, so RevAudit puts them in front of you.
Same for SAT.

## PDF drawing export

`--export-drawings [DIR]` on the CLI, or the "PDF export folder" field in the web UI,
exports a PDF of every drawing in the audited assembly — its own, plus every part that
has one — into that folder.

```bash
py -3.13 revaudit.py ASM-12345 --export-drawings "<share>\PDF FILES" --drawing-workers 6
```

**The destination is a real, permanent archive, not a one-off dump** — files are saved
flat, named exactly like the DXF/SAT folders (`<part number>_Rev<rev>.pdf`, and the
assembly's own drawing as `<assembly number>_Rev<rev>_ASSEMBLY.pdf`), so it builds up
into a shared PDF library the same way `DXF FILES`/`SAT FILES` already work. Auditing
the same part in two different assemblies just re-saves the same file - harmless, and a
free dedup. Default location: `REVAUDIT_PDF_DIR` in `.env` / `[folders] pdf` in
`revaudit.conf`. **The folder is created automatically on first export if it
doesn't already exist** - worth knowing before pointing this at a shared drive.

Each drawing costs one Onshape translation round trip (POST a translation job at the
part's exact released version, poll until done, download the result), so this is the
slow part of a run — budget roughly a second per drawing. A 68-part assembly takes
about a minute and a half with the default worker count. One failed drawing doesn't
lose the rest: it's reported by itself and everything else still exports.

Every export is version-scoped to the same revision the audit already resolved, so the
PDF always matches what was actually checked — never the live workspace.

**A zip of just this run's files is also made, separately from the permanent archive** —
in the CLI it lands next to the HTML report; in the web UI it stays under the app's own
`drawings/` folder and gets a link the same way reports do: a random, unguessable token
in the filename is what gates access, so the link works for whoever you send it to
without needing the site password, while a guessed or mistyped filename still 404s. That
separation is deliberate: the permanent per-part PDFs belong on the shared drive forever,
but a "here's what I sent Dave on Tuesday" zip snapshot doesn't.

## STEP export (3D geometry)

`--export-step [DIR]` on the CLI, or the checkbox + folder field in the web UI, exports
each audited assembly's **own 3D geometry** — one STEP file per assembly, not per part —
so it can be opened and viewed outside Onshape. Off by default in both places.

```bash
py -3.13 revaudit.py ASM-12345 --export-step "<share>\STEP FILES"
```

**STEP was chosen over Parasolid deliberately.** Parasolid is Onshape's own geometry
kernel and gives higher-fidelity, lossless output, but it's a kernel-interchange format —
mainly useful between CAD systems that already speak Parasolid natively. STEP is the
neutral, ISO-standard format practically any 3D viewer or CAD system can open, which is
what "for 3D view" actually needs. Same flat, `<assembly number>_Rev<rev>.step` naming
convention as the other folders; default location `REVAUDIT_STEP_DIR` in `.env` /
`[folders] step` in `revaudit.conf`. In the web UI, a shareable link works the same
no-password way the PDF package does.

**Component names inside the file use Onshape's part *name*, not part number, by
default — this needs one Onshape setting changed before it will do what you actually
want.** Verified by testing: the translation request always passes
`evaluateExportRule: true`, and a real export against a 68-part assembly still came back
with plain part names (`WASHER`, `NUT`, `CONNECTOR BINDER <12>`, …) — Onshape only
applies an Export Rule if one exists, and none is configured on this account yet. Fix it
once, for everyone, in Onshape (not something this tool can do for you — it's a
company-wide setting, not an API call):

> **Company/Enterprise Settings → Preferences → Export Rules** → object type **Part** →
> format **STEP** → Convention: `${partNumber}`

Until that's set, exported STEP files are still valid and useful — they just label
components by name instead of part number. The report says so every time, right next to
the download link, so nobody has to remember this paragraph.

## Files

| File | Purpose |
|---|---|
| `revaudit.py` | CLI entry point, HTML report renderer |
| `serve.py` | Single-user web UI, API key from `.env` |
| `oauth_app.py` | Multi-user web app, everyone signs in with their own Onshape account |
| `import_key.py` | Imports credentials from a text file into `.env` |
| `audit_core.py` | Production-file index and the per-assembly audit |
| `onshape_client.py` | REST client, BOM and revision helpers |
| `pdf_export.py` | Drawing PDF and assembly STEP export via Onshape's translation API |
| `config.py` | Reads `revaudit.conf` — port, address, folder paths |
| `test_audit.py` | Offline tests — mock client, no network or credentials needed |
| `dxf_indexer.py` | Publishes a DXF/SAT filename index to a host that cannot see the share |
| `install.bat` | Windows setup — config, shortcuts, optional Startup entry, firewall |
| `autostart.bat` | Turns "start RevAudit when I log in" on or off (`on` / `off` / `status`) |
| `launch.bat` / `_open_browser.bat` | Double-click launcher for `serve.py`; auto-updates from GitHub on start |
| `revaudit.conf.example` | Template for the plain-text settings file |

Run the tests with:

```bash
py -3.13 test_audit.py
```

## Notes

- Indexing a large DXF folder over a network drive takes a few seconds (~14s for 10,800
  files on a mapped drive). It happens once per run, not once per assembly.
- An assembly that has never been released won't be found, since the lookup goes through
  the revision registry. The tool says so rather than failing silently.
- API calls per assembly are roughly `2 + 2 × (unique parts)`, so ~150 for a 70-part
  assembly. Retries with backoff are built in for 429 and 5xx responses.


## Access modes

There are three ways to run it, in increasing order of how many people can use it.

### 1. CLI - just you

```bash
py -3.13 revaudit.py ASM-12345 --dxf-dir "<share>\DXF FILES" --open
```

Uses the API key in `.env`. Nothing is exposed on the network.

### 2. Single-user web UI

```bash
py -3.13 serve.py          # or double-click the RevAudit shortcut install.bat made
```

Same API key, browser front end. Host, port and the advertised address all come
from `revaudit.conf` (`bind_host = 0.0.0.0` by default, so the LAN can reach it).
Serving it beyond localhost means anyone reaching the port could run audits under
**your** key with no login, so it refuses to start that way unless
`REVAUDIT_PASSWORD` is set in `.env`, and then requires that password on every
request. On startup it prints every address it can be reached at — check that
against `revaudit.conf` if coworkers say it stopped working.

### 3. OAuth - a team

```bash
py -3.13 oauth_app.py --host 0.0.0.0 --port 8000
```

Each person signs in with their own Onshape account. This is the mode to use for
more than one or two people, because:

- there is **no shared key** to distribute, encrypt, or rotate
- tokens expire after an hour and refresh themselves, so rotation is automatic
- everyone sees exactly what their own Onshape permissions allow, rather than
  borrowing yours
- when someone's Onshape account is disabled, their access here dies with it

Reports are scoped to the session that produced them - one person cannot open
another person's report, since that report may cover documents they cannot see.

**Registering the app.** At <https://dev-portal.onshape.com> create an OAuth
application. You need:

| Field | Value |
|---|---|
| Redirect URL | the exact URL of `/oauth/callback` on your host |
| Permissions | read-only is enough; RevAudit never writes |

Copy the client ID and secret into `.env` as `ONSHAPE_OAUTH_CLIENT_ID` and
`ONSHAPE_OAUTH_CLIENT_SECRET`, and set `ONSHAPE_OAUTH_REDIRECT_URI` to the same
URL you registered - it must match character for character or Onshape rejects the
sign-in. On an enterprise account, registering an app may require an administrator.

`ONSHAPE_OAUTH_SCOPE` defaults to `OAuth2Read OAuth2ReadPII`. The authoritative
list of scopes is whatever you tick during registration; adjust the variable to
match if you enable something different.

**Letting other people on the network sign in.** By default the app only
answers sign-in at `localhost`, even when it's bound to `0.0.0.0` and reachable
from other machines - Onshape validates the callback URL exactly, and a request
arriving via a LAN IP would otherwise get sent through the `localhost` redirect,
which Onshape refuses. To open it up:

1. Add every address people will use to `ONSHAPE_OAUTH_ALLOWED_HOSTS` in `.env`,
   e.g. `ONSHAPE_OAUTH_ALLOWED_HOSTS=192.168.1.50:8000`.
2. In the Onshape app registration, click **+** next to Redirect URLs and add a
   matching `http://<host>/oauth/callback` for each one - this adds a URL, it
   does not replace the existing `localhost` one, so keep both.
3. Windows Firewall blocks inbound connections by default. Running `install.bat`
   as administrator adds the rule; to do it by hand, from an elevated PowerShell:
   ```powershell
   New-NetFirewallRule -DisplayName "RevAudit (TCP 8000)" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Domain
   ```
   Use `-Profile Domain` on a domain-joined network, `-Private` on a home/office
   LAN that isn't domain-joined - check with `Get-NetConnectionProfile`.
4. Set `bind_host = 0.0.0.0` in `revaudit.conf` (the default) and run
   `py -3.13 oauth_app.py`.

The app derives which redirect URL to use from the incoming request's Host
header, checked against the allowlist, so one running instance serves
`localhost`, a LAN IP, and any other registered hostname at once - no separate
process or config per address.

**Hosting notes.** Put HTTPS in front of it before anyone outside a trusted
network uses it - session cookies are marked `Secure` automatically once a
request arrives on an `https://` host. If the host can see the DXF share
directly (a UNC path such as `\\server\prod\DXF FILES`) it scans it live; if it
cannot, publish an index instead - see below.

## DXF coverage from a host that cannot see the share

The audit matches part numbers against **filenames only** - it never opens a
drawing. So a host with no route to `K:` can still check DXF coverage from a
published name list. For 10,795 files that list is 190 KB of text, 30 KB gzipped.

Run `dxf_indexer.py` on any PC that *can* see the share:

```bash
py -3.13 dxf_indexer.py --post http://revaudit-host:8000/dxf-index
```

Set `REVAUDIT_INDEX_TOKEN` to the same value on both machines - the host rejects
uploads without it, and refuses them entirely if the variable is unset. Schedule
it hourly and nobody needs anything installed on their own machine:

```
schtasks /create /tn "RevAudit DXF index" /sc hourly /tr "py -3.13 C:\path\to\dxf_indexer.py --post http://revaudit-host:8000/dxf-index"
```

The host scans the folder live when it can and falls back to the uploaded index
when it cannot, so the same DXF folder path works in both deployments. The report
names the folder the index came from, so you can tell which was used.

`--out dxf-index.json` writes the index to a file instead of uploading, if you
would rather move it by other means.

## License

GPL-3.0 — see [LICENSE](LICENSE). Copyright (C) 2026 Sergio Alexo.

---

RevAudit is developed by [Sergio Alexo](https://sergioalexo.com).
