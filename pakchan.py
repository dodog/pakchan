#!/usr/bin/env python3
"""
Pakchan — PAMAC-like package manager for Manjaro/Arch
with real changelogs for Pacman, AUR, Flatpak, and Snap.



Requirements:
    sudo pacman -S python-gobject gtk4 libadwaita pacman-contrib

Optional:
    yay or paru, flatpak, snapd

Run:
    python3 pakchan.py
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Gio, Pango

# Vte gives us a real embedded terminal
Vte = None
for _vte_ver in ("3.91", "2.91"):
    try:
        gi.require_version("Vte", _vte_ver)
        from gi.repository import Vte as _Vte
        Vte = _Vte
        break
    except Exception:
        continue
_HAVE_VTE = Vte is not None

# Minimal self-contained SUDO_ASKPASS fallback
_ASKPASS_GTK_SCRIPT = '''#!/usr/bin/env python3
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, Gio

class AskpassApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.pakchan.askpass",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.connect("activate", self.on_activate)
        self.cancelled = False

    def on_activate(self, app):
        self.win = Adw.ApplicationWindow(application=app)
        self.win.set_title("Authentication Required")
        self.win.set_default_size(380, -1)
        self.win.set_resizable(False)
        self.win.connect("close-request", self.on_close_request)

        toolbar_view = Adw.ToolbarView()
        toolbar_view.add_top_bar(Adw.HeaderBar())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(6)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)

        heading = Gtk.Label(label="Authentication required")
        heading.add_css_class("title-3")
        heading.set_halign(Gtk.Align.START)
        box.append(heading)

        body = Gtk.Label(label="Enter your password to continue:")
        body.set_halign(Gtk.Align.START)
        box.append(body)

        self.entry = Gtk.PasswordEntry()
        self.entry.set_show_peek_icon(True)
        self.entry.connect("activate", lambda *_a: self.submit_password())
        box.append(self.entry)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_box.set_halign(Gtk.Align.END)
        btn_box.set_margin_top(6)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda *_a: self.cancel())
        ok_btn = Gtk.Button(label="OK")
        ok_btn.add_css_class("suggested-action")
        ok_btn.connect("clicked", lambda *_a: self.submit_password())
        btn_box.append(cancel_btn)
        btn_box.append(ok_btn)
        box.append(btn_box)

        toolbar_view.set_content(box)
        self.win.set_content(toolbar_view)
        self.win.present()
        self.entry.grab_focus()

    def on_close_request(self, *_a):
        self.cancel()
        return True

    def cancel(self):
        self.cancelled = True
        self.quit()

    def submit_password(self):
        sys.stdout.write(self.entry.get_text() + "\\n")
        sys.stdout.flush()
        self.quit()

app = AskpassApp()
app.run(None)
sys.exit(1 if app.cancelled else 0)
'''

# Disable WebKit process sandbox when user namespaces are unavailable
import gzip, html, json, os, re, shlex, shutil, sys, tarfile, tempfile, threading, time

# Strip ANSI codes from raw pty output for the no-Vte fallback view
_ANSI_ESCAPE_RE = re.compile(
    r'\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])')

_VER_OP_RE = re.compile(r'[<>=].*$')

os.environ.setdefault("WEBKIT_DISABLE_SANDBOX", "1")
# Force GTK's Cairo renderer to avoid Zink/Vulkan driver warnings
os.environ.setdefault("GSK_RENDERER", "cairo")
import subprocess, urllib.request, urllib.error, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ─── Data model ───────────────────────────────────────────────────────────────

@dataclass
class Package:
    name:           str
    version:        str
    new_version:    str
    description:    str
    repo:           str           # "pacman"|"aur"|"flatpak"|"snap"
    installed_size: str = ""
    license:        str = ""
    url:            str = ""
    depends:        str = ""
    checked:        bool = False
    marked_remove:  bool = False      # True = this checkbox means "uninstall", not "install/update"
    is_dep:         bool = False
    has_desktop_entry: bool = False   # True if a .desktop launcher exists
    icon_name:      str = ""          # icon theme name to look up for this package's row
    size_bytes:     int = 0           # raw installed size, used for sorting (installed_size is display-only)
    changelog:      Optional[dict] = None
    installed:      bool = True       # False = search result not yet installed
    remote:         str = ""          # flatpak: remote to install from (e.g. "flathub"), for not-yet-installed search results
    display_name:   str = ""          # nice name (e.g. "Text Editor") — falls back to `name` (the technical id) when empty

    @property
    def has_update(self) -> bool:
        return bool(self.new_version and self.new_version != self.version)

    @property
    def cl_key(self) -> str:
        """Unique cache key — fix #11."""
        return f"{self.repo}:{self.name}"


# ─── Shell / HTTP helpers ─────────────────────────────────────────────────────

def run(cmd: list, timeout: int = 30) -> tuple:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except FileNotFoundError:
        return "", f"not found: {cmd[0]}", 127
    except subprocess.TimeoutExpired:
        return "", "timeout", 1


_GIT_SAFE_OPTS = ["-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
                  "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=10",
                  "-c", "credential.helper="]


def run_git(cmd: list, timeout: float = 10) -> tuple:
    ctx = _ctx()
    if ctx is not None:
        left = ctx.deadline - time.monotonic()
        if left <= 0:
            ctx.incomplete = True
            raise FetchBudgetExceeded()
        timeout = min(timeout, left)
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL="/dev/null", LC_ALL="C")
    full = [cmd[0], *_GIT_SAFE_OPTS, *cmd[1:]]
    try:
        p = subprocess.Popen(full, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             stdin=subprocess.DEVNULL, text=True, errors="replace",
                             env=env, start_new_session=True)
    except FileNotFoundError:
        return "", f"not found: {cmd[0]}", 127
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.communicate(timeout=2)
        except Exception:
            pass
        return "", "timeout", 1
    return out.strip(), err.strip(), p.returncode


# ─── Debug tracing ────────────────────────────────────────────────────────────

_dbg_local = threading.local()

def _dbg_trace() -> list[str]:
    t = getattr(_dbg_local, "trace", None)
    if t is None:
        t = _dbg_local.trace = []
    return t

def _dbg(msg: str):
    _dbg_trace().append(msg)

def _dbg_reset():
    _dbg_local.trace = []

def _dbg_get() -> list[str]:
    return list(_dbg_trace())


# ─── Network / input hardening ────────────────────────────────────────────────
import http.client, ipaddress, signal, socket, zlib

_MAX_HTTP_BYTES     = 8 * 1024 * 1024
_MAX_AUR_GZ_BYTES   = 128 * 1024 * 1024
_MAX_AUR_JSON_BYTES = 512 * 1024 * 1024


def _is_http_url(url: str) -> bool:
    """True only for absolute http(s) URLs."""
    try:
        p = urllib.parse.urlsplit(url or "")
    except ValueError:
        return False
    return p.scheme in ("http", "https") and bool(p.netloc)


def _host_is_blocked(host: str) -> bool:
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        try:   # shorthand/octal/hex forms such as 127.1 or 0x7f000001
            ip = ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return False
    return not ip.is_global


def _check_request_url(req) -> None:
    try:
        parts = urllib.parse.urlsplit(req.full_url)
        host = parts.hostname or ""
    except ValueError as e:
        raise urllib.error.URLError(f"bad URL: {e}")
    if parts.scheme not in ("http", "https") or _host_is_blocked(host):
        raise urllib.error.URLError(f"blocked URL: {req.full_url}")


# Failure budget: circuit breaker per host, lookup deadline
_FETCH_BUDGET_S   = 25.0   # limit for one changelog lookup
_GIT_BUDGET_S     = 20.0   # of which the git fallback may use at most this
_REQ_TIMEOUT_CAP  = 10.0   # per-request ceiling while a lookup is running
_HOST_DOWN_S      = 600    # how long a failing host is skipped afterwards
_HOST_FAIL_LIMIT  = 2      # failures within the window before a host is skipped
_HOST_FAIL_WINDOW = 90


class FetchBudgetExceeded(Exception):
    """The per-lookup deadline has passed."""


class _FetchCtx:
    __slots__ = ("deadline", "incomplete")

    def __init__(self, seconds: float):
        self.deadline = time.monotonic() + seconds
        self.incomplete = False


_budget_local = threading.local()


def _ctx() -> Optional["_FetchCtx"]:
    return getattr(_budget_local, "ctx", None)


def _budget_begin(seconds: float) -> "_FetchCtx":
    c = _budget_local.ctx = _FetchCtx(seconds)
    return c


def _budget_end() -> None:
    _budget_local.ctx = None


def _note_incomplete() -> None:
    c = _ctx()
    if c is not None:
        c.incomplete = True


_host_lock = threading.Lock()
_host_down_until: dict[str, float] = {}
_host_fails: dict[str, tuple[int, float]] = {}


def _host_is_down(host: str) -> bool:
    with _host_lock:
        until = _host_down_until.get(host)
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        del _host_down_until[host]
        return False


def _mark_host_down(host: str, secs: float, why: str) -> None:
    with _host_lock:
        _host_down_until[host] = time.monotonic() + secs
        _host_fails.pop(host, None)
    _note_incomplete()
    _dbg(f"[net] {host}: {why} — skipping it for {int(secs)} s")


def _note_host_ok(host: str) -> None:
    with _host_lock:
        _host_fails.pop(host, None)


def _note_net_failure(host: str, exc: BaseException) -> None:
    """Timeouts, resets, TLS errors, 502/503/504."""
    _note_incomplete()
    reason = getattr(exc, "reason", exc)
    weight = 2 if isinstance(reason, (ConnectionRefusedError, socket.gaierror)) else 1
    now = time.monotonic()
    with _host_lock:
        n, last = _host_fails.get(host, (0, 0.0))
        if now - last > _HOST_FAIL_WINDOW:
            n = 0
        n += weight
        trip = n >= _HOST_FAIL_LIMIT
        if not trip:
            _host_fails[host] = (n, now)
    _dbg(f"[net] {host}: {type(exc).__name__}: {str(exc)[:80]}")
    if trip:
        _mark_host_down(host, _HOST_DOWN_S, "repeated failures")


def _note_http_status(host: str, resp) -> None:
    status = getattr(resp, "status", None) or getattr(resp, "code", 200)
    hdrs = getattr(resp, "headers", None)

    def hdr(name: str):
        return hdrs.get(name) if hdrs is not None else None

    if status == 429 or (status == 403 and hdr("X-RateLimit-Remaining") == "0"):
        secs = _HOST_DOWN_S
        retry, reset = hdr("Retry-After"), hdr("X-RateLimit-Reset")
        if retry and retry.isdigit():
            secs = int(retry)
        elif reset and reset.isdigit():
            secs = int(reset) - int(time.time())
        _mark_host_down(host, max(60, min(secs, 3600)), f"rate limited (HTTP {status})")
    elif status in (502, 503, 504):
        _note_net_failure(host, OSError(f"HTTP {status}"))
    else:
        _note_host_ok(host)


def _guard_request(req) -> str:
    _check_request_url(req)
    host = (urllib.parse.urlsplit(req.full_url).hostname or "").lower()
    if _host_is_down(host):
        _note_incomplete()
        raise urllib.error.URLError(f"{host} is temporarily marked unavailable")
    ctx = _ctx()
    req._pk_clamped = False
    if ctx is not None:
        left = ctx.deadline - time.monotonic()
        if left <= 0:
            ctx.incomplete = True
            raise FetchBudgetExceeded()
        t = req.timeout if isinstance(req.timeout, (int, float)) else _REQ_TIMEOUT_CAP
        t = min(t, _REQ_TIMEOUT_CAP)
        req._pk_clamped = left < t 
        req.timeout = max(1.0, min(t, left))
    return host


def _track(host: str, fn, req):
    try:
        resp = fn(req)
    except (OSError, http.client.HTTPException) as e:
        if not getattr(req, "_pk_clamped", False):
            _note_net_failure(host, e)
        else:
            _note_incomplete()
        raise
    _note_http_status(host, resp)
    return resp


class _SafeHTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req):
        return _track(_guard_request(req), super().http_open, req)


class _SafeHTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req):
        return _track(_guard_request(req), super().https_open, req)


def _install_safe_opener() -> None:
    opener = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.UnknownHandler(),
              _SafeHTTPHandler(), _SafeHTTPSHandler(),
              urllib.request.HTTPDefaultErrorHandler(),
              urllib.request.HTTPRedirectHandler(),
              urllib.request.HTTPErrorProcessor()):
        opener.add_handler(h)
    urllib.request.install_opener(opener)

_install_safe_opener()


def _read_capped(resp, limit: int = _MAX_HTTP_BYTES) -> bytes:
    chunks, total = [], 0
    ctx = _ctx()
    while True:
        if ctx is not None and time.monotonic() > ctx.deadline:
            ctx.incomplete = True
            raise FetchBudgetExceeded()
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError(f"response larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _gunzip_capped(data: bytes, limit: int) -> bytes:
    out = b""
    while data:
        d = zlib.decompressobj(wbits=31)
        out += d.decompress(data, limit + 1 - len(out))
        if len(out) > limit:
            raise ValueError(f"decompressed data larger than {limit} bytes")
        if not d.eof:
            raise EOFError("truncated gzip stream")
        data = d.unused_data
    return out


_SAFE_IDENT_RE = re.compile(r'[A-Za-z0-9@_+][A-Za-z0-9@._+-]*')

def _is_safe_ident(s: str) -> bool:
    return bool(s) and _SAFE_IDENT_RE.fullmatch(s) is not None


def _on_activate_link(_label, uri: str) -> bool:
    return not _is_http_url(uri)


def http_get(url: str, timeout: int = 14) -> Optional[str]:
    """Send realistic browser headers so release-note sites don't reject requests."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return _read_capped(r).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 406:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                })
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return _read_capped(r).decode("utf-8", errors="replace")
            except Exception:
                return None
        return None
    except Exception:
        return None


def http_get_json(url: str, timeout: int = 14):
    """Fetch and parse a JSON API endpoint"""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = _read_capped(r).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 406:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": (
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                })
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    body = _read_capped(r).decode("utf-8", errors="replace")
            except Exception:
                return None
        else:
            return None
    except Exception:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def _is_bot_protection_page(body: str) -> bool:
    """Detect common bot-protection services returning challenge pages."""
    if not body:
        return False
    low = body.lower()
    return ("anubis" in low or "making sure you" in low or 
            "cloudflare" in low or "captcha" in low or
            "challenge" in low)


# Fix #9 — shutil.which() instead of spawning `which`
_CMD_CACHE: dict[str, bool] = {}

def cmd_exists(name: str) -> bool:
    if name not in _CMD_CACHE:
        _CMD_CACHE[name] = shutil.which(name) is not None
    return _CMD_CACHE[name]


def _fmt_bytes(s: str) -> str:
    try:
        b = int(s)
        if b >= 1_073_741_824: return f"{b/1_073_741_824:.1f} GiB"
        if b >= 1_048_576:     return f"{b/1_048_576:.1f} MiB"
        if b >= 1024:          return f"{b/1024:.1f} KiB"
        return f"{b} B"
    except (ValueError, TypeError):
        return s


# ─── Local DB readers ─────────────────────────────────────────────────────────

PACMAN_LOCAL = Path("/var/lib/pacman/local")
PACMAN_SYNC  = Path("/var/lib/pacman/sync")


def _read_local_db() -> dict:
    """Read /var/lib/pacman/local/*/desc — pure Python, no subprocess."""
    pkgs = {}
    if not PACMAN_LOCAL.exists():
        return pkgs
    for pkg_dir in PACMAN_LOCAL.iterdir():
        desc_file = pkg_dir / "desc"
        if not desc_file.exists():
            continue
        try:
            text = desc_file.read_text(errors="replace")
        except PermissionError:
            continue
        fields: dict[str, list] = {}
        cur = None
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("%") and line.endswith("%"):
                cur = line[1:-1].lower()
                fields[cur] = []
            elif line and cur is not None:
                fields[cur].append(line)
        name = " ".join(fields.get("name", []))
        if not name:
            continue
        reason = " ".join(fields.get("reason", ["0"]))
        # Check REASON file (older pacman format)
        reason_file = pkg_dir / "REASON"
        if reason_file.exists():
            try:
                reason = reason_file.read_text().strip()
            except Exception:
                pass
        pkgs[name] = {
            "version": " ".join(fields.get("version", ["?"])),
            "desc":    " ".join(fields.get("desc", [""])),
            "url":     " ".join(fields.get("url", [""])),
            "license": " ".join(fields.get("license", [""])),
            "size":    " ".join(fields.get("size", [""])),
            "depends": ", ".join(fields.get("depends", [])),
            "reason":  reason,
        }
    return pkgs


def _read_sync_db() -> tuple[set, dict, bool]:
    """Read pacman's sync databases directly"""
    names: set[str] = set()
    full:  dict[str, dict] = {}
    db_files = list(PACMAN_SYNC.glob("*.db")) if PACMAN_SYNC.exists() else []

    if db_files:
        for db_path in db_files:
            try:
                with tarfile.open(db_path, "r:gz") as tf:
                    for member in tf.getmembers():
                        if member.isfile() and member.name.endswith("/desc"):
                            try:
                                text = tf.extractfile(member).read().decode(
                                    "utf-8", "replace")
                            except Exception:
                                continue
                            fields: dict[str, list] = {}
                            cur = None
                            for line in text.splitlines():
                                line = line.strip()
                                if line.startswith("%") and line.endswith("%"):
                                    cur = line[1:-1].lower()
                                    fields[cur] = []
                                elif line and cur is not None:
                                    fields[cur].append(line)
                            name = " ".join(fields.get("name", []))
                            if not name:
                                continue
                            names.add(name)
                            full[name] = {
                                "version": " ".join(fields.get("version", ["?"])),
                                "desc":    " ".join(fields.get("desc", [""])),
                                "url":     " ".join(fields.get("url", [""])),
                                "license": " ".join(fields.get("license", [""])),
                                "depends": ", ".join(fields.get("depends", [])),
                                "conflicts": ", ".join(fields.get("conflicts", [])),
                                "provides":  ", ".join(fields.get("provides", [])),
                            }
                        else:
                            # Directory-only db entries still get recorded for repo classification
                            parts = member.name.split("/")
                            if parts and parts[0] and parts[0] not in names:
                                pkg_ver = parts[0]
                                segments = pkg_ver.rsplit("-", 2)
                                names.add(segments[0] if len(segments) >= 2 else pkg_ver)
            except Exception:
                pass
        if names:
            return names, full, True

    # Fallback: subprocess (names only)
    out, _, rc = run(["pacman", "-Slq"], timeout=12)
    if rc == 0 and out:
        return set(out.splitlines()), {}, True
    return set(), {}, False


# ─── Update detection ─────────────────────────────────────────────────────────

def _pending_pacman_updates_from_sync(local_db: dict) -> dict:
    """Fallback update detection for when pacman-contrib's checkupdates is unavailable."""
    if not PACMAN_SYNC.exists() or not cmd_exists("vercmp"):
        return {}

    sync_versions: dict[str, str] = {}
    for db_path in PACMAN_SYNC.glob("*.db"):
        try:
            with tarfile.open(db_path, "r:gz") as tf:
                for member in tf.getmembers():
                    if not member.name.endswith("/desc"):
                        continue
                    f = tf.extractfile(member)
                    if not f:
                        continue
                    text = f.read().decode("utf-8", errors="replace")
                    name = version = ""
                    cur = None
                    for line in text.splitlines():
                        line = line.strip()
                        if line == "%NAME%":
                            cur = "name"; continue
                        if line == "%VERSION%":
                            cur = "version"; continue
                        if line.startswith("%") and line.endswith("%"):
                            cur = None; continue
                        if cur == "name" and not name:
                            name = line
                        elif cur == "version" and not version:
                            version = line
                    if name and version:
                        sync_versions[name] = version
        except Exception:
            continue

    result: dict[str, str] = {}
    for name, sync_ver in sync_versions.items():
        local_info = local_db.get(name)
        if not local_info:
            continue
        local_ver = local_info.get("version", "")
        # Identical strings can never be an update
        if not local_ver or local_ver == sync_ver:
            continue
        out, _, rc = run(["vercmp", sync_ver, local_ver], timeout=5)
        if rc == 0 and out.strip():
            try:
                if int(out.strip()) > 0:
                    result[name] = sync_ver
            except ValueError:
                pass
    return result


def _pending_pacman_updates(local_db: Optional[dict] = None) -> dict:
    out, _, rc = run(["checkupdates"], timeout=45)
    result = {}
    if rc == 0 and out:
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 4:
                result[p[0]] = p[3]
    if result or local_db is None:
        return result
    # Cross-check checkupdates' empty result against the synced databases
    return _pending_pacman_updates_from_sync(local_db)


def _pending_aur_updates(helper: str) -> dict:
    """Fix #10: use correct flags per helper."""
    if helper == "yay":
        cmd = ["yay", "-Qua", "--aur"]
    else:  # paru and others
        cmd = [helper, "-Qua"]
    out, _, rc = run(cmd, timeout=60)
    result = {}
    if rc == 0 and out:
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 4:
                result[p[0]] = p[3]
    return result


def _pending_flatpak_updates() -> dict:
    out, _, rc = run(
        ["flatpak", "remote-ls", "--updates", "--columns=application,version"],
        timeout=20)
    result = {}
    if rc == 0 and out:
        for line in out.splitlines():
            p = line.split()
            if p and "." in p[0]:
                result[p[0]] = p[1] if len(p) > 1 else "latest"
    return result


def _installed_flatpak_versions() -> dict:
    """Bulk-fetch installed Flatpak versions"""
    out, _, rc = run(["flatpak", "list", "--columns=application,version"], timeout=20)
    result = {}
    if rc == 0 and out:
        for line in out.splitlines():
            parts = line.split("\t") if "\t" in line else line.split()
            if parts and "." in parts[0]:
                ver = parts[1].strip() if len(parts) > 1 else ""
                if ver:
                    result[parts[0].strip()] = ver
    return result


# ─── Package enumeration (parallelised — fix #5) ──────────────────────────────

def _preferred_lang_code() -> str:
    """Locale language codebto pick a localized .desktop Name."""
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        val = os.environ.get(var, "")
        if val and val not in ("C", "POSIX"):
            code = val.split(":")[0].split(".")[0].split("_")[0]
            if code:
                return code.lower()
    return ""


def _parse_desktop_meta(desktop_path: Path, lang: str = "") -> tuple[Optional[str], Optional[str], bool]:
    """Read Icon=, Name=, and NoDisplay= from a .desktop file's entry."""
    try:
        text = desktop_path.read_text(errors="replace")
    except Exception:
        return None, None, False
    in_section = False
    icon = None
    name_plain = None
    name_localized = None
    no_display = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = (line == "[Desktop Entry]")
            continue
        if not in_section:
            continue
        if line.startswith("Icon="):
            icon = line.split("=", 1)[1].strip() or icon
        elif line.startswith("Name="):
            name_plain = line.split("=", 1)[1].strip() or name_plain
        elif lang and line.startswith(f"Name[{lang}]="):
            name_localized = line.split("=", 1)[1].strip() or name_localized
        elif line.startswith("NoDisplay="):
            no_display = line.split("=", 1)[1].strip().lower() == "true"
    return icon, (name_localized or name_plain), no_display


# Suffixes marking a .desktop file as a companion tool, not the main app
_AUXILIARY_DESKTOP_SUFFIXES = (
    "-settings", "-preferences", "-prefs", "-config", "-configuration",
    "-setup", "-properties", "-manager", "-uninstall", "-uninstaller",
    "-about", "-wizard", "-autostart", "-service", "-daemon", "-tray",
    "-background", "-kcm", "-mimeinfo", "-nautilus", "-thunar",
)


def _desktop_entries_info() -> dict[str, tuple[str, str]]:
    """For each installed package, resolve its .desktop icon and display name."""
    lang = _preferred_lang_code()
    result: dict[str, tuple[str, str]] = {}
    if not PACMAN_LOCAL.exists():
        return result
    for pkg_dir in PACMAN_LOCAL.iterdir():
        files_path = pkg_dir / "files"
        if not files_path.exists():
            continue
        try:
            text = files_path.read_text(errors="replace")
        except Exception:
            continue
        desktop_rel_paths = [
            line.strip() for line in text.splitlines()
            if "share/applications/" in line and line.strip().endswith(".desktop")
        ]
        if not desktop_rel_paths:
            continue
        # Package name is the dir name minus the trailing "-version-rel"
        pkg_ver = pkg_dir.name
        segments = pkg_ver.rsplit("-", 2)
        pkg_name = segments[0] if len(segments) >= 2 else pkg_ver
        # A package can own more than one .desktop file
        candidates = []
        for rel in desktop_rel_paths:
            path = Path("/" + rel.lstrip("/"))
            icon, dname, no_display = _parse_desktop_meta(path, lang)
            stem = path.stem.lower()
            is_auxiliary = stem.endswith(_AUXILIARY_DESKTOP_SUFFIXES)
            candidates.append((no_display, is_auxiliary, len(stem), icon, dname))
        candidates.sort(key=lambda c: c[:3])
        icon_name = ""
        display_name = ""
        for _no_display, _is_auxiliary, _stem_len, icon, dname in candidates:
            if icon and not icon_name:
                icon_name = icon
            if dname and not display_name:
                display_name = dname
            if icon_name and display_name:
                break
        result[pkg_name] = (icon_name, display_name)
    return result


def _load_pacman_aur(local_db: dict, sync_names: set,
                     aur_helper: Optional[str]) -> tuple[list, dict, dict]:
    """Returns (packages, pacman_pending, aur_pending)."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_pac = ex.submit(_pending_pacman_updates, local_db)
        f_aur = ex.submit(_pending_aur_updates, aur_helper) if aur_helper else None
        f_gui = ex.submit(_desktop_entries_info)
        pacman_pending  = f_pac.result()
        aur_pending     = f_aur.result() if f_aur else {}
        desktop_info    = f_gui.result()

    pkgs = []
    for name, info in sorted(local_db.items()):
        reason  = info.get("reason", "0").strip()
        version = info["version"]
        if name in sync_names:
            repo    = "pacman"
            new_ver = pacman_pending.get(name, "")
        else:
            repo    = "aur"
            new_ver = aur_pending.get(name, "")
        raw_size = info.get("size", "")
        try:
            size_bytes = int(raw_size) if raw_size else 0
        except ValueError:
            size_bytes = 0
        icon_name, display_name = desktop_info.get(name, ("", ""))
        pkgs.append(Package(
            name=name, version=version, new_version=new_ver,
            description=info.get("desc", ""),
            repo=repo,
            installed_size=_fmt_bytes(raw_size),
            size_bytes=size_bytes,
            license=info.get("license", ""),
            url=info.get("url", ""),
            depends=info.get("depends", ""),
            is_dep=(reason == "1"),
            has_desktop_entry=(name in desktop_info),
            icon_name=icon_name,
            display_name=display_name,
        ))
    return pkgs, pacman_pending, aur_pending


def _load_flatpak() -> list:
    if not cmd_exists("flatpak"):
        return []
    fp_pending  = _pending_flatpak_updates()
    fp_versions = _installed_flatpak_versions()
    lang        = _preferred_lang_code()
    flatpak_dirs = [d for d in [
        Path("/var/lib/flatpak/app"),
        Path.home() / ".local/share/flatpak/app",
    ] if d.exists()]
    export_app_dirs = [
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
    ]
    seen: set[str] = set()
    pkgs = []
    for base in flatpak_dirs:
        try:
            entries = sorted(base.iterdir())
        except PermissionError:
            continue
        for app_dir in entries:
            app_id = app_dir.name
            if app_id in seen or "." not in app_id:
                continue
            seen.add(app_id)
            ver = _flatpak_installed_version(app_dir, fp_versions.get(app_id, ""))
            display_name = ""
            for export_dir in export_app_dirs:
                desktop_path = export_dir / f"{app_id}.desktop"
                if desktop_path.exists():
                    _icon, dname, _no_display = _parse_desktop_meta(desktop_path, lang)
                    if dname:
                        display_name = dname
                        break
            pkgs.append(Package(
                name=app_id, version=ver,
                new_version=fp_pending.get(app_id, ""),
                description="", repo="flatpak",
                has_desktop_entry=True,   # Flatpak apps always ship a .desktop file
                icon_name=app_id,         # Flatpak exports its icon under the app ID
                display_name=display_name,
            ))
    return pkgs


def _flatpak_search(q: str) -> list[dict]:
    """Search all configured Flatpak remotes via `flatpak search`."""
    if not cmd_exists("flatpak"):
        return []
    out, _, rc = run(["flatpak", "search", q], timeout=8)
    if rc != 0 or not out:
        return []
    results = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 6:
            continue
        name, desc, app_id, version, branch, remotes = parts[:6]
        app_id = app_id.strip()
        # A real app ID is reverse-DNS-shaped (org.gimp.GIMP)
        if not app_id or app_id.count(".") < 2:
            continue
        remote = (remotes.split(";")[0].strip() if remotes else "") or "flathub"
        results.append({
            "app_id": app_id, "name": name.strip() or app_id,
            "desc": desc.strip(), "version": version.strip(),
            "remote": remote,
        })
    return results


def _load_snap() -> list:
    if not cmd_exists("snap"):
        return []
    out, _, rc = run(["snap", "list"], timeout=15)
    if rc != 0 or not out:
        return []
    pkgs = []
    lines = out.splitlines()
    if lines and lines[0].startswith("Name"):
        lines = lines[1:]
    for line in lines:
        parts = line.split()
        if len(parts) >= 2 and parts[0] not in ("snapd",):
            pkgs.append(Package(
                name=parts[0], version=parts[1],
                new_version="", description="", repo="snap",
                icon_name=parts[0],   # best-effort guess; falls back gracefully if unresolved
            ))
    return pkgs


def _flatpak_installed_version(app_dir: Path, cli_version: str = "") -> str:
    """Prefer the version from `flatpak list`."""
    if cli_version:
        return cli_version
    try:
        for branch_dir in app_dir.iterdir():
            return branch_dir.name
    except Exception:
        pass
    return "installed"


def get_all_packages_fast() -> tuple[list, dict, bool]:
    """Parallel loading pacman/AUR, Flatpak/Snap"""
    local_db                        = _read_local_db()
    sync_names, sync_full, sync_ok  = _read_sync_db()
    aur_helper          = next(
        (h for h in ["yay", "paru"] if cmd_exists(h)), None)

    with ThreadPoolExecutor(max_workers=3) as ex:
        f_pacaur  = ex.submit(_load_pacman_aur, local_db, sync_names, aur_helper)
        f_flatpak = ex.submit(_load_flatpak)
        f_snap    = ex.submit(_load_snap)
        pacaur_pkgs, _, _ = f_pacaur.result()
        flatpak_pkgs      = f_flatpak.result()
        snap_pkgs         = f_snap.result()

    all_pkgs = sorted(pacaur_pkgs + flatpak_pkgs + snap_pkgs,
                      key=lambda p: p.name.lower())
    return all_pkgs, sync_full, sync_ok


# ─── On-demand enrichment ─────────────────────────────────────────────────────

def _flatpak_appstream_component(app_id: str) -> dict:
    """AppStream XML extraction."""
    search_dirs = [
        Path("/var/lib/flatpak/appstream"),
        Path.home() / ".local/share/flatpak/appstream",
    ]
    # Escape for exact XML text match
    id_plain  = f"<id>{app_id}</id>"
    id_attr   = f'id="{app_id}"'

    for base in search_dirs:
        if not base.exists():
            continue
        for xml_path in list(base.rglob("appstream.xml")) + list(base.rglob("*.xml.gz")):
            try:
                if xml_path.suffix == ".gz":
                    with gzip.open(xml_path, "rt", errors="replace") as f:
                        text = f.read()
                else:
                    text = xml_path.read_text(errors="replace")
            except Exception:
                continue
            if id_plain not in text and id_attr not in text:
                continue
            # Extract precise component block — anchor on exact <id> text
            pattern = (
                r'<component[^>]*>'
                r'(?:(?!</component>).)*?'
                + re.escape(id_plain) +
                r'.*?</component>'
            )
            m = re.search(pattern, text, re.DOTALL)
            if not m:
                continue
            block = m.group(0)
            result = {}
            s = re.search(r'<summary[^>]*xml:lang="en"[^>]*>([^<]+)</summary>', block)
            if not s:
                s = re.search(r'<summary(?!\s[^>]*xml:lang)([^>]*)>([^<]+)</summary>', block)
                if s:
                    result["description"] = html.unescape(s.group(2).strip())
            else:
                result["description"] = html.unescape(s.group(1).strip())
            u = re.search(r'<url[^>]*type="homepage"[^>]*>([^<]+)</url>', block)
            if not u:
                u = re.search(r'<url[^>]*>([^<]+)</url>', block)
            if u:
                result["url"] = u.group(1).strip()
            n = re.search(r'<name[^>]*xml:lang="en"[^>]*>([^<]+)</name>', block)
            if not n:
                n = re.search(r'<name(?!\s[^>]*xml:lang)([^>]*)>([^<]+)</name>', block)
                if n:
                    result["name"] = html.unescape(n.group(2).strip())
            else:
                result["name"] = html.unescape(n.group(1).strip())
            if result:
                return result
    return {}


def _local_appstream_releases(pkg_name: str) -> Optional[dict]:
    """Read release history from a package's local AppStream metainfo XML."""
    search_dirs = [
        Path("/usr/share/metainfo"),
        Path("/usr/share/appdata"),
    ]

    pkg_lower = pkg_name.lower()
    candidates: list[Path] = []
    for base in search_dirs:
        if not base.exists():
            continue
        try:
            for xml_path in base.glob("*.xml"):
                stem = xml_path.stem  # strips ".xml"
                for suffix in (".appdata", ".metainfo"):
                    if stem.endswith(suffix):
                        stem = stem[: -len(suffix)]
                        break
                components = [c.lower() for c in stem.split(".")]
                if pkg_lower in components:
                    candidates.append(xml_path)
        except Exception:
            continue

    if candidates:
        _dbg(f"[AppStream] matched {len(candidates)} local file(s): "
             f"{', '.join(p.name for p in candidates)}")
    else:
        _dbg("[AppStream] no local metainfo/appdata file matched")

    for xml_path in candidates:
        try:
            text = xml_path.read_text(errors="replace")
        except Exception:
            continue

        # Match each <release ...> tag
        release_blocks = re.findall(
            r'<release\b([^>]*?)(/?)>(.*?)(?:</release>|(?=<release|\Z))',
            text, re.DOTALL)
        if not release_blocks:
            _dbg(f"[AppStream] {xml_path.name}: no <release> tags found")
            continue

        versions = []
        for attrs, self_closing, body_xml in release_blocks[:6]:
            ver_m  = re.search(r'version="([^"]+)"', attrs)
            date_m = re.search(r'date="([^"]+)"', attrs)
            if not ver_m:
                continue
            ver  = ver_m.group(1)
            date = date_m.group(1)[:10] if date_m else ""
            body = "" if self_closing else body_xml

            items = re.findall(r'<li[^>]*>(.*?)</li>', body, re.DOTALL)
            changes = ([_strip_html(i).strip() for i in items if i.strip()]
                       if items else
                       [s.strip() for s in _strip_html(body).split("\n") if s.strip()])
            versions.append({
                "version": ver,
                "date": date,
                "changes": changes[:8] or [f"Release {ver}"],
            })
        if versions:
            _dbg(f"[AppStream] {xml_path.name}: extracted {len(versions)} version(s) ✓")
            return {"versions": versions,
                    "source": f"Local AppStream metadata — {xml_path.name}"}

    return None


def enrich_pkg(pkg: Package):
    """Fill in missing fields when a package is selected."""
    if pkg.repo == "flatpak":
        # 1. Local AppStream XML
        if not pkg.description or not pkg.url or not pkg.display_name:
            info = _flatpak_appstream_component(pkg.name)
            if info.get("description") and not pkg.description:
                pkg.description = info["description"]
            if info.get("url") and not pkg.url:
                pkg.url = info["url"]
            if info.get("name") and not pkg.display_name:
                pkg.display_name = info["name"]

        # 2. flatpak info subprocess
        if not pkg.description or not pkg.url or not pkg.installed_size:
            out, _, rc = run(["flatpak", "info", pkg.name])
            if rc == 0:
                for line in out.splitlines():
                    if ":" in line:
                        k, _, v = line.partition(":")
                        k, v = k.strip(), v.strip()
                        if k == "Summary"  and not pkg.description:  pkg.description  = v
                        elif k == "Homepage" and not pkg.url:         pkg.url          = v
                        elif k == "Installed" and not pkg.installed_size: pkg.installed_size = v
                        elif k == "Version" and not pkg.version:      pkg.version      = v

        # 3. Flathub REST API last resort
        if not pkg.description:
            data = http_get_json(
                f"https://flathub.org/api/v2/appstream/{urllib.parse.quote(pkg.name)}")
            if data and isinstance(data, dict):
                pkg.description = data.get("summary") or data.get("name") or ""
                if not pkg.url:
                    urls = data.get("project_urls") or {}
                    pkg.url = urls.get("homepage") or urls.get("Homepage") or ""

    elif pkg.repo == "snap":
        if not pkg.description or not pkg.url:
            out, _, rc = run(["snap", "info", pkg.name])
            if rc == 0:
                for line in out.splitlines():
                    if line.startswith("summary:"):
                        pkg.description = line.split(":", 1)[1].strip().strip("'\"")
                    elif line.startswith("website:"):
                        pkg.url = line.split(":", 1)[1].strip()


# ─── Cache / Mappings ─────────────────────────────────────────────────────────

MAPPINGS_URL   = "https://raw.githubusercontent.com/dodog/pakchan/refs/heads/main/data/mappings.json"
CACHE_DIR      = Path.home() / ".cache" / "pakchan"
MAPPINGS_CACHE = CACHE_DIR / "mappings.json"
CHANGELOG_DB   = CACHE_DIR / "changelogs.json"
CL_MAX_AGE_S   = 7 * 86400   # 7 days — fix #6

# AUR's full metadata dump, cached locally
AUR_META_URL       = "https://aur.archlinux.org/packages-meta-ext-v1.json.gz"
AUR_META_CACHE     = CACHE_DIR / "aur_meta.json.gz"
AUR_META_MAX_AGE_S = 24 * 3600
KNOWN_AUR_META: dict[str, dict] = {}   # {name: {version, desc, url, license, depends}}

KNOWN_GITHUB_REPOS:  dict[str, str]             = {}
KNOWN_GITLAB_REPOS:  dict[str, tuple[str, str]] = {}
KNOWN_RELEASE_PAGES: dict[str, str]             = {}

# Algorithm selection
DEFAULT_CUSTOM: dict[str, dict] = {
    "firefox":     {"parser": "mozilla"},
    "thunderbird": {"parser": "mozilla"},
    "filezilla":   {"parser": "filezilla"},
}
KNOWN_CUSTOM:        dict[str, dict]            = DEFAULT_CUSTOM.copy()   # custom parsers (merged with mappings)

# Known GitLab-like hosts
KNOWN_GITLAB_LIKE = {
    "gitlab.com",
    "gitlab.gnome.org",
    "invent.kde.org",
    "source.kde.org",
    "gitlab.winehq.org",
    "gitlab.archlinux.org",
}

def _apply_mappings(data: dict):
    global KNOWN_GITHUB_REPOS, KNOWN_GITLAB_REPOS, KNOWN_RELEASE_PAGES, KNOWN_CUSTOM
    KNOWN_GITHUB_REPOS  = data.get("github", {})
    KNOWN_RELEASE_PAGES = data.get("release_pages", {})
    # Merge any remotely-provided custom mappings with local defaults
    KNOWN_CUSTOM = {}
    for pkg, entry in DEFAULT_CUSTOM.items():
        KNOWN_CUSTOM[pkg] = dict(entry)
    for pkg, entry in (data.get("custom") or {}).items():
        if not isinstance(entry, dict):
            continue
        existing = KNOWN_CUSTOM.get(pkg, {})
        KNOWN_CUSTOM[pkg] = {**existing, **entry}
    raw_gl = data.get("gitlab", {})
    KNOWN_GITLAB_REPOS = {
        pkg: (info["host"], info["repo"])
        for pkg, info in raw_gl.items()
        if isinstance(info, dict) and "host" in info and "repo" in info
    }


def _load_mappings_from_cache():
    """Load from disk cache immediately (called at startup, no network)."""
    if MAPPINGS_CACHE.exists():
        try:
            _apply_mappings(json.loads(MAPPINGS_CACHE.read_text()))
        except Exception:
            pass


def _refresh_mappings_bg():
    """Fix #1: Fetch remote mappings in background after UI is shown."""
    def _fetch():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        raw = http_get(MAPPINGS_URL, timeout=10)
        if raw:
            try:
                data = json.loads(raw)
                MAPPINGS_CACHE.write_text(raw, encoding="utf-8")
                _apply_mappings(data)
            except Exception:
                pass
    threading.Thread(target=_fetch, daemon=True).start()


def _parse_aur_meta(gz_bytes: bytes) -> dict[str, dict]:
    """Parse AUR's packages-meta-ext-v1.json.gz into a name-keyed lookup."""
    try:
        raw  = _gunzip_capped(gz_bytes, _MAX_AUR_JSON_BYTES)
        data = json.loads(raw)
    except Exception as e:
        print(f"[aur-meta] decompress/parse failed: {e}", file=sys.stderr)
        return {}
    result: dict[str, dict] = {}
    if not isinstance(data, list):
        print(f"[aur-meta] unexpected top-level JSON type: {type(data).__name__} "
              f"(expected a list)", file=sys.stderr)
        return result
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("Name")
        if not name:
            continue
        result[name] = {
            "version": entry.get("Version") or "?",
            "desc":    entry.get("Description") or "",
            "url":     entry.get("URL") or "",
            "license": ", ".join(entry.get("License") or []),
            "depends": ", ".join(entry.get("Depends") or []),
            "conflicts": ", ".join(entry.get("Conflicts") or []),
            "provides":  ", ".join(entry.get("Provides") or []),
        }
    print(f"[aur-meta] parsed {len(result)} packages", file=sys.stderr)
    return result


def _load_aur_meta_from_cache():
    """Load from disk cache."""
    global KNOWN_AUR_META
    if AUR_META_CACHE.exists():
        try:
            KNOWN_AUR_META = _parse_aur_meta(AUR_META_CACHE.read_bytes())
            print(f"[aur-meta] loaded {len(KNOWN_AUR_META)} packages from "
                  f"disk cache ({AUR_META_CACHE})", file=sys.stderr)
        except Exception as e:
            print(f"[aur-meta] failed to read disk cache: {e}", file=sys.stderr)
    else:
        print(f"[aur-meta] no disk cache yet at {AUR_META_CACHE} — "
              f"waiting for background fetch", file=sys.stderr)


def _refresh_aur_meta_bg():
    """Refresh AUR's metadata dump in the background if the cache is stale."""
    def _fetch():
        global KNOWN_AUR_META
        need_refresh = True
        if AUR_META_CACHE.exists():
            age = time.time() - AUR_META_CACHE.stat().st_mtime
            need_refresh = age > AUR_META_MAX_AGE_S
        if not need_refresh:
            print(f"[aur-meta] disk cache is fresh enough, skipping "
                  f"background re-fetch", file=sys.stderr)
            return
        print(f"[aur-meta] fetching {AUR_META_URL} …", file=sys.stderr)
        try:
            req = urllib.request.Request(
                AUR_META_URL, headers={"User-Agent": "Pakchan/2.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                gz_bytes = _read_capped(r, _MAX_AUR_GZ_BYTES)
        except Exception as e:
            print(f"[aur-meta] fetch failed: {e}", file=sys.stderr)
            return
        if not gz_bytes:
            print("[aur-meta] fetch returned no data", file=sys.stderr)
            return
        print(f"[aur-meta] fetched {len(gz_bytes)} bytes, parsing…", file=sys.stderr)
        parsed = _parse_aur_meta(gz_bytes)
        if not parsed:
            print("[aur-meta] parse produced 0 packages — not caching or "
                  "swapping in this result", file=sys.stderr)
            return
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            AUR_META_CACHE.write_bytes(gz_bytes)
        except Exception as e:
            print(f"[aur-meta] failed to write disk cache: {e}", file=sys.stderr)
        KNOWN_AUR_META = parsed
        print(f"[aur-meta] ready: {len(KNOWN_AUR_META)} packages available "
              f"for search", file=sys.stderr)
    threading.Thread(target=_fetch, daemon=True).start()


# ── Fix #2: Debounced changelog DB save ──────────────────────────────────────

_CL_DB:         dict  = {}
_cl_dirty:      bool  = False
_cl_save_lock         = threading.Lock()
_cl_last_save:  float = 0.0
_SAVE_INTERVAL        = 30.0   # seconds


def _cl_db_load():
    global _CL_DB
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if CHANGELOG_DB.exists():
        try:
            _CL_DB = json.loads(CHANGELOG_DB.read_text(encoding="utf-8"))
        except Exception:
            _CL_DB = {}


def _cl_db_flush(force: bool = False):
    global _cl_dirty, _cl_last_save
    with _cl_save_lock:
        if not _cl_dirty:
            return
        now = time.monotonic()
        if not force and (now - _cl_last_save) < _SAVE_INTERVAL:
            return
        try:
            CHANGELOG_DB.write_text(
                json.dumps(_CL_DB, ensure_ascii=False, indent=2), encoding="utf-8")
            _cl_dirty    = False
            _cl_last_save = now
        except Exception:
            pass


def _cl_cache_get(key: str) -> Optional[dict]:
    """Fix #6: Return None if entry older than CL_MAX_AGE_S."""
    entry = _CL_DB.get(key)
    if not entry:
        return None
    fetched_at = entry.get("_fetched_at", 0)
    age = time.time() - fetched_at
    if age > CL_MAX_AGE_S:
        entry["_stale"] = True   # mark stale but still return for display
    return entry


def _cl_cache_set(key: str, data: dict):
    global _cl_dirty
    data["_fetched_at"] = time.time()
    data.pop("_stale", None)
    data.pop("_from_cache", None)
    _CL_DB[key] = data
    _cl_dirty = True
    _cl_db_flush()


# ─── HTML helpers ─────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    text = re.sub(r"<li[^>]*>", "• ", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _parse_md_changelog(body: str) -> list[str]:
    changes = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        # Skip obvious noise lines
        if _is_pgp_garbage(line):
            continue
        if re.match(r'^(tag|tagger|release)\b', line, re.I):
            continue
        if re.match(r'^(version\b|v\b)\s*\d', line.lower()) or re.match(r'^\d+(?:[\.\-]\d+)+$', line):
            continue
        if line.startswith(("- ", "* ", "+ ", "• ")):
            text = line[2:].strip()
            if text and not text.startswith("http"):
                changes.append(text)
        elif line.startswith("### ") and len(changes) < 15:
            changes.append(f"[{line[4:].strip()}]")
        elif line.startswith("## ") and len(changes) < 15:
            changes.append(f"[{line[3:].strip()}]")
    if not changes and body:
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            if _is_pgp_garbage(line):
                continue
            if re.match(r'^(tag|tagger|release)\b', line, re.I):
                continue
            if re.match(r'^(version\b|v\b)\s*\d', line.lower()) or re.match(r'^\d+(?:[\.\-]\d+)+$', line):
                continue
            if line and not line.startswith("#") and not line.startswith("http"):
                changes.append(line)
            if len(changes) >= 4:
                break
    return changes


# ── Shared scraper helpers ────────────────────────────────────────────────────

def _strip_noise_blocks(html: str) -> str:
    """Remove <head>, <nav>, <header>, <footer>, <script>, <style> blocks."""
    for tag in ("head", "nav", "header", "footer", "script", "style",
                "svg", "noscript"):
        html = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', ' ', html,
                      flags=re.DOTALL | re.IGNORECASE)
    return html


# ── Release page dispatcher ───────────────────────────────────────────────────

def _fetch_parallel(urls: list[str], timeout: int = 12) -> dict[str, Optional[str]]:
    """Fetch multiple URLs in parallel, return {url: body}."""
    if not urls:
        return {}
    results: dict[str, Optional[str]] = {}
    ctx = _ctx()

    def _one(u: str) -> Optional[str]:
        _budget_local.ctx = ctx
        return http_get(u, timeout)

    with ThreadPoolExecutor(max_workers=min(len(urls), 6)) as ex:
        futs = {ex.submit(_one, u): u for u in urls}
        for f in as_completed(futs):
            results[futs[f]] = f.result()
    return results


# ── Custom parsers (from mappings.json "custom" section) ─────────────────────

def _scrape_custom(pkg_name: str, entry: dict, target_version: str = "") -> Optional[dict]:
    """Dispatch to custom parser based on entry['parser'] field."""
    parser = entry.get("parser", "")
    if parser == "gitlab":
        host = entry.get("host", "")
        repo = entry.get("repo", "")
        if host and repo:
            return _gitlab_releases(host, repo, pkg_name, target_version)
        return None

    url = entry.get("url", "")

    # Firefox/Thunderbird use Mozilla's JSON API instead of scraping `url`
    if parser == "mozilla":
        body = http_get(url, timeout=16) if url else ""
        return _scrape_mozilla(url, body or "", pkg_name, target_version)

    if not url:
        return None

    body = http_get(url, timeout=16)
    if not body:
        return None
    if parser == "text_file":
        # Detect markdown-formatted NEWS files even when mapped as plain text
        if _looks_like_markdown_changelog(body):
            return _scrape_github_raw_changelog(body)
        return _scrape_text_file(body)
    if parser == "github_raw":
        return _scrape_github_raw_changelog(body)
    if parser == "filezilla":
        return _scrape_filezilla_changelog(body, url)
    # Unknown parser type
    return None


def _scrape_text_file(body: str) -> Optional[dict]:
    """Parse a plain-text changelog/release-notes file (no HTML at all)."""
    versions: list[dict] = []
    lines = body.splitlines()

    ver_header = re.compile(
        r'^\s*'
        r'(?:[A-Za-z][\wÀ-ž _-]*?\s+)?'        # optional app name prefix
        r'(?:version|release|ver(?:zia)?|v\.?)?\s*'  # EN/SK version keyword
        r'[v=\[\-#*_\s]*'
        r'([\d]+\.[\d]+(?:\.[\d]+)?(?:\s*[\w]+)?)'    # version number
        r'\s*[=\]\-_]*'
        r'(?:\s*[\(\[]?([\d]{4}[-./][\d]{2}[-./][\d]{2})[\)\]]?)?'  # optional date
        r'\s*$',
        re.IGNORECASE)

    verzia_inline = re.compile(r'\bverzia\s+([\d]+(?:\.[\d]+)+)', re.IGNORECASE)

    current_ver     = None
    current_date    = ""
    current_changes: list[str] = []

    def _flush():
        if current_ver and current_changes:
            versions.append({
                "version": current_ver,
                "date":    current_date,
                "changes": current_changes[:10],
            })

    for line in lines:
        m = ver_header.match(line)
        if not m:
            if len(line.strip()) < 80:
                vm = verzia_inline.search(line)
                if vm:
                    date_m = re.search(r'(\d{4}[-./]\d{2}[-./]\d{2})', line)
                    _flush()
                    current_ver     = vm.group(1)
                    current_date    = (date_m.group(1) if date_m else "")[:10]
                    current_changes = []
                    if len(versions) >= 6:
                        break
                    continue
        if m and m.group(1):
            _flush()
            current_ver     = m.group(1).strip()
            current_date    = (m.group(2) or "")[:10]
            current_changes = []
            if len(versions) >= 6:
                break
            continue

        if current_ver is None:
            continue

        stripped = line.strip()
        if not stripped or re.match(r'^[=\-_]{3,}$', stripped):
            continue

        if stripped[0] in ("-", "*", "+", "•", "·"):
            text = stripped[1:].strip()
            if text and len(text) > 3:
                current_changes.append(text)
        elif line.startswith(("    ", "\t")) and len(stripped) > 5:
            current_changes.append(stripped)
        elif len(stripped) > 10:
            current_changes.append(stripped)

    _flush()
    return {"versions": versions, "source": "Plain-text release notes"} if versions else None


def _looks_like_markdown_changelog(body: str) -> bool:
    """True if the file uses Markdown-style headings."""
    return bool(re.search(r'(?m)^#{1,3}[ \t]', body[:2000]))


def _scrape_github_raw_changelog(body: str) -> Optional[dict]:
    """Parse a raw CHANGELOG/RELEASE-NOTES/NEWS file (Markdown headings)."""
    versions = []
    heading_re = re.compile(
        r'^(#{1,3}[ \t]+[^\n]*)\n'      # group 1: the whole heading line (single line only)
        r'(.*?)'                        # group 2: body until next heading/EOF
        r'(?=^#{1,3}[ \t]+|\Z)',
        re.DOTALL | re.MULTILINE)
    # Version number must appear within the first few words
    version_in_heading_re = re.compile(
        r'^#{1,3}[ \t]+(?:[A-Za-z][\w.+-]{0,20}[ \t]+){0,3}'
        r'\[?v?(\d+\.\d[\d.]*(?:-[\w.]+)?)\]?')
    date_in_heading_re = _GENERIC_DATE_RE

    for m in heading_re.finditer(body):
        heading_line = m.group(1)
        block        = m.group(2)
        vm = version_in_heading_re.match(heading_line)
        if not vm:
            continue
        ver  = vm.group(1)
        dm   = date_in_heading_re.search(heading_line)
        date = _normalize_date_str(dm.group(0)) if dm else ""
        changes = _parse_md_changelog(block)
        versions.append({"version": ver, "date": date,
                         "changes": changes[:10] or [f"Release {ver}"]})
        if len(versions) >= 6:
            break
    if versions:
        return {"versions": versions, "source": "GitHub raw changelog"}
    # Fall back to plain-text parser for non-Markdown changelog formats
    return _scrape_text_file(body)


# ── Mozilla ───────────────────────────────────────────────────────────────────

def _scrape_mozilla(url: str, body: str, pkg_name: str = "", target_version: str = "") -> Optional[dict]:
    """Try the product-details JSON API first, then fall back to scraping the releases index."""
    # Determine Thunderbird-vs-Firefox from the package name, not the URL
    is_thunderbird = "thunderbird" in pkg_name.lower()
    prod  = "thunderbird" if is_thunderbird else "firefox"
    base  = "https://www.thunderbird.net" if is_thunderbird else "https://www.firefox.com"

    # The pacman/AUR "firefox" package tracks mainline releases
    is_esr = (not is_thunderbird) and "esr" in pkg_name.lower()

    # 1. Try product-details JSON
    pd = http_get_json(f"https://product-details.mozilla.org/1.0/{prod}.json")
    if not pd or not isinstance(pd, dict):
        _dbg(f"[mozilla] product-details JSON: fetch/parse failed for {prod}")
    if pd and isinstance(pd, dict):
        releases = pd.get("releases", {})
        pool = [(k, v) for k, v in releases.items()
                if isinstance(v, dict) and v.get("date")
                and v.get("category") in ("major", "stability", "esr")]
        pool = [(k, v) for k, v in pool if ("esr" in k.lower()) == is_esr]
        items = sorted(pool, key=lambda x: x[1].get("date", ""), reverse=True)[:5]
        # Prefer the exact target version's own entry when it's known
        stripped_target = _strip_pacman_epoch_pkgrel(target_version) if target_version else ""
        target_key = f"{prod}-{stripped_target}" if stripped_target else ""
        if (target_key and target_key in releases
                and not any(k == target_key for k, _ in items)):
            _dbg(f"[mozilla] target version {stripped_target!r} not in the "
                 f"top-N-by-date window — adding it explicitly")
            items = [(target_key, releases[target_key])] + items
            items = items[:6]
        if not items:
            _dbg(f"[mozilla] product-details JSON: fetched OK but 0 matching "
                 f"releases (got {len(releases)} raw entries, is_esr={is_esr})")
        if items:
            top_cat = releases.get(items[0][0], {}).get("category")
            _dbg(f"[mozilla] product-details JSON: top candidate "
                 f"{items[0][1].get('version')} ({items[0][1].get('date')}) "
                 f"key={items[0][0]!r} category={top_cat!r} "
                 f"pkg_name={pkg_name!r} target_version={target_version!r} "
                 f"is_esr={is_esr}")
            _dbg("[mozilla] full candidate pool: " + ", ".join(
                f"{k}(cat={v.get('category')!r},date={v.get('date')})"
                for k, v in items))
            if stripped_target:
                exact = releases.get(target_key)
                _dbg(f"[mozilla] exact key {target_key!r} in dataset: "
                     f"{exact if exact else 'NOT FOUND'}")
            note_urls = [f"{base}/en-US/{prod}/{v.get('version', k)}/releasenotes/"
                         for k, v in items]
            pages     = _fetch_parallel(note_urls, timeout=12)
            fetched   = sum(1 for u in note_urls if pages.get(u))
            _dbg(f"[mozilla] release notes pages: fetched {fetched}/{len(note_urls)}")
            versions  = []
            for (k, info), note_url in zip(items, note_urls):
                ver     = str(info.get("version", k))
                date    = str(info.get("date", ""))[:10]
                notes   = pages.get(note_url) or ""
                changes = _parse_mozilla_notes(notes)
                if not changes:
                    _dbg(f"[mozilla] {ver}: page fetched but parser found "
                         f"0 change entries (page len={len(notes)})")
                versions.append({"version": ver, "date": date,
                                  "changes": changes[:10] or [f"Release {ver}"]})
            if versions:
                return {"versions": versions,
                        "source": "Mozilla product-details + release notes"}

    # 2. Scrape the releases index page body
    _dbg(f"[mozilla] falling back to index-page scrape (body len={len(body or '')})")
    clean     = _strip_noise_blocks(body)
    ver_links = list(dict.fromkeys(re.findall(
        rf'/{prod}/([\d]+\.[\d.]+(?:esr)?)/releasenotes/', clean)))[:5]

    if not ver_links:
        ver_links = list(dict.fromkeys(re.findall(
            r'>([\d]+\.[\d]+(?:\.[\d]+)?(?:esr)?)<', clean)))[:5]

    if not ver_links:
        _dbg("[mozilla] index-page scrape: no version links found either — giving up")
        return None

    note_urls = [f"{base}/en-US/{prod}/{v}/releasenotes/" for v in ver_links]
    pages     = _fetch_parallel(note_urls, timeout=12)
    versions  = []
    for ver, note_url in zip(ver_links, note_urls):
        notes   = pages.get(note_url) or ""
        changes = _parse_mozilla_notes(notes)
        versions.append({"version": ver, "date": "",
                         "changes": changes[:10] or [f"Release {ver}"]})
    return {"versions": versions, "source": "Mozilla release notes"} if versions else None


def _parse_mozilla_notes(html_text: str) -> list[str]:
    """Extract actual change entries from a Mozilla/Thunderbird release notes page."""
    if not html_text:
        return []

    # Step 1: Remove obvious noise blocks before any parsing
    clean = html_text
    for tag in ("head", "nav", "header", "footer", "script", "style"):
        clean = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', clean,
                       flags=re.DOTALL | re.IGNORECASE)

    # Step 2: Try to find the main content area
    main_match = re.search(
        r'<(?:main|article)[^>]*>(.*?)</(?:main|article)>',
        clean, re.DOTALL | re.IGNORECASE)
    if not main_match:
        main_match = re.search(
            r'<div[^>]*class="[^"]*(?:notes|content|main|release)[^"]*"[^>]*>(.*?)</div>',
            clean, re.DOTALL | re.IGNORECASE)
    body = main_match.group(1) if main_match else clean

    def _mozilla_text_ok(text: str) -> bool:
        if not text or len(text) < 15 or len(text) > 500:
            return False
        if re.match(r'^(?:Windows|Mac|macOS|Linux|Android|iOS|GTK\+?|GTK|Requires|Supported|Release)\b',
                    text, re.IGNORECASE):
            return False
        if re.search(r'\b(?:Windows|Mac|macOS|Linux|GTK\+?|Android|iOS)\b.*\b(?:later|higher|minimum|requires|supported)\b',
                     text, re.IGNORECASE):
            return False
        if re.match(r'^[\d\.]+\s+\d{4}-\d{2}-\d{2}$', text):
            return False
        if 'Mozilla Public License' in text:
            return False
        return True

    changes = []

    # Step 3: Prefer actual Thunderbird/Mozilla note blocks first.
    note_texts = re.findall(
        r'<div[^>]*class=["\"][^"\"]*note-text[^"\"]*["\"][^>]*>(.*?)</div>',
        body, re.DOTALL | re.IGNORECASE)
    for note_html in note_texts:
        for p in re.findall(r'<p[^>]*>(.*?)</p>', note_html, re.DOTALL | re.IGNORECASE):
            text = _strip_html(p).strip()
            if _mozilla_text_ok(text) and text not in changes:
                changes.append(text)
    if changes:
        return changes[:12]

    # Step 4: Look for section headings + their list items
    sections = re.findall(
        r'<(?:section|div)[^>]*class="[^"]*'
        r'(?:new|fixed|changed|security|developer|enterprise)[^"]*"[^>]*>'
        r'(.*?)</(?:section|div)>',
        body, re.DOTALL | re.IGNORECASE)

    if not sections:
        # Fallback: heading followed by <ul>
        sections = re.findall(
            r'<h[2-4][^>]*>(?:New|Fixed|Changed|Security|Developer|What.s New)'
            r'[^<]*</h[2-4]>\s*(.*?)(?=<h[2-4]|$)',
            body, re.DOTALL | re.IGNORECASE)

    for section in sections:
        items = re.findall(r'<li[^>]*>(.*?)</li>', section, re.DOTALL)
        for item in items:
            text = _strip_html(item).strip()
            if _mozilla_text_ok(text) and not re.search(r'fill:|behavior:|url\(', text):
                changes.append(text)

    if not changes:
        # Last resort: all <li> in main body, same quality filter
        items = re.findall(r'<li[^>]*>(.*?)</li>', body, re.DOTALL)
        for item in items:
            text = _strip_html(item).strip()
            if _mozilla_text_ok(text) and not re.search(r'fill:|behavior:|url\(', text):
                changes.append(text)

    return changes[:12]


def _scrape_filezilla_changelog(body: str, url: str) -> Optional[dict]:
    """Parse FileZilla's changelog.php page into versions."""
    if not body:
        return None

    versions = []
    # If the URL returns an Atom/RSS feed, parse <entry> items
    if body.lstrip().startswith('<?xml') or '<feed' in body.lower() or '<rss' in body.lower():
        entries = re.findall(r'<entry>(.*?)</entry>', body, flags=re.DOTALL|re.IGNORECASE)
        for e in entries[:8]:
            title_m = re.search(r'<title[^>]*>(.*?)</title>', e, re.DOTALL|re.IGNORECASE)
            updated_m = re.search(r'<updated[^>]*>(.*?)</updated>', e, re.DOTALL|re.IGNORECASE)
            summary_m = re.search(r'<summary[^>]*>(.*?)</summary>', e, re.DOTALL|re.IGNORECASE)
            title = _strip_html(title_m.group(1)) if title_m else ''
            date = (updated_m.group(1) if updated_m else '')[:10]
            summary = summary_m.group(1) if summary_m else ''
            # Extract version number from title, e.g. 'FileZilla Client 3.70.6 released'
            ver_m = re.search(r'(\d+\.\d+(?:\.\d+)?)', title)
            ver = ver_m.group(1) if ver_m else title
            changes = []
            # summary may contain XHTML; extract <li> or paragraphs
            lis = re.findall(r'<li[^>]*>(.*?)</li>', summary, re.DOTALL|re.IGNORECASE)
            if lis:
                for li in lis[:10]:
                    t = _strip_html(li).strip()
                    if t:
                        changes.append(t)
            else:
                # fallback: paragraphs or plain text
                ps = re.findall(r'<p[^>]*>(.*?)</p>', summary, re.DOTALL|re.IGNORECASE)
                if ps:
                    for p in ps[:6]:
                        for line in _strip_html(p).splitlines():
                            s = line.strip()
                            if s:
                                changes.append(s)
                else:
                    txt = _strip_html(summary).strip()
                    if txt:
                        for line in txt.splitlines():
                            s=line.strip()
                            if s:
                                changes.append(s)
            if changes:
                versions.append({"version": ver, "date": date, "changes": changes[:10]})
        return {"versions": versions, "source": f"FileZilla feed — {url}"} if versions else None

    # Otherwise fall back to site scraping: look for list items or paragraphs
    lis = re.findall(r'<li[^>]*>(.*?)</li>', body, re.DOTALL|re.IGNORECASE)
    if lis:
        # Use the first group of list items as a loose changelog
        changes = [_strip_html(li).strip() for li in lis[:12] if _strip_html(li).strip()]
        if changes:
            return {"versions": [{"version": "latest", "date": "", "changes": changes[:10]}],
                    "source": f"FileZilla changelog page — {url}"}

    return None


# ─── Generic release-notes scraper (for arbitrary "release_pages" sites) ─────

_GENERIC_HEADING_RE = re.compile(
    r'<(h[1-4])[^>]*>(.*?)</\1>', re.IGNORECASE | re.DOTALL)
_GENERIC_VERSION_IN_TEXT_RE = re.compile(
    r'\b(?:version\s+|release\s+|v\.?)?(\d{1,4}(?:\.\d{1,4}){1,3})\b', re.IGNORECASE)
_GENERIC_DATE_RE = re.compile(
    r'(\d{4}[-/]\d{1,2}[-/]\d{1,2}'
    r'|(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4})',
    re.IGNORECASE)
_GENERIC_MIN_ENTRIES = 2   # need at least this many plausible sections to trust the result


def _normalize_date_str(raw: str) -> str:
    """Best-effort conversion of a found date string to YYYY-MM-DD."""
    raw = raw.strip()
    m = re.match(r'(\d{4})[-/](\d{1,2})[-/](\d{1,2})', raw)
    if m:
        y, mo, d = m.groups()
        return f"{y}-{int(mo):02d}-{int(d):02d}"
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%B %d %Y", "%b %d %Y"):
        try:
            return datetime.strptime(raw.replace(",", ""), fmt.replace(",", "")).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw[:10]


def _extract_generic_block_changes(block_html: str) -> list[str]:
    """Pull descriptive lines from the HTML between one version heading and the next."""
    items = re.findall(r'<li[^>]*>(.*?)</li>', block_html, re.DOTALL)
    if items:
        changes = [_strip_html(i).strip() for i in items]
    else:
        paras = re.findall(r'<p[^>]*>(.*?)</p>', block_html, re.DOTALL)
        changes = [_strip_html(p).strip() for p in paras]
    # Drop empty/junk fragments: CSS/JS leftovers, nav labels, etc.
    changes = [c for c in changes
               if 5 < len(c) < 400 and "{" not in c and "function(" not in c
               and len(c.split()) >= 2]
    return changes[:10]


def _scrape_headings_for_versions(html_text: str) -> list[dict]:
    """Strategy 1: repeating h1-h4 headings, each naming a version."""
    matches = list(_GENERIC_HEADING_RE.finditer(html_text))
    versions = []
    for i, m in enumerate(matches):
        heading_text = _strip_html(m.group(2))
        vm = _GENERIC_VERSION_IN_TEXT_RE.search(heading_text)
        if not vm:
            continue
        ver = vm.group(1)
        dm  = _GENERIC_DATE_RE.search(heading_text)
        date = _normalize_date_str(dm.group(0)) if dm else ""
        start = m.end()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(html_text)
        changes = _extract_generic_block_changes(html_text[start:end])
        versions.append({"version": ver, "date": date,
                         "changes": changes or [f"Release {ver}"]})
    return versions


def _scrape_bold_or_dt_for_versions(html_text: str) -> list[dict]:
    """Fallback: some changelog pages mark releases with bold text, not headings."""
    matches = list(re.finditer(
        r'<(b|strong|dt)[^>]*>(.*?)</\1>', html_text, re.IGNORECASE | re.DOTALL))
    versions = []
    for i, m in enumerate(matches):
        text = _strip_html(m.group(2))
        vm = _GENERIC_VERSION_IN_TEXT_RE.search(text)
        if not vm:
            continue
        ver = vm.group(1)
        dm = _GENERIC_DATE_RE.search(text)
        date = _normalize_date_str(dm.group(0)) if dm else ""
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else min(start + 4000, len(html_text))
        changes = _extract_generic_block_changes(html_text[start:end])
        versions.append({"version": ver, "date": date,
                         "changes": changes or [f"Release {ver}"]})
    return versions


def _scrape_table_rows_for_versions(html_text: str) -> list[dict]:
    """Fallback: parse a simple table of releases (version/date/notes)."""
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html_text, re.DOTALL | re.IGNORECASE)
    versions = []
    for row in rows:
        text = _strip_html(row)
        vm = _GENERIC_VERSION_IN_TEXT_RE.search(text)
        if not vm:
            continue
        ver = vm.group(1)
        dm  = _GENERIC_DATE_RE.search(text)
        date = _normalize_date_str(dm.group(0)) if dm else ""
        remainder = text.replace(vm.group(0), "", 1)
        if dm:
            remainder = remainder.replace(dm.group(0), "", 1)
        remainder = re.sub(r'\s+', ' ', remainder).strip(" -–|\t")
        versions.append({"version": ver, "date": date,
                         "changes": [remainder] if len(remainder) > 3 else [f"Release {ver}"]})
    return versions


def _scrape_index_links_for_versions(html_text: str) -> list[tuple]:
    """Fallback: index pages that just link out to each version's own page."""
    hrefs = re.findall(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html_text,
                       re.DOTALL | re.IGNORECASE)
    candidates = []
    for href, text in hrefs:
        label = _strip_html(text)
        vm = _GENERIC_VERSION_IN_TEXT_RE.search(label) or _GENERIC_VERSION_IN_TEXT_RE.search(href)
        if vm:
            candidates.append((vm.group(1), href))
    return candidates


def _has_meaningful_entry(versions: list[dict]) -> bool:
    return any(_is_meaningful_changelog(v.get("changes", [])) for v in versions)


def _generic_release_page_scraper(url: str, pkg_name: str) -> Optional[dict]:
    """Best-effort, site-agnostic scraper for arbitrary "release notes" pages."""
    body = http_get(url, timeout=14)
    if not body:
        return None
    clean = _strip_noise_blocks(body)

    versions = _scrape_headings_for_versions(clean)
    if not _has_meaningful_entry(versions):
        alt = _scrape_bold_or_dt_for_versions(clean)
        if _has_meaningful_entry(alt):
            versions = alt
    if not _has_meaningful_entry(versions):
        alt = _scrape_table_rows_for_versions(clean)
        if _has_meaningful_entry(alt):
            versions = alt

    if _has_meaningful_entry(versions):
        versions.sort(key=lambda v: _tag_selection_key(v.get("version", "")), reverse=True)
        return {"versions": versions[:8], "source": f"Release notes (auto-detected) — {url}"}

    # index of links to per-version pages
    index_links = _scrape_index_links_for_versions(clean)
    if len(index_links) < _GENERIC_MIN_ENTRIES:
        return None

    # Try two candidates for "the newest"
    by_doc_order = index_links[0]
    by_version   = max(index_links, key=lambda t: _tag_selection_key(t[0]))
    for newest_ver, newest_href in dict.fromkeys([by_doc_order, by_version]):
        sub_url = urllib.parse.urljoin(url, newest_href)
        if sub_url == url:
            continue
        sub_body = http_get(sub_url, timeout=14)
        if not sub_body:
            continue
        sub_clean = _strip_noise_blocks(sub_body)
        sub_versions = _scrape_headings_for_versions(sub_clean)
        if not _has_meaningful_entry(sub_versions):
            # The whole subpage IS the notes for this one version
            changes = _extract_generic_block_changes(sub_clean)
            if changes:
                sub_versions = [{"version": newest_ver, "date": "", "changes": changes}]
        if _has_meaningful_entry(sub_versions):
            sub_versions.sort(key=lambda v: _tag_selection_key(v.get("version", "")), reverse=True)
            return {"versions": sub_versions[:8],
                    "source": f"Release notes (auto-detected, followed index link) — {sub_url}"}
    return None


# ─── Changelog: upstream GitHub / GitLab ─────────────────────────────────────

def _repo_name_plausible(pkg_name: str, repo_path: str) -> bool:
    """Sanity check before trusting a repo discovered by scanning a homepage."""
    repo_name = repo_path.rstrip("/").split("/")[-1].lower()
    pkg_lower = pkg_name.lower()
    # Normalise common separators so "gnome-shell" ~ "gnomeshell" etc. match
    norm_repo = re.sub(r'[-_.]', '', repo_name)
    norm_pkg  = re.sub(r'[-_.]', '', pkg_lower)
    if norm_pkg == norm_repo:
        return True
    # Allow the package name to be a prefix/suffix of the repo
    if len(norm_pkg) >= 4 and (norm_repo.startswith(norm_pkg) or norm_pkg.startswith(norm_repo)):
        return True
    return False

def _find_repo_link_in_page(url: str) -> Optional[tuple]:
    """Scan a homepage for the project's own source-code repository link."""
    body = http_get(url, timeout=8)
    if not body:
        _dbg(f"[homepage scan] could not fetch {url}")
        return None

    # helper to normalize a repo URL/path: strip '/-/' and known resource suffixes
    def normalize_repo_from_href(href: str):
        # Remove query/fragment
        h = href.split("#", 1)[0].split("?", 1)[0]
        # If it contains '/-/', keep only the left side (repo root)
        if "/-/" in h:
            h = h.split("/-/", 1)[0]
        # Remove common trailing resource tokens
        for tok in ("/releases", "/tags", "/issues", "/pulls", "/commits", "/blob", "/tree", "/work_items", "/raw"):
            idx = h.find(tok)
            if idx != -1:
                h = h[:idx]
        return h.rstrip("/")

    # Find all href attributes, including unquoted values.
    hrefs = re.findall(r'href\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s>]+))', body)
    hrefs = [h for match in hrefs for h in match if h]
    seen = set()
    for raw in hrefs:
        if not raw or raw in seen:
            continue
        seen.add(raw)
        # Resolve protocol-relative and relative URLs
        if raw.startswith("//"):
            raw_full = "https:" + raw
        elif raw.startswith("http://") or raw.startswith("https://"):
            raw_full = raw
        else:
            # Make relative URLs absolute using the homepage base
            try:
                raw_full = urllib.parse.urljoin(url, raw)
            except Exception:
                raw_full = raw
        low = raw_full.lower()
        # Filter only GitHub / GitLab-looking links
        if "github.com" not in low and "gitlab" not in low and not any(low.endswith(k) for k in ("invent.kde.org","source.kde.org")):
            continue

        _dbg(f"[homepage scan] candidate href: {raw_full}")

        # Attempt to parse host+path
        try:
            p = urllib.parse.urlparse(raw_full)
        except Exception:
            _dbg(f"[homepage scan] parse failed for {raw_full}")
            continue
        host = (p.netloc or "").lower()
        path = p.path or ""
        path = path.lstrip("/")

        # If host is github
        if "github.com" in host:
            # require at least owner/repo
            parts = [s for s in path.split("/") if s and s != "-"]
            if len(parts) >= 2:
                repo = "/".join(parts[:len(parts)])  # keep nested groups if present
                # strip .git suffix
                repo = repo.removesuffix(".git").rstrip("/")
                _dbg(f"[homepage scan] github candidate -> {repo}")
                return ("github", repo)
            else:
                _dbg(f"[homepage scan] github candidate rejected (not owner/repo): {raw_full}")
                continue

        # If host looks like a GitLab instance
        if "gitlab" in host or host.endswith("invent.kde.org") or host.endswith("source.kde.org") or host.endswith("gitlab.gnome.org"):
            # Normalize and strip trailing pieces like /-/work_items
            base = normalize_repo_from_href(raw_full)
            # base may be something like https://gitlab.gnome.org/GNOME/gnome-calendar
            m = re.match(r'https?://([^/]+)/(.+)', base)
            if not m:
                _dbg(f"[homepage scan] gitlab candidate parse fail: {base}")
                continue
            ghost, gpath = m.group(1), m.group(2)
            parts = [s for s in gpath.split("/") if s and s != "-"]
            if len(parts) >= 2:
                repo = "/".join(parts)  # keep subgroup/project if present
                repo = repo.removesuffix(".git").rstrip("/")
                _dbg(f"[homepage scan] gitlab candidate -> host={ghost} repo={repo}")
                return ("gitlab", ghost, repo)
            else:
                _dbg(f"[homepage scan] gitlab candidate rejected (not group/project): {raw_full}")
                continue

    _dbg("[homepage scan] no repo link found")
    return None


def _find_repo_via_homepage(url: str, pkg_name: str = "") -> Optional[tuple]:
    """Resolve a package's source repo by following its homepage URL."""
    if not url:
        return None

    if "github.com/" in url:
        m = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", url)
        if m:
            repo = m.group(1).rstrip("/")
            if not pkg_name or _repo_name_plausible(pkg_name, repo):
                return ("github", repo)
            return None

    gl = re.search(r"(gitlab\.[A-Za-z0-9.-]+)/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", url)
    if gl:
        repo = gl.group(2).rstrip("/")
        if not pkg_name or _repo_name_plausible(pkg_name, repo):
            return ("gitlab", gl.group(1), repo)
        return None

    found = _find_repo_link_in_page(url)
    if found:
        repo = found[1] if found[0] == "github" else found[2]
        if not pkg_name or _repo_name_plausible(pkg_name, repo):
            return found
        return None

    if "sourceforge.net" in url:
        sf = re.search(r"sourceforge\.net/projects?/([^/\s]+)", url)
        if sf:
            found = _find_repo_link_in_page(f"https://sourceforge.net/p/{sf.group(1)}/code/")
            if found:
                repo = found[1] if found[0] == "github" else found[2]
                if not pkg_name or _repo_name_plausible(pkg_name, repo):
                    return found

    return None


def _github_releases(repo: str, _pkg_name: str) -> Optional[dict]:
    data = http_get_json(f"https://api.github.com/repos/{repo}/releases?per_page=8")
    if data and isinstance(data, list) and data:
        versions = []
        for rel in data[:6]:
            ver  = _extract_version_from_tag(rel.get("tag_name") or "")
            date = (rel.get("published_at") or "")[:10]
            body = rel.get("body") or ""
            versions.append({"version": ver, "date": date,
                             "changes": _parse_md_changelog(body)[:10] or [f"Release {ver}"]})
        if versions:
            return {"versions": versions, "source": f"GitHub Releases — {repo}"}

    # Last resort: bare tags with no content
    data = http_get_json(f"https://api.github.com/repos/{repo}/tags?per_page=8")
    if data and isinstance(data, list) and data:
        return {"versions": [{"version": _extract_version_from_tag(t.get("name") or ""),
                              "date": "", "changes": ["See GitHub for release notes."]}
                             for t in data[:6]],
                "source": f"GitHub tags — {repo}"}
    return None


def _is_meaningful_changelog(changes: list[str]) -> bool:
    """Detect if changelog content is actually meaningful or just boilerplate."""
    if not changes:
        return False
    # Consider a changelog meaningful only if at least one line looks descriptive
    for change in changes:
        if not change:
            continue
        # Skip URLs and obvious 'see X' fallbacks
        low = change.strip().lower()
        if low.startswith("http://") or low.startswith("https://"):
            continue
        if re.match(r'^see\s+https?://\S+\s+for\s+details\.?$', low):
            continue
        if _is_noise_line(change):
            continue
        # Require a reasonably descriptive line (length + words)
        if len(change.strip()) >= 20 and len(change.split()) >= 3:
            return True
    return False


def _extract_version_from_tag(tag_name: str) -> str:
    """Normalise a tag name into a readable, comparison-friendly version."""
    t = (tag_name or "").strip()
    t = t.removeprefix("v").removeprefix("V")

    for _ in range(3):
        if _looks_like_clean_version(t):
            break
        stripped = re.sub(r'^release[-_]', '', t, flags=re.I)
        if stripped == t:
            m = re.match(r'^[A-Za-z][A-Za-z0-9.]*-(.+)$', t)
            stripped = m.group(1) if m else t
        if stripped == t:
            break
        t = stripped

    # GNOME-style: PROJECT_NAME_X_Y_Z -> trailing numeric run with dots
    m = re.search(r'((?:\d+_)+\d+)$', t)
    if m:
        return m.group(1).replace("_", ".")
    return t


def _version_sort_key(ver: str) -> tuple:
    """Parse a version-ish string into a tuple that sorts in semantic-version order."""
    t = (ver or "").strip().removeprefix("v").removeprefix("V")
    parts = re.split(r"[._-]", t)
    key: list[tuple[int, object]] = []
    for part in parts:
        if part.isdigit():
            key.append((0, int(part)))
        elif part:
            key.append((1, part.lower()))
    return tuple(key)


_CLEAN_VERSION_RE = re.compile(
    r'^v?\d+(\.\d+){0,4}(?:[-.](?:alpha|beta|rc|pre|dev)\d*)?(?:-\d+)?$',
    re.IGNORECASE)


def _looks_like_clean_version(tag: str) -> bool:
    """True if a tag looks like a straightforward version number."""
    return bool(_CLEAN_VERSION_RE.match((tag or "").strip()))


def _tag_selection_key(ver: str) -> tuple:
    """Sort key for choosing the best (most likely genuinely newest) tag."""
    return (_looks_like_clean_version(ver), _version_sort_key(ver))


def _strip_pacman_epoch_pkgrel(v: str) -> str:
    """Pacman version strings are formatted [epoch:]pkgver-pkgrel."""
    v = (v or "").strip()
    if ":" in v:
        v = v.split(":", 1)[1]
    # pkgver itself cannot contain "-" per Arch packaging conventions
    if "-" in v:
        v = v.rsplit("-", 1)[0]
    return v


def _target_version_satisfied(versions: list[dict], target_version: str) -> bool:
    """Sanity check: is the newest known version at least as new as the target?"""
    if not versions or not target_version:
        return True
    tgt_key = _version_sort_key(_strip_pacman_epoch_pkgrel(target_version))
    if not tgt_key:
        return True
    top_key = _version_sort_key(versions[0].get("version", ""))
    if not top_key:
        return True
    n = min(len(tgt_key), len(top_key))
    return top_key[:n] >= tgt_key[:n]


def _versions_contain_target(versions: list[dict], target_version: str) -> bool:
    """Whether the target version shows up verbatim in the changelog entries."""
    if not versions or not target_version:
        return True   # can't judge — don't manufacture a false negative
    tgt_key = _version_sort_key(_strip_pacman_epoch_pkgrel(target_version))
    if not tgt_key:
        return True
    for v in versions:
        cand_key = _version_sort_key(v.get("version", ""))
        if not cand_key:
            continue
        n = min(len(tgt_key), len(cand_key))
        if n and cand_key[:n] == tgt_key[:n]:
            return True
    return False


# ─── GitLab hosts with known API blocking but git access working ──────────────
_GIT_FIRST_HOSTS = {"invent.kde.org", "source.kde.org"}


def _gitlab_releases(host: str, repo: str, _pkg_name: str,
                     target_version: str = "") -> Optional[dict]:
    """Try, in priority order: GitLab Releases API, Tags API, a NEWS/CHANGELOG file, then raw git tags."""
    best_stale: Optional[dict] = None        # newest entry looked older than target
    best_unconfirmed: Optional[dict] = None  # satisfies target, but exact version not literally listed

    def _consider(result: Optional[dict]) -> Optional[dict]:
        """Sort a candidate result's versions newest-first."""
        nonlocal best_stale, best_unconfirmed
        if not result or not result.get("versions"):
            return None
        result["versions"].sort(
            key=lambda v: _tag_selection_key(v.get("version", "")), reverse=True)
        if _target_version_satisfied(result["versions"], target_version):
            if target_version and not _versions_contain_target(result["versions"], target_version):
                # Newest entry is at least as new as the target
                result["_version_unconfirmed"] = True
                _dbg(f"[gitlab] {result.get('source')}: satisfies target "
                     f"{target_version!r} but it isn't literally listed — "
                     f"keeping as fallback, trying next source for a confirmed match")
                if best_unconfirmed is None:
                    best_unconfirmed = result
                return None
            return result
        _dbg(f"[gitlab] {result.get('source')}: newest found "
             f"{result['versions'][0].get('version')!r} looks older than "
             f"target {target_version!r} — trying next source")
        if best_stale is None:
            best_stale = result
        return None

    # For known problematic hosts, try git access before API calls
    if host in _GIT_FIRST_HOSTS:
        r = _consider(_gitlab_git_fallback(host, repo, _pkg_name))
        if r:
            return r

    encoded = urllib.parse.quote(repo, safe="")

    # 2. Releases API
    data = http_get_json(f"https://{host}/api/v4/projects/{encoded}/releases?per_page=20")
    if data and isinstance(data, list) and data:
        versions = []
        for rel in data:
            ver  = _extract_version_from_tag(rel.get("tag_name") or "")
            date = (rel.get("released_at") or rel.get("created_at") or "")[:10]
            desc = rel.get("description") or ""
            changes = _parse_md_changelog(desc)
            versions.append({"version": ver, "date": date,
                             "changes": changes[:10] or [desc[:120].replace("\n"," ")] or [f"Release {ver}"]})
        versions.sort(key=lambda v: _tag_selection_key(v.get("version", "")), reverse=True)
        versions = versions[:6]
        if any(_is_meaningful_changelog(v.get("changes", [])) for v in versions):
            r = _consider({"versions": versions, "source": f"GitLab Releases — {host}/{repo}"})
            if r:
                return r

    # 3. Tags API
    tags = http_get_json(f"https://{host}/api/v4/projects/{encoded}/repository/tags?per_page=20")
    if tags and isinstance(tags, list) and tags:
        candidates = []
        for tag in tags:
            ver = _extract_version_from_tag(tag.get("name") or "")
            msg = tag.get("message") or (tag.get("commit") or {}).get("message", "")
            if not msg or "no release notes" in msg.lower():
                continue
            changes = [l.strip("- ").strip() for l in msg.splitlines()
                       if l.strip() and not l.strip().startswith("#")
                       and not _is_pgp_garbage(l)
                       # Drop the tag's generic "Release version X.Y.Z" line — no real content
                       and not re.match(r'^release\s+version\s+[\d.]+\s*$', l.strip(), re.I)]
            if changes:
                candidates.append({
                    "version": ver,
                    "date": ((tag.get("commit") or {}).get("created_at") or "")[:10],
                    "changes": changes[:8],
                })
        candidates.sort(key=lambda v: _tag_selection_key(v.get("version", "")), reverse=True)
        candidates = candidates[:6]
        if any(_is_meaningful_changelog(v.get("changes", [])) for v in candidates):
            r = _consider({"versions": candidates, "source": f"GitLab tags — {host}/{repo}"})
            if r:
                return r

    # 4. NEWS/CHANGELOG file in the repo root
    r = _consider(_fetch_gitlab_news_file(host, repo))
    if r:
        return r

    r = _consider(_gitlab_git_fallback(host, repo, _pkg_name))
    if r:
        return r

    # Nothing produced a fully-confirmed match
    if best_unconfirmed:
        return best_unconfirmed
    if best_stale:
        best_stale["source"] += "  [may not include the latest release]"
        best_stale["_version_mismatch"] = True
        return best_stale
    return None


def _gitlab_default_branch(host: str, repo: str) -> Optional[str]:
    """Look up the project's actual default branch via the GitLab API."""
    encoded = urllib.parse.quote(repo, safe="")
    data = http_get_json(f"https://{host}/api/v4/projects/{encoded}")
    if data and isinstance(data, dict):
        db = data.get("default_branch")
        if db:
            return db
    return None


def _fetch_gitlab_news_file(host: str, repo: str) -> Optional[dict]:
    """Try NEWS/CHANGELOG files via GitLab's raw-file endpoint."""
    filenames = ["NEWS", "CHANGELOG", "NEWS.md", "CHANGELOG.md",
                 "CHANGES", "CHANGES.md", "HISTORY", "HISTORY.md"]
    branch = _gitlab_default_branch(host, repo) or "main"
    urls = [f"https://{host}/{repo}/-/raw/{branch}/{fname}" for fname in filenames]
    pages = _fetch_parallel(urls, timeout=10)
    found_any_body = False
    for url in urls:
        body = pages.get(url)
        if body and len(body) > 50:
            found_any_body = True
            low = body.lower()
            if "<html" in low or _is_bot_protection_page(body):
                continue
            result = _scrape_github_raw_changelog(body) if _looks_like_markdown_changelog(body) \
                     else _scrape_text_file(body)
            if result and result.get("versions"):
                # Files aren't guaranteed to list entries newest-first — re-sort by version
                result["versions"].sort(
                    key=lambda v: _tag_selection_key(v.get("version", "")),
                    reverse=True)
                fname = url.rsplit("/", 1)[-1]
                result["source"] = f"GitLab {fname} — {host}/{repo}"
                _dbg(f"[gitlab] NEWS/CHANGELOG file (HTTP): found and parsed {fname}")
                return result
    if found_any_body:
        _dbg(f"[gitlab] NEWS/CHANGELOG file (HTTP): found a file but couldn't "
             f"parse any versions from it")
    else:
        _dbg(f"[gitlab] NEWS/CHANGELOG file (HTTP): none of the common "
             f"filenames exist on branch {branch!r} at {host}/{repo}")
    return None


def _gitlab_git_fallback(host: str, repo: str, _pkg_name: str) -> Optional[dict]:
    if not cmd_exists("git"):
        return None
    if _host_is_down(host):
        _dbg(f"[git] skipped: {host} is temporarily marked unavailable")
        _note_incomplete()
        return None
    repo_url = f"https://{host}/{repo}.git"
    git_deadline = time.monotonic() + _GIT_BUDGET_S

    def _t(cap: float) -> float:
        return max(1.0, min(cap, git_deadline - time.monotonic()))

    out, err, rc = run_git(["git", "ls-remote", "--tags", "--refs", repo_url], timeout=_t(10))
    if rc != 0 or not out:
        _dbg(f"[git] ls-remote {host}/{repo}: {err or 'no tags'}")
        if err == "timeout":
            _note_net_failure(host, TimeoutError("git ls-remote timed out"))
        return None

    tags: list[tuple[str, str]] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, ref = parts
        if not ref.startswith("refs/tags/"):
            continue
        if ref.endswith("^{}"):
            continue
        tag = ref[len("refs/tags/"):]
        tags.append((tag, sha))
    if not tags:
        return None

    # Reuse the shared version-sort key instead of a separate local copy
    tags.sort(key=lambda tr: _tag_selection_key(tr[0]), reverse=True)
    tags = tags[:6]
    versions = []
    with tempfile.TemporaryDirectory(prefix="pakchan-git-") as tmpdir:
        if run_git(["git", "init", "--bare", "--quiet", tmpdir], timeout=_t(5))[2] != 0:
            return None
        git = ["git", "-C", tmpdir]
        if run_git(git + ["remote", "add", "origin", repo_url], timeout=_t(5))[2] != 0:
            return None

        # One fetch for all tags
        refspecs = [f"refs/tags/{t}:refs/tags/{t}" for t, _ in tags]
        _, ferr, frc = run_git(git + ["fetch", "--quiet", "--depth", "1", "--no-tags",
                                      "--filter=blob:none", "origin", *refspecs],
                               timeout=_t(15))
        if frc != 0:
            _dbg(f"[git] fetch {host}/{repo}: {ferr[:120]}")
            if ferr == "timeout":
                _note_net_failure(host, TimeoutError("git fetch timed out"))
            return None

        for tag, _sha in tags:
            if time.monotonic() > git_deadline:
                _dbg("[git] time budget used up")
                _note_incomplete()
                break
            date_out, _, date_rc = run_git(git + ["show", "-s", "--format=%cI", f"refs/tags/{tag}"], timeout=_t(5))
            body_out, _, body_rc = run_git(git + ["show", "-s", "--format=%B", f"refs/tags/{tag}"], timeout=_t(5))
            if date_rc != 0 or body_rc != 0:
                continue
            date = date_out.strip().splitlines()[0] if date_out.strip() else ""
            raw_changes = _parse_md_changelog(body_out)[:10]
            # Filter out noisy lines from parsed changes
            if raw_changes:
                raw_changes = [c for c in raw_changes if not _is_noise_line(c)]
            # If parsed changes are empty or noisy, try to pick a non-noise line
            if not raw_changes:
                lines = [line.strip() for line in body_out.splitlines() if line.strip()]
                picked = None
                for ln in lines:
                    if _is_noise_line(ln):
                        continue
                    picked = ln
                    break
                if picked:
                    raw_changes = [picked]
            # Only include this tag if it contains meaningful changelog lines
            if raw_changes and _is_meaningful_changelog(raw_changes):
                versions.append({
                    "version": _extract_version_from_tag(tag),
                    "date": date,
                    "changes": raw_changes,
                })
            if len(versions) >= 6:
                break
    if versions:
        return {"versions": versions,
                "source": f"GitLab git — {host}/{repo}"}
    # No meaningful annotated tags found via git fallback
    return None

def _upstream_changelog(url: str, pkg_name: str, version: str) -> Optional[dict]:
    name = pkg_name.lower()
    # NOTE: mappings are checked centrally via _check_mappings_first
    if name in KNOWN_CUSTOM:
        entry = KNOWN_CUSTOM[name]
        r = _scrape_custom(pkg_name, entry, version)
        if r and r.get("versions") and _target_version_satisfied(r["versions"], version):
            if version and not _versions_contain_target(r["versions"], version):
                r["_version_unconfirmed"] = True
            return r
        url = entry.get("url", "")
        if url:
            return {
                "versions": [{"version": version, "date": "",
                              "changes": [f"See {url} for details."]}],
                "source": f"Custom ({entry.get('parser', '')}) — {url}",
                "_link_only": True,
                "_link_url": url,
            }
    if name in KNOWN_RELEASE_PAGES:
        page_url = KNOWN_RELEASE_PAGES[name]
        r = _generic_release_page_scraper(page_url, pkg_name)
        if r and r.get("versions") and _target_version_satisfied(r["versions"], version):
            if version and not _versions_contain_target(r["versions"], version):
                r["_version_unconfirmed"] = True
            return r
        return {
            "versions": [{"version": version, "date": "",
                          "changes": [f"See {page_url} for details."]}],
            "source": f"Release page — {page_url}",
            "_link_only": True,
            "_link_url": page_url,
        }

    # Known GitLab/GitHub mappings: return a link-only fallback for direct callers
    if name in KNOWN_GITLAB_REPOS:
        host, repo = KNOWN_GITLAB_REPOS[name]
        url = f"https://{host}/{repo}/-/releases"
        return {
            "versions": [{"version": version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"GitLab repo mapping — {host}/{repo}",
            "_link_only": True,
            "_link_url": url,
        }
    if name in KNOWN_GITHUB_REPOS:
        repo = KNOWN_GITHUB_REPOS[name]
        url = f"https://github.com/{repo}/releases"
        return {
            "versions": [{"version": version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"GitHub repo mapping — {repo}",
            "_link_only": True,
            "_link_url": url,
        }

    if not url:
        return None
    # 1. Direct GitHub URL
    gh = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", url)
    if gh:
        r = _github_releases(gh.group(1).rstrip("/").removesuffix(".git"), pkg_name)
        if r and r.get("versions"): return r
    # 2. Direct GitLab URL
    gl = re.search(r"(gitlab\.[^/\s]+)/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", url)
    if gl:
        r = _gitlab_releases(gl.group(1), gl.group(2).removesuffix(".git"), pkg_name, version)
        if r and r.get("versions"): return r
    # 3. Homepage scraping (GitHub or GitLab — whichever the homepage links to)
    fallback_link = None
    found = _find_repo_via_homepage(url, pkg_name)
    if found:
        if found[0] == "github":
            r = _github_releases(found[1], pkg_name)
            if r and r.get("versions"): return r
            fallback_link = f"https://github.com/{found[1]}/releases"
        else:
            r = _gitlab_releases(found[1], found[2], pkg_name, version)
            if r and r.get("versions"): return r
            fallback_link = f"https://{found[1]}/{found[2]}/-/releases"
    if fallback_link:
        return {
            "versions": [{"version": version or "", "date": "",
                          "changes": [f"See {fallback_link} for details."]}],
            "source": "Upstream repo link",
            "_link_only": True,
            "_link_url": fallback_link,
        }
    return None

# ─── Per-source changelog functions ──────────────────────────────────────────

def _check_mappings_first(pkg: Package) -> Optional[dict]:
    """Always check every mapping type BEFORE any other source."""
    name = pkg.name.lower()

    target_version = pkg.new_version or pkg.version

    if name in KNOWN_GITHUB_REPOS:
        repo = KNOWN_GITHUB_REPOS[name]
        r = _github_releases(repo, pkg.name)
        if r and r.get("versions"):
            if target_version and not _versions_contain_target(r["versions"], target_version):
                r["_version_unconfirmed"] = True
            return r
        url = f"https://github.com/{repo}/releases"
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"GitHub repo mapping — {repo}",
            "_link_only": True,
            "_link_url": url,
        }

    if name in KNOWN_GITLAB_REPOS:
        host, repo = KNOWN_GITLAB_REPOS[name]
        r = _gitlab_releases(host, repo, pkg.name, target_version)
        if r and r.get("versions"):
            return r
        url = f"https://{host}/{repo}/-/releases"
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"GitLab repo mapping — {host}/{repo}",
            "_link_only": True,
            "_link_url": url,
        }

    # Custom parser (text_file, github_raw, mozilla, filezilla, …)
    if name in KNOWN_CUSTOM:
        entry = KNOWN_CUSTOM[name]
        url   = entry.get("url", "")
        r     = _scrape_custom(pkg.name, entry, target_version)
        # version check
        if r and r.get("versions") and _target_version_satisfied(r["versions"], target_version):
            if target_version and not _versions_contain_target(r["versions"], target_version):
                r["_version_unconfirmed"] = True
            return r
        # Mapping exists but scraping failed or failed the version check
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"Custom ({entry.get('parser', '')}) — {url}",
        }

    # Dedicated release page: try the generic scraper before falling back to a link
    if name in KNOWN_RELEASE_PAGES:
        url = KNOWN_RELEASE_PAGES[name]
        r = _generic_release_page_scraper(url, pkg.name)
        # No further fallback method
        if r and r.get("versions") and _target_version_satisfied(r["versions"], target_version):
            if target_version and not _versions_contain_target(r["versions"], target_version):
                r["_version_unconfirmed"] = True
            return r
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"Release page — {url}",
            "_link_only": True,
            "_link_url": url,
        }

    return None



def _is_pgp_garbage(text: str) -> bool:
    """Return True if a line looks like PGP signature noise or base64 blob."""
    t = text.strip()
    if not t:
        return False
    # Explicit PGP markers
    if re.search(r'BEGIN PGP|END PGP|Hash: SHA|Comment: ', t):
        return True
    # Long base64-only lines (PGP signature body — 60+ chars, only base64 chars)
    if len(t) > 40 and re.match(r'^[A-Za-z0-9+/=]{40,}$', t):
        return True
    # Common PGP base64 line prefixes (iQIZ, iHUE, iIQI, iQEz, etc.)
    if re.match(r'^i[A-Z0-9]{3}[A-Z]', t) and len(t) > 30:
        return True
    return False


def _is_noise_line(text: str) -> bool:
    """Return True if a single changelog line looks like noise (PGP, tag metadata, base64 blobs, or very short tokens)."""
    if not text:
        return True
    t = text.strip()
    if _is_pgp_garbage(t):
        return True
    # Tag/Tagger/Release markers
    if re.match(r'^(tag|tagger|release|signed-off-by|co-authored-by)\b', t, re.I):
        return True
    # Bare version-like lines
    if re.match(r'^(version\b|v\b)\s*\d', t, re.I) or re.match(r'^\d+(?:[\.\-]\d+)+$', t):
        return True
    # Lines that mention GnuPG/GPG or signatures are noise
    if re.search(r'\bgnupg\b|\bgpg\b|\bpgp\b|\bsignature\b', t, re.I):
        return True
    # Short base64-like fragments (6-40 chars) are usually noise
    if 6 <= len(t) <= 40 and re.match(r'^[A-Za-z0-9+/=]+$', t):
        return True
    # Very short single-word items are likely navigation/labels
    if len(t.split()) < 2 or len(t) < 10:
        return True
    return False


def fetch_changelog_pacman(pkg: Package) -> dict:
    # 1. Always check mappings first
    r = _check_mappings_first(pkg)
    if r:
        _dbg(f"[1] mappings.json: hit ({r.get('source')})")
        return r
    _dbg("[1] mappings.json: no entry for this package")

    # 2. Local AppStream metainfo (fast, on-disk, no network) — desktop apps only
    r = _local_appstream_releases(pkg.name)
    if r:
        _dbg(f"[2] local AppStream: hit ({r.get('source')})")
        return r
    _dbg("[2] local AppStream: no usable file")

    # Steps 4/5 (known GitLab/GitHub mapping) are skipped here
    name = pkg.name.lower()
    target_version = pkg.new_version or pkg.version

    # 3. Direct GitHub/GitLab URL
    if not pkg.url:
        out, _, _ = run(["pacman", "-Si", pkg.name])
        for line in out.splitlines():
            if line.strip().startswith("URL") and ":" in line:
                pkg.url = line.partition(":")[2].strip()
                break
    _dbg(f"[3] package URL: {pkg.url or '(none)'}")
    if pkg.url:
        gh = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", pkg.url)
        if gh:
            repo = gh.group(1).rstrip("/").removesuffix(".git")
            r = _github_releases(repo, pkg.name)
            if r and r.get("versions"):
                _dbg(f"[3] direct GitHub URL: hit ({repo})")
                return r
            _dbg(f"[3] direct GitHub URL {repo}: no usable data")
        gl = re.search(r"(gitlab\.[^/\s]+)/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", pkg.url)
        if gl:
            host, repo = gl.group(1), gl.group(2).removesuffix(".git")
            r = _gitlab_releases(host, repo, pkg.name, target_version)
            if r and r.get("versions"):
                _dbg(f"[3] direct GitLab URL: hit ({host}/{repo})")
                return r
            _dbg(f"[3] direct GitLab URL {host}/{repo}: no usable data")
        if not gh and not gl:
            _dbg("[3] package URL is not a direct GitHub/GitLab link")
    else:
        _dbg("[3] no package URL to check")

    # 4. Homepage scraping for an indirect GitHub/GitLab link
    fallback_link = None
    if pkg.url:
        found = _find_repo_via_homepage(pkg.url, pkg.name)
        if found:
            if found[0] == "github":
                _dbg(f"[4] homepage scan found GitHub repo: {found[1]}")
                r = _github_releases(found[1], pkg.name)
                if r and r.get("versions"):
                    _dbg("[4] homepage-discovered repo: hit")
                    return r
                fallback_link = f"https://github.com/{found[1]}/releases"
            else:
                _dbg(f"[4] homepage scan found GitLab repo: {found[1]}/{found[2]}")
                r = _gitlab_releases(found[1], found[2], pkg.name, target_version)
                if r and r.get("versions"):
                    _dbg("[4] homepage-discovered repo: hit")
                    return r
                fallback_link = f"https://{found[1]}/{found[2]}/-/releases"
            _dbg("[4] homepage-discovered repo: no usable data")
        else:
            _dbg("[4] homepage scan: no repo link found (or rejected by plausibility check)")
    else:
        _dbg("[4] no package URL to scan")

    if fallback_link:
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {fallback_link} for details."]}],
            "source": "Upstream repo link",
            "_link_only": True,
            "_link_url": fallback_link,
        }

    # Nothing found: no PKGBUILD-history fallback for pacman-repo packages
    return {"versions": [{"version": pkg.version, "date": "",
                          "changes": ["Changelog not found."]}],
            "source": "unavailable",
            "_manual_check_url": pkg.url or None}


def fetch_changelog_aur(pkg: Package) -> dict:
    # 1. Always check mappings first
    r = _check_mappings_first(pkg)
    if r:
        _dbg(f"[1] mappings.json: hit ({r.get('source')})")
        return r
    _dbg("[1] mappings.json: no entry for this package")

    # 2. Local AppStream metainfo (fast, on-disk, no network) — desktop apps only
    r = _local_appstream_releases(pkg.name)
    if r:
        _dbg(f"[2] local AppStream: hit ({r.get('source')})")
        return r
    _dbg("[2] local AppStream: no usable file")

    # Step 4 (known GitLab/GitHub mapping) is skipped here
    name = pkg.name.lower()
    target_version = pkg.new_version or pkg.version

    # 3. Direct GitHub/GitLab URL
    if not pkg.url:
        data = http_get_json(
            f"https://aur.archlinux.org/rpc/v5/info/{urllib.parse.quote(pkg.name)}")
        if data and data.get("results"):
            pkg.url = data["results"][0].get("URL", "")
    _dbg(f"[3] package URL: {pkg.url or '(none)'}")
    if pkg.url:
        gh = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", pkg.url)
        if gh:
            repo = gh.group(1).rstrip("/").removesuffix(".git")
            r = _github_releases(repo, pkg.name)
            if r and r.get("versions"):
                _dbg(f"[3] direct GitHub URL: hit ({repo})")
                return r
            _dbg(f"[3] direct GitHub URL {repo}: no usable data")
        gl = re.search(r"(gitlab\.[^/\s]+)/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", pkg.url)
        if gl:
            host, repo = gl.group(1), gl.group(2).removesuffix(".git")
            r = _gitlab_releases(host, repo, pkg.name, target_version)
            if r and r.get("versions"):
                _dbg(f"[3] direct GitLab URL: hit ({host}/{repo})")
                return r
            _dbg(f"[3] direct GitLab URL {host}/{repo}: no usable data")
    else:
        _dbg("[3] no package URL to check")

    # 4. Homepage scraping (GitHub or GitLab)
    fallback_link = None
    if pkg.url:
        found = _find_repo_via_homepage(pkg.url, pkg.name)
        if found:
            if found[0] == "github":
                _dbg(f"[5] homepage scan found GitHub repo: {found[1]}")
                r = _github_releases(found[1], pkg.name)
                if r and r.get("versions"):
                    _dbg("[4] homepage-discovered repo: hit")
                    return r
                fallback_link = f"https://github.com/{found[1]}/releases"
            else:
                _dbg(f"[4] homepage scan found GitLab repo: {found[1]}/{found[2]}")
                r = _gitlab_releases(found[1], found[2], pkg.name, target_version)
                if r and r.get("versions"):
                    _dbg("[4] homepage-discovered repo: hit")
                    return r
                fallback_link = f"https://{found[1]}/{found[2]}/-/releases"
            _dbg("[4] homepage-discovered repo: no usable data")
        else:
            _dbg("[4] homepage scan: no repo link found (or rejected by plausibility check)")
    else:
        _dbg("[4] no package URL to scan")

    if fallback_link:
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {fallback_link} for details."]}],
            "source": "Upstream repo link",
            "_link_only": True,
            "_link_url": fallback_link,
        }

    # 5. AUR cgit fallback (PKGBUILD commit history) — absolute last resort
    versions = []
    body = http_get(
        f"https://aur.archlinux.org/cgit/aur.git/log/"
        f"?h={urllib.parse.quote(pkg.name)}&showmsg=1")
    if body:
        seen: set[str] = set()
        for subj_html, date_html in re.findall(
                r'<td class="logsubject">(.*?)</td>.*?<td class="logdate">(.*?)</td>',
                body, re.DOTALL)[:8]:
            subj = _strip_html(subj_html).strip()
            date = _strip_html(date_html).strip()[:10]
            if not subj or subj in seen or _is_pgp_garbage(subj):
                continue
            seen.add(subj)
            m   = re.search(r"(\d+[\.\d]+-\d+|\d+\.\d+[\.\d]*)", subj)
            ver = m.group(1) if m else pkg.version
            versions.append({"version": ver, "date": date, "changes": [subj]})
            if len(versions) >= 5:
                break

    if versions:
        _dbg("[5] AUR cgit log: hit")
        return {"versions": versions, "source": "AUR cgit log"}

    _dbg("[5] AUR cgit log: no usable data — giving up")
    return {"versions": [{"version": pkg.version, "date": "",
                          "changes": ["No commit history found on AUR."]}],
            "source": "unavailable",
            "_manual_check_url": pkg.url or None}


def fetch_changelog_flatpak(pkg: Package) -> dict:
    # 1. Always check mappings first (custom / release_pages)
    r = _check_mappings_first(pkg)
    if r:
        return r

    versions = []
    app_id   = pkg.name

    # 2. Flathub REST API
    data = http_get_json(
        f"https://flathub.org/api/v2/appstream/{urllib.parse.quote(app_id)}")
    if data and isinstance(data, dict):
        if not pkg.url:
            urls = data.get("project_urls") or {}
            pkg.url = urls.get("homepage") or urls.get("Homepage") or ""
        if not pkg.description:
            pkg.description = data.get("summary") or ""
        for rel in (data.get("releases") or [])[:6]:
            if not isinstance(rel, dict): continue
            ver  = str(rel.get("version") or "")
            date = str(rel.get("date") or "")[:10]
            desc = str(rel.get("description") or "")
            items = re.findall(r"<li[^>]*>(.*?)</li>", desc, re.DOTALL)
            changes = ([_strip_html(i).strip() for i in items if i.strip()]
                       if items else
                       [s.strip() for s in _strip_html(desc).split("\n") if s.strip()])
            versions.append({"version": ver, "date": date,
                             "changes": changes[:8] or [f"Release {ver}"]})

    # 3. Flathub AppStream XML CDN
    if not versions:
        xml = http_get(f"https://dl.flathub.org/repo/appstream/x86_64"
                       f"/{urllib.parse.quote(app_id)}.xml")
        if xml:
            release_blocks = re.findall(
                r'<release\b([^>]*?)(/?)>(.*?)(?:</release>|(?=<release|\Z))',
                xml, re.DOTALL)
            for attrs, self_closing, body_xml in release_blocks[:6]:
                ver_m  = re.search(r'version="([^"]+)"', attrs)
                date_m = re.search(r'date="([^"]+)"', attrs)
                if not ver_m:
                    continue
                ver  = ver_m.group(1)
                date = date_m.group(1)[:10] if date_m else ""
                body = "" if self_closing else body_xml
                items = re.findall(r"<li[^>]*>(.*?)</li>", body, re.DOTALL)
                changes = ([_strip_html(i).strip() for i in items if i.strip()]
                           if items else
                           [s.strip() for s in _strip_html(body).split("\n") if s.strip()])
                versions.append({"version": ver, "date": date,
                                 "changes": changes[:8] or [f"Release {ver}"]})

    # 4. Upstream GitHub/GitLab via package URL
    if not versions and pkg.url:
        r = _upstream_changelog(pkg.url, app_id, pkg.version)
        if r and r.get("versions"):
            return r

    if not versions:
        versions = [{"version": pkg.version, "date": "",
                     "changes": ["Release notes not available on Flathub."]}]
        return {"versions": versions, "source": "Flathub AppStream metadata",
                "_manual_check_url": pkg.url or None}
    return {"versions": versions, "source": "Flathub AppStream metadata"}


def fetch_changelog_snap(pkg: Package) -> dict:
    # 1. Always check mappings first
    r = _check_mappings_first(pkg)
    if r:
        return r

    # The Snap Store API has no changelog/release-notes field at all
    versions  = []
    snap_info: dict = {}
    store_url = None
    headers   = {"User-Agent": "Pakchan/2.0",
                 "Snap-Device-Series": "16",
                 "Snap-Device-Architecture": "amd64"}
    try:
        req = urllib.request.Request(
            f"https://api.snapcraft.io/v2/snaps/info/{urllib.parse.quote(pkg.name)}",
            headers=headers)
        with urllib.request.urlopen(req, timeout=14) as resp:
            data = json.loads(_read_capped(resp))
    except Exception:
        data = None
    if data and isinstance(data, dict):
        # Check both possible nesting locations for per-snap metadata
        snap_info = data.get("snap") if isinstance(data.get("snap"), dict) else {}
        store_url = data.get("store-url") or snap_info.get("store-url")
        seen_ver: set[str] = set()
        for entry in (data.get("channel-map") or []):
            if not isinstance(entry, dict): continue
            ver  = str(entry.get("version") or "")
            rev  = str(entry.get("revision") or "")
            date = str(entry.get("created-at") or "")[:10]
            if not ver or ver in seen_ver: continue
            seen_ver.add(ver)
            versions.append({"version": f"{ver} (rev {rev})" if rev else ver,
                              "date": date, "changes": []})
            if len(versions) >= 4: break
    store_url = store_url or f"https://snapcraft.io/{pkg.name}"

    # Candidate upstream URL
    links = {}
    if data and isinstance(data, dict):
        links = snap_info.get("links") or data.get("links") or {}
    candidates = []
    for key in ("source-code", "issues", "website"):
        for u in (links.get(key) or []):
            if u and u not in candidates:
                candidates.append(u)
    if not pkg.url:
        # Fall back to `snap info`'s local website line if the API gave nothing
        out, _, rc = run(["snap", "info", pkg.name])
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("website:"):
                    pkg.url = line.split(":", 1)[1].strip()
                    break
    if pkg.url and pkg.url not in candidates:
        candidates.append(pkg.url)

    best_link_only = None
    for url in candidates:
        r = _upstream_changelog(url, pkg.name, pkg.version)
        if r and r.get("versions"):
            if not r.get("_link_only"):
                return r
            elif best_link_only is None:
                best_link_only = r

    # No real changelog found anywhere
    fallback_url = (best_link_only.get("_link_url") if best_link_only
                    else (candidates[0] if candidates else store_url))
    if not versions:
        versions = [{"version": pkg.version, "date": "", "changes": []}]
    for v in versions:
        v["changes"] = ["Snap Store doesn't provide release notes for this package."]
    return {"versions": versions, "source": "Snap Store",
            "_manual_check_url": fallback_url}


def fetch_changelog(pkg: Package) -> dict:
    """Fix #6/#11: keyed by repo:name, respects expiry."""
    key    = pkg.cl_key
    cached = _cl_cache_get(key)
    if cached and not cached.get("_stale"):
        cached["_from_cache"] = True
        return cached

    _dbg_reset()
    ctx = _budget_begin(_FETCH_BUDGET_S)
    _dbg(f"Resolving changelog for package={pkg.name!r} repo={pkg.repo!r} "
         f"url={pkg.url!r}")
    try:
        if   pkg.repo == "pacman":  result = fetch_changelog_pacman(pkg)
        elif pkg.repo == "aur":     result = fetch_changelog_aur(pkg)
        elif pkg.repo == "flatpak": result = fetch_changelog_flatpak(pkg)
        elif pkg.repo == "snap":    result = fetch_changelog_snap(pkg)
        else:
            _dbg(f"Unknown repo type: {pkg.repo!r}")
            return {"versions": [], "error": "Unknown repo.", "source": "error",
                    "_debug": _dbg_get()}
    except FetchBudgetExceeded:
        _dbg(f"[budget] gave up after {_FETCH_BUDGET_S:.0f} s")
        if cached:      # return stale rather than nothing
            cached["_from_cache"] = True
            cached["_debug"] = _dbg_get()
            return cached
        return {"versions": [], "source": "error", "_incomplete": True, "_debug": _dbg_get(),
                "error": f"Lookup took longer than {_FETCH_BUDGET_S:.0f} s and was stopped. "
                         "Retry, or check the project's page manually."}
    except Exception as e:
        _dbg(f"EXCEPTION: {e}")
        if cached:      # return stale on error
            cached["_from_cache"] = True
            cached["_debug"] = _dbg_get()
            return cached
        return {"versions": [], "error": str(e), "source": "error", "_debug": _dbg_get()}
    finally:
        _budget_end()

    debug_trace = _dbg_get()
    if ctx.incomplete:
        # A source timed out, was rate-limited or was skipped.
        result["_incomplete"] = True
        if result.get("source") == "unavailable":
            result = {"versions": [], "source": "error", "_incomplete": True,
                      "error": "Some sources did not respond in time, so no changelog "
                               "could be confirmed. Retry in a few minutes."}
    real = (bool(result.get("versions")) and not result.get("_link_only")
            and result.get("source") != "unavailable")
    if result.get("versions") and (real or not ctx.incomplete):
        _cl_cache_set(key, dict(result)) 
    result["_debug"] = debug_trace
    return result


# ─── GTK Application ──────────────────────────────────────────────────────────

def _resolve_source_url(changelog: dict) -> Optional[str]:
    """Best-effort extraction of a real URL for the "Source:" line."""
    if changelog.get("_link_url"):
        return changelog["_link_url"]
    source = changelog.get("source", "") or ""
    m = re.search(r'https?://\S+', source)
    if m:
        return m.group(0).rstrip(".,)]")
    m = re.search(r'GitHub[^—]*—\s*([\w.-]+/[\w.-]+)', source)
    if m:
        return f"https://github.com/{m.group(1)}"
    m = re.search(r'GitLab[^—]*—\s*([\w.\-]+)/([\w.\-]+/[\w.\-]+)', source)
    if m:
        return f"https://{m.group(1)}/{m.group(2)}"
    return None


SORT_OPTIONS = ["Relevance", "A → Z", "Z → A", "Size ↓", "Size ↑", "Updates first"]

# Curated list of well-known apps to fill the "All" tab
POPULAR_PACMAN_NAMES = [
    "firefox", "thunderbird", "libreoffice-fresh", "gimp", "inkscape",
    "blender", "vlc", "mpv", "obs-studio", "kdenlive", "audacity",
    "krita", "shotcut", "handbrake", "gnome-boxes", "virtualbox", "wine",
    "htop", "neofetch", "keepassxc", "transmission-gtk", "qbittorrent",
    "filezilla", "gparted", "timeshift", "bleachbit", "steam", "lutris",
    "gedit", "kate", "geany", "flameshot", "peek", "deluge", "remmina",
    "digikam", "shotwell", "darktable", "rawtherapee", "musescore",
    "godot", "syncthing", "nextcloud-client",
]
POPULAR_AUR_NAMES = [
    "visual-studio-code-bin", "sublime-text-4", "discord", "spotify",
    "google-chrome", "slack-desktop", "zoom", "postman-bin", "dbeaver",
    "brave-bin", "opera", "insomnia-bin", "android-studio", "dropbox",
    "onlyoffice-bin", "bitwarden",
]


class PakchanApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="sk.mayday.pakchan")
        self.connect("activate", self.on_activate)

    def on_activate(self, app):
        PakchanWindow(application=app).present()


class PakchanWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Pakchan")
        self.set_default_size(1180, 760)

        self.all_packages:  list[Package] = []
        self.filtered:      list[Package] = []
        self.selected_pkg:  Optional[Package] = None
        self._cl_inflight:     set[str] = set()
        self._enrich_inflight: set[str] = set()
        self.current_tab    = "changelog"
        self.current_filter = "installed"
        self.current_sort   = SORT_OPTIONS[0]
        self._sync_ok       = True
        # Pacman-repo package browsing/search-to-install support
        self.pacman_sync_full:     dict[str, dict]  = {}
        self._available_cache:     dict[str, Package] = {}
        self.search_extra_results: list[Package]    = []

        self._build_ui()
        self._load_packages()

    # ── CSS ───────────────────────────────────────────────────────────────────

    def _css(self):
        p = Gtk.CssProvider()
        css = b"""
        .badge-pacman  {background:#E3F2FD;color:#1565C0;border-radius:4px;padding:1px 6px;font-size:11px;}
        .badge-aur     {background:#F3E5F5;color:#6A1B9A;border-radius:4px;padding:1px 6px;font-size:11px;}
        .badge-flatpak {background:#E8F5E9;color:#2E7D32;border-radius:4px;padding:1px 6px;font-size:11px;}
        .badge-snap    {background:#FFF3E0;color:#E65100;border-radius:4px;padding:1px 6px;font-size:11px;}
        .has-update    {color:@success_color;font-weight:bold;}
        .stale-warn    {color:@warning_color;font-style:italic;font-size:11px;}
        .mono          {font-family:monospace;font-size:12px;}
        .sidebar-hdr   {font-size:11px;font-weight:bold;
                        color:alpha(@foreground_color,0.45);padding:10px 12px 3px;}
        .active-filter {font-weight:bold;color:@accent_color;}
        .dep-tag       {font-size:10px;color:alpha(@foreground_color,0.4);}
        .update-panel  {background:alpha(@foreground_color,0.03);
                        border-top:1px solid alpha(@foreground_color,0.12);}
        .update-log    {font-family:monospace;font-size:11px;padding:6px 10px;}
        .action-install{color:@success_color;}
        .action-update {color:@accent_color;}
        .action-remove {color:@error_color;}
        """
        p.load_from_bytes(GLib.Bytes.new(css))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._css()
        self.icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # Wraps the window so toast notifications can float over anything on screen
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(root)
        self.set_content(self.toast_overlay)

        # Header bar
        hb = Adw.HeaderBar()
        hb.set_title_widget(Gtk.Label(label="Pakchan"))

        ref = Gtk.Button(icon_name="view-refresh-symbolic")
        ref.set_tooltip_text("Refresh packages")
        ref.connect("clicked", lambda _: self._load_packages())
        hb.pack_start(ref)

        # ── Hamburger menu ────────────────────────────────────────────────────
        menu_btn = Gtk.MenuButton()
        menu_btn.set_icon_name("open-menu-symbolic")
        menu_btn.set_tooltip_text("Menu")

        menu = Gio.Menu()
        menu.append("Submit changelog source…", "win.submit_source")
        menu.append("About Pakchan", "win.about")
        menu_btn.set_menu_model(menu)
        hb.pack_end(menu_btn)

        # Wire up actions
        submit_action = Gio.SimpleAction.new("submit_source", None)
        submit_action.connect("activate", self._on_submit_source)
        self.add_action(submit_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about)
        self.add_action(about_action)

        root.append(hb)

        # Fix #19: sync_names warning banner (hidden by default)
        self.sync_banner = Adw.Banner(title=(
            "⚠ Official sync DB could not be read. "
            "All packages shown as AUR/foreign. Run: sudo pacman -Sy"))
        self.sync_banner.set_revealed(False)
        root.append(self.sync_banner)

        # Loading page
        self.status_page = Adw.StatusPage()
        self.status_page.set_title("Loading packages…")
        self.status_page.set_description("Reading local package databases")
        self.status_page.set_icon_name("system-software-update-symbolic")
        self.status_page.set_vexpand(True)

        # Main layout
        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_vexpand(True)

        left = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        sidebar = self._build_sidebar()
        left.set_start_child(sidebar)
        left.set_resize_start_child(False)   # categories stay compact by default; drag to resize
        left.set_shrink_start_child(False)   # can't be dragged smaller than the sidebar's own min width
        pkg_panel = self._build_pkg_panel()
        pkg_panel.set_hexpand(True)
        left.set_end_child(pkg_panel)
        left.set_resize_end_child(True)      # extra window width goes to the package list, not the sidebar
        left.set_shrink_end_child(False)
        left.set_position(162)
        # Minimum width so the outer divider can't squeeze this whole side to nothing
        left.set_size_request(400, -1)
        self.paned.set_start_child(left)
        self.paned.set_resize_start_child(True)
        # Disable Gtk.Paned's default allow-shrink so the divider can't hide a column
        self.paned.set_shrink_start_child(False)
        self.paned.set_end_child(self._build_detail_panel())
        self.paned.set_resize_end_child(False)
        self.paned.set_shrink_end_child(False)
        self.paned.set_position(780)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.status_page, "loading")
        self.stack.add_named(self.paned,       "main")
        root.append(self.stack)

        # Integrated update panel
        self.update_revealer = Gtk.Revealer()
        self.update_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.update_revealer.set_reveal_child(False)
        self.update_revealer.set_child(self._build_update_panel())
        root.append(self.update_revealer)

        self.footer = Gtk.Label(label="Ready")
        self.footer.set_xalign(0)
        self.footer.add_css_class("dim-label")
        self.footer.set_margin_start(12)
        self.footer.set_margin_top(3)
        self.footer.set_margin_bottom(5)
        root.append(self.footer)

        # Global shortcuts: Ctrl+F focuses search, Escape clears it or the focus
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_window_key)
        self.add_controller(key_controller)

    def _on_window_key(self, controller, keyval, keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_f, Gdk.KEY_F):
            self.search.grab_focus()
            self.search.select_region(0, -1)
            return True
        if keyval == Gdk.KEY_Escape:
            if self.search.get_text():
                self.search.set_text("")
                self._do_search()
            else:
                self.listbox.grab_focus()
            return True
        return False

    def _build_sidebar(self):
        sb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sb.set_size_request(162, -1)

        lbl = Gtk.Label(label="BROWSE"); lbl.add_css_class("sidebar-hdr")
        lbl.set_xalign(0); sb.append(lbl)

        self._filter_btns: dict[str, Gtk.Button] = {}
        for key, label, icon in [
            ("all",       "All",       "view-app-grid-symbolic"),
            ("installed", "Installed", "computer-symbolic"),
            ("pacman",    "Pacman",    "system-software-update-symbolic"),
            ("aur",       "AUR",       "applications-development-symbolic"),
            ("flatpak",   "Flatpak",   "application-x-executable-symbolic"),
            ("snap",      "Snap",      "package-x-generic-symbolic"),
            ("updates",   "Updates",   "software-update-available-symbolic"),
        ]:
            btn = self._mkbtn(label, icon)
            btn.connect("clicked", self._on_filter, key)
            self._filter_btns[key] = btn
            sb.append(btn)

        self.current_filter = "installed"
        self._hl_sidebar()
        return sb

    def _mkbtn(self, label: str, icon: str) -> Gtk.Button:
        btn = Gtk.Button(); btn.add_css_class("flat")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.set_margin_start(8); row.set_margin_end(8)
        row.set_margin_top(4);   row.set_margin_bottom(4)
        row.append(Gtk.Image.new_from_icon_name(icon))
        lw = Gtk.Label(label=label)
        lw.set_xalign(0); lw.set_hexpand(True)
        row.append(lw)
        btn.set_child(row)
        return btn

    def _hl_sidebar(self):
        for key, btn in self._filter_btns.items():
            lbl = self._btn_label(btn)
            if lbl:
                if key == self.current_filter:
                    lbl.add_css_class("active-filter")
                else:
                    lbl.remove_css_class("active-filter")

    def _btn_label(self, btn) -> Optional[Gtk.Label]:
        row = btn.get_child()
        if not row: return None
        child = row.get_first_child()
        while child:
            if isinstance(child, Gtk.Label): return child
            child = child.get_next_sibling()
        return None

    def _build_pkg_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Toolbar
        tb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        tb.set_margin_start(10); tb.set_margin_end(10)
        tb.set_margin_top(8);    tb.set_margin_bottom(8)

        self.search = Gtk.Entry()
        self.search.set_placeholder_text("Search…")
        self.search.set_hexpand(True)
        self.search.connect("activate", lambda _: self._do_search())
        self.search.connect("changed", self._on_search_changed)
        self.search.connect("icon-press", self._on_search_icon_press)
        tb.append(self.search)

        sb = Gtk.Button(icon_name="system-search-symbolic")
        sb.set_tooltip_text("Search")
        sb.connect("clicked", lambda _: self._do_search())
        tb.append(sb)

        # Fix #16: sort dropdown
        self.sort_drop = Gtk.DropDown.new_from_strings(SORT_OPTIONS)
        self.sort_drop.set_tooltip_text("Sort order")
        self.sort_drop.connect("notify::selected", self._on_sort_changed)
        tb.append(self.sort_drop)

        # Fix #15: select-all hidden when not in updates view
        self.sel_all = Gtk.CheckButton(label="Select all")
        self.sel_all.connect("toggled", self._on_select_all)
        self.sel_all.set_visible(False)
        tb.append(self.sel_all)

        # Update button
        self.apply_btn = Gtk.Button(label="Update (0)")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.set_sensitive(False)
        self.apply_btn.connect("clicked", self._apply_updates)
        # Apply button stays always visible now that any checkbox can mean removal
        tb.append(self.apply_btn)

        box.append(tb)
        box.append(Gtk.Separator())

        sc = Gtk.ScrolledWindow()
        sc.set_vexpand(True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._on_row_selected)
        # Keyboard navigation
        kc = Gtk.EventControllerKey()
        kc.connect("key-pressed", self._on_list_key)
        self.listbox.add_controller(kc)
        sc.set_child(self.listbox)

        self.empty_state = Adw.StatusPage()
        self.empty_state.set_icon_name("system-search-symbolic")
        self.empty_state.set_vexpand(True)

        self.list_stack = Gtk.Stack()
        self.list_stack.set_vexpand(True)
        self.list_stack.add_named(sc, "list")
        self.list_stack.add_named(self.empty_state, "empty")
        box.append(self.list_stack)

        self.count_lbl = Gtk.Label(label="")
        self.count_lbl.add_css_class("dim-label")
        self.count_lbl.set_margin_start(10)
        self.count_lbl.set_margin_top(4); self.count_lbl.set_margin_bottom(6)
        self.count_lbl.set_xalign(0)
        box.append(self.count_lbl)
        return box

    def _build_detail_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_size_request(360, -1)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header_row.set_margin_start(12); header_row.set_margin_end(12)
        header_row.set_margin_top(10)

        self.d_icon = Gtk.Image()
        self.d_icon.set_pixel_size(48)
        self.d_icon.set_from_icon_name("package-x-generic-symbolic")
        header_row.append(self.d_icon)

        name_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        name_col.set_hexpand(True)

        self.d_name = Gtk.Label()
        self.d_name.set_markup("<b>Select a package</b>")
        self.d_name.set_xalign(0)
        self.d_name.set_margin_bottom(2)
        self.d_name.set_ellipsize(Pango.EllipsizeMode.END)
        name_col.append(self.d_name)
        header_row.append(name_col)
        box.append(header_row)

        self.d_desc = Gtk.Label(label="Click a package to view details.")
        self.d_desc.set_xalign(0)
        self.d_desc.set_margin_start(12); self.d_desc.set_margin_end(12)
        self.d_desc.set_margin_bottom(8)
        self.d_desc.add_css_class("dim-label")
        self.d_desc.set_wrap(True); self.d_desc.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        self.d_desc.set_hexpand(True)
        box.append(self.d_desc)
        box.append(Gtk.Separator())

        self.tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.tabs.set_homogeneous(True)
        self._tab_btns: dict[str, Gtk.ToggleButton] = {}
        for key, label in [("changelog","Changelog"),("info","Info"),("files","Files")]:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_tab, key)
            self._tab_btns[key] = btn
            self.tabs.append(btn)
        self._tab_btns["changelog"].set_active(True)
        box.append(self.tabs)
        box.append(Gtk.Separator())

        sc = Gtk.ScrolledWindow()
        sc.set_vexpand(True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.d_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.d_box.set_margin_start(12); self.d_box.set_margin_end(12)
        self.d_box.set_margin_top(8);    self.d_box.set_margin_bottom(8)
        sc.set_child(self.d_box)
        box.append(sc)
        return box

    # ── Package rows ──────────────────────────────────────────────────────────

    _ICON_FALLBACK = {
        "pacman":  "system-software-update-symbolic",
        "aur":     "applications-development-symbolic",
        "flatpak": "application-x-executable-symbolic",
        "snap":    "package-x-generic-symbolic",
    }

    def _icon_widget_for(self, pkg: Package) -> Gtk.Image:
        """Real app icon (PAMAC-style) when one can be resolved."""
        img = Gtk.Image()
        img.set_pixel_size(32)
        name = pkg.icon_name or pkg.name
        if name and self.icon_theme.has_icon(name):
            img.set_from_icon_name(name)
        else:
            img.set_from_icon_name(self._ICON_FALLBACK.get(pkg.repo, "package-x-generic-symbolic"))
        return img

    def _make_row(self, pkg: Package) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow(); row.pkg = pkg
        hb  = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hb.set_margin_start(8); hb.set_margin_end(8)
        hb.set_margin_top(5);   hb.set_margin_bottom(5)

        cb = Gtk.CheckButton()
        # Re-derive checkbox state from the current filter, don't trust stale flags
        if not pkg.installed:
            cb.set_active(pkg.checked)
        elif self.current_filter == "updates":
            cb.set_active(pkg.checked and not pkg.marked_remove)
        else:
            cb.set_active(pkg.checked and pkg.marked_remove)
        # Checkbox meaning changes with context
        if not pkg.installed:
            cb.set_sensitive(True)
            cb.set_tooltip_text("Select to install")
        elif self.current_filter == "updates":
            cb.set_sensitive(pkg.has_update)
            cb.set_tooltip_text("Select to update" if pkg.has_update else "")
        else:
            cb.set_sensitive(True)
            cb.set_tooltip_text("Select to uninstall")
        cb.connect("toggled", self._on_pkg_check, pkg)
        hb.append(cb)

        # Immediate visual feedback for what the checkbox will do
        action_icon = Gtk.Image()
        action_icon.set_pixel_size(16)
        self._refresh_action_icon(action_icon, pkg)
        hb.append(action_icon)
        cb.connect("toggled", self._on_action_icon_refresh, pkg, action_icon)

        hb.append(self._icon_widget_for(pkg))

        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        nb.set_hexpand(True)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nl  = Gtk.Label(label=pkg.display_name or pkg.name)
        nl.set_xalign(0); nl.set_ellipsize(Pango.EllipsizeMode.END)
        nl.add_css_class("heading"); top.append(nl)
        badge = Gtk.Label(label=pkg.repo)
        badge.add_css_class(f"badge-{pkg.repo}"); top.append(badge)
        if pkg.is_dep:
            dep = Gtk.Label(label="dep"); dep.add_css_class("dep-tag")
            top.append(dep)
        if not pkg.installed:
            tag = Gtk.Label(label="not installed"); tag.add_css_class("dep-tag")
            top.append(tag)
        nb.append(top)

        vb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        if pkg.installed:
            vl = Gtk.Label(label=pkg.version)
            vl.add_css_class("dim-label"); vl.set_xalign(0); vb.append(vl)
            if pkg.has_update:
                vb.append(Gtk.Label(label="→"))
                nl2 = Gtk.Label(label=pkg.new_version)
                nl2.add_css_class("has-update"); vb.append(nl2)
        else:
            vl = Gtk.Label(label=f"Available: {pkg.new_version}")
            vl.add_css_class("dim-label"); vl.set_xalign(0); vb.append(vl)
        nb.append(vb)
        hb.append(nb)

        if pkg.installed_size:
            sl = Gtk.Label(label=pkg.installed_size)
            sl.add_css_class("dim-label"); sl.set_halign(Gtk.Align.END)
            hb.append(sl)

        row.set_child(hb)

        right_click = Gtk.GestureClick(button=3)
        right_click.connect("pressed",
            lambda gesture, n_press, x, y: self._show_row_context_menu(pkg, row, x, y))
        row.add_controller(right_click)

        return row

    def _show_row_context_menu(self, pkg: Package, row: Gtk.ListBoxRow, x: float, y: float):
        """Right-click menu: copy name, open homepage, force-refresh changelog."""
        self.listbox.select_row(row)

        popover = Gtk.Popover()
        popover.set_parent(row)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover.set_has_arrow(False)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(4); box.set_margin_end(4)
        box.set_margin_top(4);   box.set_margin_bottom(4)

        def _add_item(label: str, callback, sensitive: bool = True):
            btn = Gtk.Button(label=label)
            btn.add_css_class("flat")
            child = btn.get_child()
            if child:
                child.set_xalign(0)
            btn.set_sensitive(sensitive)
            def _on_click(_btn):
                popover.popdown()
                callback()
            btn.connect("clicked", _on_click)
            box.append(btn)

        _add_item("Copy name", lambda: self._copy_text_to_clipboard(pkg.name))
        _add_item("Open homepage", lambda: self._open_uri(pkg.url), sensitive=bool(pkg.url))
        _add_item("Force-refresh changelog", lambda: self._context_force_refresh(pkg))

        popover.set_child(box)
        popover.popup()

    def _copy_text_to_clipboard(self, text: str):
        self.get_clipboard().set(text)
        self.footer.set_text(f"Copied “{text}” to clipboard.")

    def _open_uri(self, url: str):
        if not url:
            return
        if not _is_http_url(url):
            self.footer.set_text("Refusing to open a non-http(s) link.")
            return
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception:
            self.footer.set_text(f"Could not open {url}")

    def _context_force_refresh(self, pkg: Package):
        self._force_refresh_cl(pkg)
        self.footer.set_text(f"Changelog cache cleared for {pkg.name}.")

    # ── Sort ──────────────────────────────────────────────────────────────────

    def _relevance_score(self, p: Package) -> tuple:
        """Relevance sort key: query match quality first, then PAMAC-style defaults."""
        q = self.search.get_text().lower().strip()
        installed_rank = 0 if p.installed else 1
        if q:
            name = p.name.lower()
            if name == q:            query_rank = 0
            elif name.startswith(q): query_rank = 1
            elif q in name:          query_rank = 2
            else:                    query_rank = 3   # description-only match
            return (query_rank, installed_rank, p.name.lower())
        if self.current_filter == "all":
            installed_rank = 1 if p.installed else 0
        explicit_rank = 0 if not p.is_dep else 1
        gui_rank      = 0 if p.has_desktop_entry else 1
        return (installed_rank, explicit_rank, gui_rank, p.name.lower())

    def _sorted(self, pool: list[Package]) -> list[Package]:
        s = self.current_sort
        if   s == "Relevance":    return sorted(pool, key=self._relevance_score)
        elif s == "A → Z":        return sorted(pool, key=lambda p: p.name.lower())
        elif s == "Z → A":        return sorted(pool, key=lambda p: p.name.lower(), reverse=True)
        elif s == "Size ↓":
            return sorted(pool, key=lambda p: p.size_bytes, reverse=True)
        elif s == "Size ↑":
            return sorted(pool, key=lambda p: (p.size_bytes == 0, p.size_bytes))
        elif s == "Updates first": return sorted(pool, key=lambda p: (not p.has_update, p.name.lower()))
        return pool

    # ── List population ───────────────────────────────────────────────────────

    def _populate_list(self):
        # Cancel any in-progress population
        self._pop_generation = getattr(self, "_pop_generation", 0) + 1

        while child := self.listbox.get_first_child():
            self.listbox.remove(child)

        flt = self.current_filter
        q   = self.search.get_text().lower().strip()

        # Fix issue 2: search always across ALL packages, ignore source filter
        if q:
            pool = [p for p in self.all_packages
                    if q in p.name.lower() or q in p.description.lower()]
            self.search_extra_results = (self._find_installable_matches(q) +
                                          self._find_installable_flatpak_matches(q) +
                                          self._find_installable_aur_matches(q))
            pool = pool + self.search_extra_results
        elif flt == "updates":
            pool = [p for p in self.all_packages if p.has_update]
            self.search_extra_results = []
        elif flt == "installed":
            pool = list(self.all_packages)
            self.search_extra_results = []
        elif flt == "all":
            # "All" tab: installed packages plus most popular apps
            extra = self._popular_candidates()
            pool = list(self.all_packages) + extra
            self.search_extra_results = extra
        else:
            pool = [p for p in self.all_packages if p.repo == flt]
            self.search_extra_results = []

        pool = self._sorted(pool)
        self.filtered = pool

        if pool:
            self.list_stack.set_visible_child_name("list")
        else:
            self.list_stack.set_visible_child_name("empty")
            if q:
                self.empty_state.set_title("No matching packages")
                self.empty_state.set_description(f"Nothing matches “{q}”. Try a different search term.")
            else:
                self.empty_state.set_title("No packages here")
                self.empty_state.set_description("Nothing in this category right now.")

        # Progressive rendering in chunks so UI stays responsive
        CHUNK = 80
        gen   = self._pop_generation

        def _add_chunk(offset: int):
            if self._pop_generation != gen:
                return False   # stale — a new populate started, abort
            chunk = pool[offset: offset + CHUNK]
            for p in chunk:
                self.listbox.append(self._make_row(p))
            if offset + CHUNK < len(pool):
                GLib.idle_add(_add_chunk, offset + CHUNK)
            return False

        GLib.idle_add(_add_chunk, 0)

        # 'Select all' stays Updates-only; Apply button is always visible
        is_upd = (flt == "updates")
        self.sel_all.set_visible(is_upd)

        self._update_counts_label()
        self._update_footer()

        total = self._checked_count()
        self.apply_btn.set_sensitive(total > 0)
        self.apply_btn.set_label(self._apply_btn_label())

    def _selectable_pool(self) -> list:
        """All packages that can currently have `.checked` set."""
        return self.all_packages + self.search_extra_results

    def _checked_count(self) -> int:
        return sum(1 for p in self._selectable_pool() if p.checked)

    def _apply_btn_label(self) -> str:
        checked = [p for p in self._selectable_pool() if p.checked]
        n = len(checked)
        if not n:
            return "Update (0)"
        installs  = [p for p in checked if not p.installed]
        removals  = [p for p in checked if p.marked_remove]
        updates   = [p for p in checked if p.installed and not p.marked_remove]
        kinds = sum(bool(g) for g in (installs, removals, updates))
        if kinds > 1:
            return f"Apply ({n})"
        if removals:
            return f"Uninstall ({n})"
        if installs:
            return f"Install ({n})"
        return f"Update ({n})"

    def _find_installable_matches(self, q: str, limit: int = 150) -> list:
        """Search the pacman sync db."""
        if not q or not self.pacman_sync_full:
            return []
        installed_names = {p.name for p in self.all_packages}
        scored = []
        for name, info in self.pacman_sync_full.items():
            if name in installed_names:
                continue
            name_l = name.lower()
            if name_l == q:            rank = 0
            elif name_l.startswith(q): rank = 1
            elif q in name_l:          rank = 2
            elif q in info.get("desc", "").lower(): rank = 3
            else:                      continue
            scored.append((rank, len(name), name, info))
        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        matches = []
        for _, _, name, info in scored[:limit]:
            cache_key = f"pacman:{name}"
            pkg = self._available_cache.get(cache_key)
            if pkg is None:
                pkg = Package(
                    name=name, version="", new_version=info.get("version", "?"),
                    description=info.get("desc", ""), repo="pacman",
                    license=info.get("license", ""), url=info.get("url", ""),
                    depends=info.get("depends", ""), installed=False,
                )
                self._available_cache[cache_key] = pkg
            matches.append(pkg)
        return matches

    def _find_installable_flatpak_matches(self, q: str, limit: int = 60) -> list:
        if not q:
            return []
        installed_ids = {p.name for p in self.all_packages if p.repo == "flatpak"}
        raw = _flatpak_search(q)
        scored = []
        for r in raw:
            app_id = r["app_id"]
            if app_id in installed_ids:
                continue
            name_l = r["name"].lower()
            id_l   = app_id.lower()
            if name_l == q or id_l == q:                rank = 0
            elif name_l.startswith(q) or id_l.startswith(q): rank = 1
            elif q in name_l or q in id_l:               rank = 2
            elif q in r.get("desc", "").lower():          rank = 3
            else:                                          rank = 4  # flatpak search already filtered — keep, just ranked last
            scored.append((rank, len(app_id), app_id, r))
        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        matches = []
        for _, _, app_id, r in scored[:limit]:
            cache_key = f"flatpak:{app_id}"
            pkg = self._available_cache.get(cache_key)
            if pkg is None:
                pkg = Package(
                    name=app_id, version="", new_version=r.get("version", "?"),
                    description=r.get("desc", ""), repo="flatpak",
                    installed=False, remote=r.get("remote", "flathub"),
                    icon_name=app_id, display_name=r.get("name", ""),
                )
                self._available_cache[cache_key] = pkg
            matches.append(pkg)
        return matches

    def _find_installable_aur_matches(self, q: str, limit: int = 150) -> list:
        if not q:
            return []
        if not KNOWN_AUR_META:
            print("[aur-meta] search ran but KNOWN_AUR_META is still empty "
                  "(background fetch not done yet, or it failed — check "
                  "earlier [aur-meta] messages above)", file=sys.stderr)
            return []
        installed_names = {p.name for p in self.all_packages}
        scored = []
        for name, info in KNOWN_AUR_META.items():
            if name in installed_names:
                continue
            name_l = name.lower()
            if name_l == q:            rank = 0
            elif name_l.startswith(q): rank = 1
            elif q in name_l:          rank = 2
            elif q in info.get("desc", "").lower(): rank = 3
            else:                      continue
            scored.append((rank, len(name), name, info))
        scored.sort(key=lambda t: (t[0], t[1], t[2]))
        matches = []
        for _, _, name, info in scored[:limit]:
            cache_key = f"aur:{name}"
            pkg = self._available_cache.get(cache_key)
            if pkg is None:
                pkg = Package(
                    name=name, version="", new_version=info.get("version", "?"),
                    description=info.get("desc", ""), repo="aur",
                    license=info.get("license", ""), url=info.get("url", ""),
                    depends=info.get("depends", ""), installed=False,
                )
                self._available_cache[cache_key] = pkg
            matches.append(pkg)
        return matches

    def _popular_candidates(self) -> list:
        """Popular not-currently-installed apps."""
        installed_names = {p.name for p in self.all_packages}
        out = []
        for name in POPULAR_PACMAN_NAMES:
            if name in installed_names:
                continue
            info = self.pacman_sync_full.get(name)
            if not info:
                continue
            cache_key = f"pacman:{name}"
            pkg = self._available_cache.get(cache_key)
            if pkg is None:
                pkg = Package(
                    name=name, version="", new_version=info.get("version", "?"),
                    description=info.get("desc", ""), repo="pacman",
                    license=info.get("license", ""), url=info.get("url", ""),
                    depends=info.get("depends", ""), installed=False,
                )
                self._available_cache[cache_key] = pkg
            out.append(pkg)
        for name in POPULAR_AUR_NAMES:
            if name in installed_names:
                continue
            info = KNOWN_AUR_META.get(name)
            if not info:
                continue
            cache_key = f"aur:{name}"
            pkg = self._available_cache.get(cache_key)
            if pkg is None:
                pkg = Package(
                    name=name, version="", new_version=info.get("version", "?"),
                    description=info.get("desc", ""), repo="aur",
                    license=info.get("license", ""), url=info.get("url", ""),
                    depends=info.get("depends", ""), installed=False,
                )
                self._available_cache[cache_key] = pkg
            out.append(pkg)
        return out

    def _pkg_conflicts(self, pkg: Package) -> set:
        """Bare package names this package declares in Conflicts=."""
        info = None
        if pkg.repo == "pacman":
            info = self.pacman_sync_full.get(pkg.name)
        elif pkg.repo == "aur":
            info = KNOWN_AUR_META.get(pkg.name)
        if not info:
            return set()
        raw = info.get("conflicts", "")
        return {_VER_OP_RE.sub("", c).strip() for c in raw.split(",") if c.strip()}

    def _classify_batch_conflicts(self, sel: list):
        """Checks each package about to be installed against its declared conflicts."""
        installs = [p for p in sel if not p.installed]
        removals = {p.name for p in sel if p.marked_remove}
        installed_names = {p.name for p in self.all_packages if p.installed}
        resolved, unresolved = [], []
        for p in installs:
            for c in self._pkg_conflicts(p):
                if c == p.name:
                    continue   # a package can't conflict with itself
                if c in removals:
                    resolved.append((p, c))
                elif c in installed_names:
                    unresolved.append((p, c))
        return resolved, unresolved

    def _update_counts_label(self):
        """Rebuild the "N packages · N selected" label under the list."""
        pool    = self.filtered
        flt     = self.current_filter
        q       = self.search.get_text().lower().strip()
        n       = len(pool)
        n_upd   = sum(1 for p in pool if p.has_update and p.installed)
        checked = self._checked_count()
        parts   = [f"{n} package{'s' if n != 1 else ''}"]
        if flt != "updates" and n_upd:
            parts.append(f"{n_upd} with updates")
        if q and self.search_extra_results:
            parts.append(f"{len(self.search_extra_results)} available to install")
        elif q:
            parts.append("search results")
        elif flt == "all" and self.search_extra_results:
            parts.append(f"{len(self.search_extra_results)} popular picks")
        if checked:
            parts.append(f"{checked} selected")
        self.count_lbl.set_text(" · ".join(parts))

    def _update_footer(self):
        pkgs  = self.all_packages
        n_p   = sum(1 for p in pkgs if p.repo == "pacman")
        n_a   = sum(1 for p in pkgs if p.repo == "aur")
        n_f   = sum(1 for p in pkgs if p.repo == "flatpak")
        n_s   = sum(1 for p in pkgs if p.repo == "snap")
        n_upd = sum(1 for p in pkgs if p.has_update)
        flt   = self.current_filter
        if flt == "installed":
            self.footer.set_text(
                f"{len(pkgs)} packages total · {n_upd} update{'s' if n_upd!=1 else ''} available"
                f" · Pacman {n_p}  AUR {n_a}  Flatpak {n_f}  Snap {n_s}")
        elif flt == "all":
            n_extra = len(self.search_extra_results)
            self.footer.set_text(
                f"{len(pkgs)} installed"
                + (f" · {n_extra} popular pick{'s' if n_extra!=1 else ''} to explore" if n_extra else ""))
        elif flt == "updates":
            self.footer.set_text(
                f"{n_upd} pending update{'s' if n_upd!=1 else ''}")
        else:
            src_count = sum(1 for p in pkgs if p.repo == flt)
            src_upd   = sum(1 for p in pkgs if p.repo == flt and p.has_update)
            self.footer.set_text(
                f"{flt.title()}: {src_count} installed"
                + (f" · {src_upd} with updates" if src_upd else ""))

    # ── Loading ───────────────────────────────────────────────────────────────

    def _load_packages(self):
        self.stack.set_visible_child_name("loading")
        self.status_page.set_title("Loading packages…")
        self.status_page.set_description("Reading local package databases")
        self.all_packages = []
        self.selected_pkg = None
        threading.Thread(target=self._fetch_all, daemon=True).start()

    def _fetch_all(self):
        pkgs, sync_full, sync_ok = get_all_packages_fast()
        GLib.idle_add(self._on_loaded, pkgs, sync_full, sync_ok)

    def _on_loaded(self, pkgs: list, sync_full: dict, sync_ok: bool):
        self.all_packages     = pkgs
        self.pacman_sync_full = sync_full   # {name: info} for every repo package — powers search-for-installables
        # Cached "available to install" results are now stale
        self._available_cache = {}
        self.search_extra_results = []
        self._sync_ok     = sync_ok
        # Fix #19
        self.sync_banner.set_revealed(not sync_ok and bool(pkgs))

        if not pkgs:
            self.status_page.set_title("No packages found")
            self.status_page.set_description("Could not read the local package database.")
            return False

        self.stack.set_visible_child_name("main")
        self._populate_list()
        # Fix #1: refresh mappings in background after UI is shown
        _refresh_mappings_bg()
        _refresh_aur_meta_bg()
        return False

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_filter(self, btn, key):
        self.current_filter = key
        self.search.set_text("")   # clear search — restores category browsing
        self._hl_sidebar()
        if key != "updates":
            # Only clear install/update selections
            for p in self.all_packages:
                if not p.marked_remove: p.checked = False
            for p in self._available_cache.values():
                if not p.marked_remove: p.checked = False
            self.sel_all.set_active(False)
        self._populate_list()

    def _on_search_changed(self, entry):
        # Only show the clear icon once there's something to clear.
        has_text = bool(entry.get_text())
        entry.set_icon_from_icon_name(
            Gtk.EntryIconPosition.SECONDARY,
            "edit-clear-symbolic" if has_text else None)
        if has_text:
            entry.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Clear")

    def _on_search_icon_press(self, entry, icon_pos):
        if icon_pos == Gtk.EntryIconPosition.SECONDARY:
            entry.set_text("")
            self._do_search()

    def _do_search(self):
        q = self.search.get_text().strip()
        if q:
            # Search crosses all sources — reset sidebar highlight but not the filter
            for key, btn in self._filter_btns.items():
                lbl = self._btn_label(btn)
                if lbl:
                    lbl.remove_css_class("active-filter")
            # Highlight "all" as active during search
            all_lbl = self._btn_label(self._filter_btns["all"])
            if all_lbl:
                all_lbl.add_css_class("active-filter")
        else:
            self._hl_sidebar()
        self._populate_list()

    def _on_sort_changed(self, drop, _param):
        self.current_sort = SORT_OPTIONS[drop.get_selected()]
        self._populate_list()

    def _on_select_all(self, btn):
        for p in self.filtered:
            if p.has_update:
                p.checked = btn.get_active()
                # Select-all only lives on the Updates tab
                p.marked_remove = False
        self._populate_list()

    def _checkbox_action_visual(self, pkg: Package) -> Optional[tuple]:
        """Returns (icon_name, css_class, tooltip) describing what a checked box means."""
        if not pkg.installed:
            if pkg.checked:
                return ("list-add-symbolic", "action-install", "Will be installed")
            return None
        if self.current_filter == "updates":
            if pkg.checked and not pkg.marked_remove:
                return ("software-update-available-symbolic", "action-update", "Will be updated")
            return None
        if pkg.checked and pkg.marked_remove:
            return ("user-trash-symbolic", "action-remove", "Will be uninstalled")
        return None

    _ACTION_ICON_CSS_CLASSES = ("action-install", "action-update", "action-remove")

    def _refresh_action_icon(self, img: Gtk.Image, pkg: Package):
        for cls in self._ACTION_ICON_CSS_CLASSES:
            img.remove_css_class(cls)
        visual = self._checkbox_action_visual(pkg)
        if visual:
            icon_name, css_class, tooltip = visual
            img.set_from_icon_name(icon_name)
            img.add_css_class(css_class)
            img.set_tooltip_text(tooltip)
            img.set_visible(True)
        else:
            img.set_visible(False)

    def _on_action_icon_refresh(self, cb, pkg: Package, action_icon: Gtk.Image):
        self._refresh_action_icon(action_icon, pkg)

    def _on_pkg_check(self, cb, pkg: Package):
        active = cb.get_active()
        pkg.checked = active
        # Resolve what this checkbox means at the moment it's toggled
        pkg.marked_remove = bool(active and pkg.installed and self.current_filter != "updates")
        total = self._checked_count()
        self.apply_btn.set_sensitive(total > 0)
        self.apply_btn.set_label(self._apply_btn_label())
        self._update_counts_label()
        self._update_footer()

    def _on_row_selected(self, lb, row):
        if row is None: return
        pkg = row.pkg
        self.selected_pkg = pkg
        self.d_name.set_markup(f"<b>{GLib.markup_escape_text(pkg.display_name or pkg.name)}</b>")
        self.d_desc.set_text(pkg.description or "Loading…")
        if pkg.icon_name and self.icon_theme.has_icon(pkg.icon_name):
            self.d_icon.set_from_icon_name(pkg.icon_name)
        else:
            self.d_icon.set_from_icon_name(self._ICON_FALLBACK.get(pkg.repo, "package-x-generic-symbolic"))
        if pkg.repo in ("flatpak", "snap") and (
                not pkg.description or not pkg.url or
                (pkg.repo == "flatpak" and not pkg.display_name)):
            if pkg.cl_key not in self._enrich_inflight:
                self._enrich_inflight.add(pkg.cl_key)
                threading.Thread(target=self._enrich_bg, args=(pkg,), daemon=True).start()
        self._render_detail()

    def _enrich_bg(self, pkg: Package):
        try:
            enrich_pkg(pkg)
        finally:
            GLib.idle_add(self._enrich_done, pkg)

    def _is_selected(self, pkg: Package) -> bool:
        sp = self.selected_pkg
        return sp is not None and sp.repo == pkg.repo and sp.name == pkg.name

    def _enrich_done(self, pkg: Package):
        self._enrich_inflight.discard(pkg.cl_key)
        if self._is_selected(pkg):
            self.d_name.set_markup(
                f"<b>{GLib.markup_escape_text(pkg.display_name or pkg.name)}</b>")
            self.d_desc.set_text(pkg.description or "No description available.")
            if self.current_tab == "info":
                self._render_detail()
        return False

    def _on_tab(self, btn, key):
        self.current_tab = key
        for k, b in self._tab_btns.items():
            b.set_active(k == key)
        self._render_detail()

    # Keyboard arrow navigation
    def _on_list_key(self, controller, keyval, keycode, state):
        UP   = Gdk.KEY_Up
        DOWN = Gdk.KEY_Down
        if keyval not in (UP, DOWN):
            return False
        row = self.listbox.get_selected_row()
        if row is None:
            first = self.listbox.get_row_at_index(0)
            if first: self.listbox.select_row(first)
            return True
        idx  = row.get_index()
        next_row = self.listbox.get_row_at_index(idx + (1 if keyval == DOWN else -1))
        if next_row:
            self.listbox.select_row(next_row)
            next_row.grab_focus()
        return True

    # ── Detail panel ──────────────────────────────────────────────────────────

    def _clear(self):
        while child := self.d_box.get_first_child():
            self.d_box.remove(child)

    def _render_detail(self):
        self._clear()
        pkg = self.selected_pkg
        if not pkg: return
        if   self.current_tab == "info":      self._render_info(pkg)
        elif self.current_tab == "changelog":  self._render_changelog(pkg)
        elif self.current_tab == "files":      self._render_files(pkg)

    def _info_row(self, label: str, value: str, is_url: bool = False):
        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        l  = Gtk.Label(label=label)
        l.add_css_class("dim-label")
        l.set_size_request(100, -1); l.set_xalign(0); l.set_valign(Gtk.Align.START)
        hb.append(l)
        if is_url and value and value.startswith("http"):
            btn = Gtk.LinkButton(uri=value)
            btn.set_label(value)
            btn.set_halign(Gtk.Align.START)
            inner = btn.get_child()
            if inner:
                inner.set_ellipsize(Pango.EllipsizeMode.END)
                inner.set_max_width_chars(34)
            hb.append(btn)
        else:
            v = Gtk.Label(label=value or "—")
            v.set_xalign(0); v.set_wrap(True)
            v.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            v.set_hexpand(True); v.set_selectable(True)
            hb.append(v)
        self.d_box.append(hb)

    def _install_one(self, pkg: Package):
        pkg.checked = True
        self._confirm_and_apply([pkg])

    def _render_info(self, pkg: Package):
        self._info_row("Source", pkg.repo.upper())
        # Show the raw technical identifier
        if pkg.display_name and pkg.display_name != pkg.name:
            label = "Application ID" if pkg.repo == "flatpak" else "Package name"
            self._info_row(label, pkg.name)
        if pkg.installed:
            self._info_row("Installed", pkg.version)
            if pkg.has_update:     self._info_row("Update to",  pkg.new_version)
            if pkg.installed_size: self._info_row("On disk",    pkg.installed_size)
        else:
            self._info_row("Available", pkg.new_version)
        if pkg.license:        self._info_row("License",    pkg.license)
        if pkg.url:            self._info_row("URL",        pkg.url, is_url=True)
        if pkg.depends:        self._info_row("Depends",    pkg.depends)
        if pkg.is_dep:
            note = Gtk.Label(label="ⓘ Installed as a dependency")
            note.add_css_class("dim-label"); note.set_xalign(0); note.set_margin_top(6)
            self.d_box.append(note)
        if not pkg.installed:
            btn = Gtk.Button(label="Install")
            btn.add_css_class("suggested-action")
            btn.set_halign(Gtk.Align.START); btn.set_margin_top(10)
            btn.connect("clicked", lambda _: self._install_one(pkg))
            self.d_box.append(btn)

    def _make_cached_icon(self) -> Gtk.Image:
        icon = Gtk.Image.new_from_icon_name("document-open-recent-symbolic")
        icon.set_tooltip_text("Loaded from cache")
        icon.add_css_class("dim-label")
        return icon

    def _render_changelog(self, pkg: Package):
        if pkg.changelog is None:
            sp = Gtk.Spinner(); sp.start()
            sp.set_size_request(24, 24); sp.set_halign(Gtk.Align.CENTER)
            self.d_box.append(sp)
            lbl = Gtk.Label(label="Fetching changelog…")
            lbl.add_css_class("dim-label"); lbl.set_halign(Gtk.Align.CENTER)
            self.d_box.append(lbl)
            if pkg.cl_key not in self._cl_inflight:
                self._cl_inflight.add(pkg.cl_key)
                threading.Thread(target=self._bg_cl, args=(pkg,), daemon=True).start()
            return

        if pkg.changelog.get("error") and not pkg.changelog.get("versions"):
            err = Gtk.Label(label=pkg.changelog["error"])
            err.add_css_class("error"); err.set_wrap(True); self.d_box.append(err)
            rb = Gtk.Button(label="Retry"); rb.set_halign(Gtk.Align.CENTER)
            rb.connect("clicked", lambda _: self._force_refresh_cl(pkg))
            self.d_box.append(rb)
            self._append_debug_expander(pkg)
            return

        # If this package only has a release_pages mapping
        if pkg.changelog.get("_link_only"):
            url = pkg.changelog.get("_link_url", "")
            escaped_url = GLib.markup_escape_text(url)
            link_lbl = Gtk.Label()
            link_lbl.set_markup(f'See <a href="{escaped_url}">{escaped_url}</a> for details.')
            link_lbl.connect("activate-link", _on_activate_link)
            link_lbl.set_xalign(0)
            link_lbl.set_wrap(True); link_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            link_lbl.set_hexpand(True)
            link_lbl.set_margin_bottom(4)
            self.d_box.append(link_lbl)

            ref_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            if pkg.changelog.get("_from_cache"):
                ref_row.append(self._make_cached_icon())
            ref_btn = Gtk.Button(label="↻ Refresh")
            ref_btn.add_css_class("flat"); ref_btn.set_halign(Gtk.Align.START)
            ref_btn.connect("clicked", lambda _: self._force_refresh_cl(pkg))
            ref_row.append(ref_btn)
            self.d_box.append(ref_row)

            self._append_debug_expander(pkg)
            return

        # Source label: URL portion is a clickable link
        src_desc = pkg.changelog.get('source', '')
        src_url = _resolve_source_url(pkg.changelog)
        src = Gtk.Label()
        src.set_xalign(0)
        src.set_wrap(True); src.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        src.set_hexpand(True)
        if src_url:
            desc_text = src_desc
            if src_url in desc_text:
                desc_text = desc_text.replace(src_url, "").rstrip(" —-")
            else:
                # Strip the repo path
                path_part = re.sub(r'^https?://[^/]+/?', '', src_url)
                if path_part and path_part in desc_text:
                    desc_text = desc_text.replace(path_part, "").rstrip(" —-")
            escaped_desc = GLib.markup_escape_text(f"Source: {desc_text}".rstrip())
            escaped_url  = GLib.markup_escape_text(src_url)
            src.set_markup(f'{escaped_desc}  <a href="{escaped_url}">{escaped_url}</a>')
            src.connect("activate-link", _on_activate_link)
        else:
            src.set_text(f"Source: {src_desc}")
        src.add_css_class("dim-label"); src.set_margin_bottom(2)
        self.d_box.append(src)

        # adds a clickable link when automatic changelog detection found nothing
        manual_url = pkg.changelog.get("_manual_check_url")
        if manual_url:
            escaped_url = GLib.markup_escape_text(manual_url)
            manual_lbl = Gtk.Label()
            manual_lbl.set_markup(
                f'Please check manually at: <a href="{escaped_url}">{escaped_url}</a>')
            manual_lbl.connect("activate-link", _on_activate_link)
            manual_lbl.set_xalign(0)
            manual_lbl.set_wrap(True); manual_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            manual_lbl.set_hexpand(True)
            manual_lbl.add_css_class("dim-label"); manual_lbl.set_margin_bottom(2)
            self.d_box.append(manual_lbl)

        # Stale warning
        if pkg.changelog.get("_stale"):
            stale_lbl = Gtk.Label(label="⚠ Cached data may be outdated (>7 days)")
            stale_lbl.add_css_class("stale-warn"); stale_lbl.set_xalign(0)
            self.d_box.append(stale_lbl)

        # Newest version found didn't match the installed/pending version
        if pkg.changelog.get("_version_mismatch"):
            mismatch_lbl = Gtk.Label(
                label="⚠ This may not be the changelog for the current version")
            mismatch_lbl.add_css_class("stale-warn"); mismatch_lbl.set_xalign(0)
            self.d_box.append(mismatch_lbl)

        # Newest entry is new enough, but unconfirmed
        elif pkg.changelog.get("_version_unconfirmed"):
            unconfirmed_lbl = Gtk.Label(
                label="ℹ Exact update version not listed below — "
                      "check the source link above for details")
            unconfirmed_lbl.add_css_class("dim-label"); unconfirmed_lbl.set_xalign(0)
            unconfirmed_lbl.set_wrap(True); unconfirmed_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            unconfirmed_lbl.set_hexpand(True)
            self.d_box.append(unconfirmed_lbl)

        ref_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        if pkg.changelog.get("_from_cache"):
            ref_row.append(self._make_cached_icon())
        ref_btn = Gtk.Button(label="↻ Refresh")
        ref_btn.add_css_class("flat"); ref_btn.set_halign(Gtk.Align.START)
        ref_btn.connect("clicked", lambda _: self._force_refresh_cl(pkg))
        ref_row.append(ref_btn)
        self.d_box.append(ref_row)
        self.d_box.append(Gtk.Separator())

        for v in pkg.changelog.get("versions", []):
            if not isinstance(v, dict): continue
            vb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            vl = Gtk.Label()
            vl.set_markup(
                f"<b>{GLib.markup_escape_text(str(v.get('version', '?')))}</b>")
            vl.set_xalign(0); vb.append(vl)
            if v.get("date"):
                dl = Gtk.Label(label=str(v["date"]))
                dl.add_css_class("dim-label"); vb.append(dl)
            self.d_box.append(vb)
            for change in v.get("changes", []):
                if not isinstance(change, str): continue
                rb2 = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                bul = Gtk.Label(label="•")
                bul.add_css_class("dim-label"); bul.set_valign(Gtk.Align.START)
                rb2.append(bul)
                cl = Gtk.Label(label=change)
                cl.set_xalign(0); cl.set_wrap(True)
                cl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                cl.set_hexpand(True); cl.set_selectable(True)
                rb2.append(cl)
                self.d_box.append(rb2)
            self.d_box.append(Gtk.Separator())

        self._append_debug_expander(pkg)

    def _append_debug_expander(self, pkg: Package):
        """Show exactly which resolution steps were tried for this package."""
        trace = pkg.changelog.get("_debug") if pkg.changelog else None
        if not trace:
            return
        expander = Gtk.Expander(label="Debug: resolution steps")
        expander.set_margin_top(6)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_start(8)
        box.set_margin_top(4)
        for line in trace:
            lbl = Gtk.Label(label=line)
            lbl.add_css_class("mono")
            lbl.add_css_class("dim-label")
            lbl.set_xalign(0)
            lbl.set_wrap(True)
            lbl.set_selectable(True)
            box.append(lbl)
        copy_btn = Gtk.Button(label="Copy trace")
        copy_btn.add_css_class("flat")
        copy_btn.set_halign(Gtk.Align.START)
        copy_btn.set_margin_top(4)
        copy_btn.connect("clicked", lambda _: self._copy_debug_trace(trace))
        box.append(copy_btn)
        expander.set_child(box)
        self.d_box.append(expander)

    def _copy_debug_trace(self, trace: list[str]):
        clipboard = self.get_clipboard()
        clipboard.set(str("\n".join(trace)))
        self.footer.set_text("Debug trace copied to clipboard.")

    def _force_refresh_cl(self, pkg: Package):
        key = pkg.cl_key
        if key in _CL_DB:
            del _CL_DB[key]
            _cl_db_flush(force=True)
        pkg.changelog = None
        if self._is_selected(pkg):
            self._render_detail()

    def _render_files(self, pkg: Package):
        """Walk the real Flatpak deploy directory; run pacman -Ql off the main thread."""
        if not pkg.installed:
            note = Gtk.Label(label="Not installed — nothing to list yet.")
            note.add_css_class("dim-label"); note.set_halign(Gtk.Align.CENTER)
            note.set_margin_top(20)
            self.d_box.append(note)
            return

        if pkg.repo == "pacman":
            sp = Gtk.Spinner(); sp.start()
            sp.set_size_request(24, 24); sp.set_halign(Gtk.Align.CENTER)
            self.d_box.append(sp)
            lbl = Gtk.Label(label="Reading file list…")
            lbl.add_css_class("dim-label"); lbl.set_halign(Gtk.Align.CENTER)
            self.d_box.append(lbl)
            threading.Thread(target=self._bg_files, args=(pkg,), daemon=True).start()
            return

        if pkg.repo == "flatpak":
            found_files = False
            for base in [Path("/var/lib/flatpak/app"),
                         Path.home() / ".local/share/flatpak/app"]:
                app_dir = base / pkg.name
                if not app_dir.exists():
                    continue
                try:
                    for branch_dir in sorted(app_dir.iterdir()):
                        for arch_dir in sorted(branch_dir.iterdir()):
                            active = arch_dir / "active"
                            if active.exists():
                                for item in sorted(active.iterdir())[:40]:
                                    lbl = Gtk.Label(label=str(item))
                                    lbl.add_css_class("mono"); lbl.set_xalign(0)
                                    self.d_box.append(lbl)
                                found_files = True
                                break
                        if found_files: break
                except Exception:
                    pass
                if found_files: break
            if not found_files:
                note = Gtk.Label(label=f"/var/lib/flatpak/app/{pkg.name}/")
                note.add_css_class("mono"); note.set_xalign(0)
                self.d_box.append(note)
            return

        if pkg.repo == "snap":
            l = Gtk.Label(label=f"/snap/{pkg.name}/current/")
            l.add_css_class("mono"); l.set_xalign(0)
            self.d_box.append(l)
            return

        note = Gtk.Label(label="File list not available.")
        note.add_css_class("dim-label"); note.set_wrap(True)
        self.d_box.append(note)

    def _bg_files(self, pkg: Package):
        out, _, rc = run(["pacman", "-Ql", pkg.name])
        lines = []
        if rc == 0:
            for line in out.splitlines()[:80]:
                parts = line.split(None, 1)
                lines.append(parts[1] if len(parts) > 1 else line)
        GLib.idle_add(self._files_done, pkg, lines)

    def _files_done(self, pkg: Package, lines: list):
        if self._is_selected(pkg) and self.current_tab == "files":
            self._clear()
            if lines:
                for path in lines:
                    l = Gtk.Label(label=path)
                    l.add_css_class("mono"); l.set_xalign(0); l.set_selectable(True)
                    self.d_box.append(l)
            else:
                note = Gtk.Label(label="File list not available.")
                note.add_css_class("dim-label"); note.set_wrap(True)
                self.d_box.append(note)
        return False

    def _bg_cl(self, pkg: Package):
        try:
            pkg.changelog = fetch_changelog(pkg)
        except Exception as e:
            pkg.changelog = {"versions": [], "error": str(e), "source": "error"}
        GLib.idle_add(self._cl_done, pkg)

    def _cl_done(self, pkg: Package):
        self._cl_inflight.discard(pkg.cl_key)
        sel = self.selected_pkg
        if (sel is not None and sel is not pkg and sel.cl_key == pkg.cl_key
                and sel.changelog is None):
            sel.changelog = pkg.changelog
        if self._is_selected(pkg) and self.current_tab == "changelog":
            self._render_detail()
        return False

    # ── About & Menu ──────────────────────────────────────────────────────────

    def _on_about(self, action, param):
        """Show About dialog."""
        dlg = Adw.AboutDialog()
        dlg.set_application_name("Pakchan")
        dlg.set_version("1.0.0")
        dlg.set_comments(
            "A PAMAC-like package manager for Manjaro/Arch Linux "
            "with real changelogs for Pacman, AUR, Flatpak, and Snap.")
        dlg.set_website("https://dodog.github.io/pakchan/web/")
        dlg.set_issue_url("https://github.com/dodog/pakchan/issues")
        dlg.set_license_type(Gtk.License.MIT_X11)
        dlg.set_developers([
            "Jozef Gaal",
            "Pakchan contributors https://github.com/dodog/pakchan/graphs/contributors",
        ])
        dlg.set_copyright("© 2026 Jozef Gaal")
        dlg.add_link("Donate", "https://buymeacoffee.com/dodog")

        # Show package counts as extra info
        n_pkgs = len(self.all_packages)
        n_maps = (len(KNOWN_GITHUB_REPOS) + len(KNOWN_GITLAB_REPOS)
                  + len(KNOWN_RELEASE_PAGES) + len(KNOWN_CUSTOM))
        dlg.set_debug_info(
            f"Installed packages: {n_pkgs}\n"
            f"Changelog mappings: {n_maps}\n"
            f"Mappings source: {MAPPINGS_URL}\n"
            f"Cache dir: {CACHE_DIR}\n"
            f"Changelog DB: {CHANGELOG_DB}\n"
            f"Python: {sys.version.split()[0]}\n"
        )
        dlg.present(self)

    def _on_submit_source(self, action, param):
        """Open the pakchan web submission page."""
        try:
            Gio.AppInfo.launch_default_for_uri("https://dodog.github.io/pakchan/web/", None)
        except Exception:
            _dbg("failed to open submission page in default browser")

    # ── Apply updates ─────────────────────────────────────────────────────────

    # ── Integrated update panel ──────────────────────────────────────────────

    def _build_update_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("update-panel")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.set_margin_start(10); header.set_margin_end(10)
        header.set_margin_top(6);    header.set_margin_bottom(6)
        self.update_spinner = Gtk.Spinner()
        header.append(self.update_spinner)
        self.update_status_lbl = Gtk.Label(label="Updating…")
        self.update_status_lbl.set_xalign(0)
        self.update_status_lbl.set_hexpand(True)
        self.update_status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self.update_status_lbl)
        # Collapsed by default
        self.update_expand_btn = Gtk.Button(icon_name="pan-down-symbolic")
        self.update_expand_btn.add_css_class("flat")
        self.update_expand_btn.set_tooltip_text("Show details")
        self.update_expand_btn.connect("clicked", self._on_toggle_update_log)
        header.append(self.update_expand_btn)
        self.update_close_btn = Gtk.Button(icon_name="window-close-symbolic")
        self.update_close_btn.add_css_class("flat")
        self.update_close_btn.set_tooltip_text("Hide panel")
        self.update_close_btn.connect(
            "clicked", lambda _: self.update_revealer.set_reveal_child(False))
        header.append(self.update_close_btn)
        box.append(header)

        # Plain show/hide
        self.update_log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.update_log_box.set_visible(False)
        self.update_log_box.append(Gtk.Separator())

        # Terminal or plain-text fallback
        if _HAVE_VTE:
            self.vte_term = Vte.Terminal()
            self.vte_term.set_size_request(-1, 220)
            self.vte_term.set_hexpand(True)
            self.vte_term.connect("child-exited", self._on_update_child_exited)
            self.vte_sc = Gtk.ScrolledWindow()
            self.vte_sc.set_child(self.vte_term)
            self.update_log_box.append(self.vte_sc)
        else:
            self.vte_sc = None

        # Plain scrolling log used whenever there's no pty involved
        self.update_textview = Gtk.TextView()
        self.update_textview.set_editable(False)
        self.update_textview.set_cursor_visible(False)
        self.update_textview.set_wrap_mode(Gtk.WrapMode.CHAR)
        self.update_textview.add_css_class("update-log")
        self.textview_sc = Gtk.ScrolledWindow()
        self.textview_sc.set_child(self.update_textview)
        self.textview_sc.set_size_request(-1, 220)
        self.textview_sc.set_visible(not _HAVE_VTE)
        self.update_log_box.append(self.textview_sc)

        box.append(self.update_log_box)
        return box

    def _on_toggle_update_log(self, btn):
        expanded = self.update_log_box.get_visible()
        self.update_log_box.set_visible(not expanded)
        btn.set_icon_name("pan-up-symbolic" if not expanded else "pan-down-symbolic")
        btn.set_tooltip_text("Hide details" if not expanded else "Show details")

    def _apply_updates(self, btn):
        sel = [p for p in self._selectable_pool() if p.checked]
        if not sel: return
        self._confirm_and_apply(sel)

    def _confirm_and_apply(self, sel: list):
        # AUR installs need an AUR helper (yay/paru) to actually run
        aur_needs_helper = [p for p in sel if p.repo == "aur" and not p.installed]
        if aur_needs_helper and not (cmd_exists("yay") or cmd_exists("paru")):
            names = ", ".join(p.name for p in aur_needs_helper[:3])
            if len(aur_needs_helper) > 3:
                names += f", and {len(aur_needs_helper) - 3} more"
            dlg = Adw.AlertDialog(
                heading="AUR helper required",
                body=f"Installing {names} from the AUR needs a helper like "
                     f"yay or paru, which isn't installed. Install one first, "
                     f"then try again.",
            )
            dlg.add_response("ok", "OK")
            dlg.present(self)
            return

        # Conflict check (see _classify_batch_conflicts)
        _resolved, unresolved = self._classify_batch_conflicts(sel)
        if unresolved:
            lines = "\n".join(f"• {p.name} conflicts with {c}" for p, c in unresolved[:5])
            if len(unresolved) > 5:
                lines += f"\n…and {len(unresolved) - 5} more"
            dlg = Adw.AlertDialog(
                heading="Conflicting packages selected",
                body=(f"{lines}\n\nInstalling these while the conflicting "
                      f"package stays installed can leave pacman/the AUR "
                      f"helper stuck trying to resolve it. Either uncheck "
                      f"the install, or also check the conflicting package "
                      f"so it's removed as part of the same batch."),
            )
            dlg.add_response("ok", "OK")
            dlg.present(self)
            return

        all_installs = all(not p.installed for p in sel)
        any_installs = any(not p.installed for p in sel)
        removals = [p for p in sel if p.marked_remove]
        removal_only = bool(removals) and len(removals) == len(sel)
        if removal_only:
            names = ", ".join(p.name for p in removals[:5])
            if len(removals) > 5:
                names += f", and {len(removals) - 5} more"
            heading, verb = "Uninstall selected packages?", "Uninstall"
            body = (f"Remove {names}. Any dependencies these packages pulled "
                     f"in that nothing else needs will be removed too — "
                     f"pacman refuses on its own if something else still "
                     f"depends on one of them. You'll be asked for your "
                     f"password in the usual system prompt.")
        elif removals or any_installs:
            heading, verb = "Apply selected changes?", "Apply"
            body = (f"{verb} {len(sel)} package(s)"
                     + (f", including {len(removals)} removal(s)" if removals else "")
                     + ". You'll be asked for your password in the usual "
                       f"system prompt.")
        elif all_installs:
            heading, verb = "Install selected packages?", "Install"
            body = (f"{verb} {len(sel)} package(s). You'll be asked for your "
                     f"password in the usual system prompt.")
        else:
            heading, verb = "Update selected packages?", "Update"
            body = (f"{verb} {len(sel)} package(s). You'll be asked for your "
                     f"password in the usual system prompt.")
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("apply", verb)
        # A pure removal batch gets the destructive (red) styling instead
        appearance = (Adw.ResponseAppearance.DESTRUCTIVE if removal_only
                      else Adw.ResponseAppearance.SUGGESTED)
        dlg.set_response_appearance("apply", appearance)
        dlg.set_default_response("apply")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._on_apply_dialog_response, sel)
        dlg.present(self)

    def _on_apply_dialog_response(self, dlg, response, sel: list):
        if response == "apply":
            self._do_apply(sel)

    def _make_askpass_script(self) -> Optional[str]:
        """graphical password prompt"""
        try:
            d = tempfile.mkdtemp(prefix="pakchan-askpass-")
            gtk_fallback = Path(d) / "askpass_gtk.py"
            gtk_fallback.write_text(_ASKPASS_GTK_SCRIPT)
            gtk_fallback.chmod(0o755)
            env_lines = "\n".join(
                f"export {var}={shlex.quote(val)}"
                for var in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY",
                            "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")
                for val in [os.environ.get(var, "")] if val
            )
            script = Path(d) / "askpass"
            script.write_text(
                "#!/bin/sh\n"
                f"{env_lines}\n"
                "if command -v zenity >/dev/null 2>&1; then\n"
                "    exec zenity --password --title='Authentication required'\n"
                "elif command -v kdialog >/dev/null 2>&1; then\n"
                "    exec kdialog --password 'Enter your password:'\n"
                "else\n"
                f"    exec python3 {shlex.quote(str(gtk_fallback))}\n"
                "fi\n"
            )
            script.chmod(0o755)
            return str(script)
        except Exception:
            return None

    def _do_apply(self, sel: list):
        rejected = [p for p in sel
                    if not _is_safe_ident(p.name)
                    or (p.repo == "flatpak" and p.remote and not _is_safe_ident(p.remote))]
        if rejected:
            rej_ids = {id(p) for p in rejected}
            sel = [p for p in sel if id(p) not in rej_ids]
            msg = (f"Skipped {len(rejected)} package(s) with unsafe names: "
                   + ", ".join(repr(p.name)[:60] for p in rejected[:3]))
            print(f"[pakchan] {msg}", file=sys.stderr)
            self.footer.set_text(msg)
            if not sel:
                return
        # shlex.quote all package names
        pac = [shlex.quote(p.name) for p in sel if p.repo == "pacman" and not p.marked_remove]
        aur = [shlex.quote(p.name) for p in sel if p.repo == "aur" and not p.marked_remove]
        snp = [shlex.quote(p.name) for p in sel if p.repo == "snap" and not p.marked_remove]

        # Flatpak update vs. install use different commands
        flt_update  = [shlex.quote(p.name) for p in sel
                       if p.repo == "flatpak" and p.installed and not p.marked_remove]
        flt_new_by_remote: dict[str, list[str]] = {}
        for p in sel:
            if p.repo == "flatpak" and not p.installed:
                flt_new_by_remote.setdefault(p.remote or "flathub", []).append(
                    shlex.quote(p.name))

        # Removals does not need an AUR helper — plain pacman -R covers it
        resolved_conflicts, _unresolved = self._classify_batch_conflicts(sel)
        early_removal_names = {c for _incoming, c in resolved_conflicts}
        rm_pac_aur_early = [shlex.quote(p.name) for p in sel
                             if p.repo in ("pacman", "aur") and p.marked_remove
                             and p.name in early_removal_names]
        rm_pac_aur_late  = [shlex.quote(p.name) for p in sel
                             if p.repo in ("pacman", "aur") and p.marked_remove
                             and p.name not in early_removal_names]
        rm_flatpak = [shlex.quote(p.name) for p in sel
                      if p.repo == "flatpak" and p.marked_remove]
        rm_snap    = [shlex.quote(p.name) for p in sel
                      if p.repo == "snap" and p.marked_remove]

        # Privilege escalation differs by what's being installed
        askpass_path = self._make_askpass_script() if aur else None
        self._askpass_dir = str(Path(askpass_path).parent) if askpass_path else None
        env_prefix = (f'export SUDO_ASKPASS={shlex.quote(askpass_path)}; '
                      if askpass_path else "")
        if aur:
            # Don't let a stray git fetch hang waiting for credentials
            env_prefix += 'export GIT_TERMINAL_PROMPT=0; '
            env_prefix += 'unset GPG_TTY; '

        # A single pkexec call needs no separate SUDO_ASKPASS setup
        use_pkexec = cmd_exists("pkexec")
        single_root_cmd = "pkexec" if use_pkexec else "sudo -A"

        cmds = []
        # Conflict-resolving removals run first
        if rm_pac_aur_early:
            cmds.append(f"{single_root_cmd} pacman -Rns --noconfirm {' '.join(rm_pac_aur_early)}")
        if pac: cmds.append(f"{single_root_cmd} pacman -S --noconfirm {' '.join(pac)}")
        if aur:
            h = "yay" if cmd_exists("yay") else "paru"
            cmds.append(f"{h} -S --noconfirm {' '.join(aur)}")
        if flt_update: cmds.append(f"flatpak update -y {' '.join(flt_update)}")
        for remote, ids in flt_new_by_remote.items():
            cmds.append(f"flatpak install -y {shlex.quote(remote)} {' '.join(ids)}")
        if snp: cmds.append(f"{single_root_cmd} snap refresh {' '.join(snp)}")
        # Everything else settles after installs/updates, same as pacman's own -Syu order
        if rm_pac_aur_late: cmds.append(f"{single_root_cmd} pacman -Rns --noconfirm {' '.join(rm_pac_aur_late)}")
        if rm_flatpak: cmds.append(f"flatpak uninstall -y {' '.join(rm_flatpak)}")
        if rm_snap:    cmds.append(f"{single_root_cmd} snap remove {' '.join(rm_snap)}")
        if not cmds:
            return
        full = " && ".join(cmds)

        # Capture the real exit status so it survives the echo/Done lines
        runner = (f"printf '\\033[1m$ %s\\033[0m\\n' {shlex.quote(full)}; "
                  f'{env_prefix}{full}; status=$?; echo; '
                  f'echo "[pakchan] Done."; exit $status')

        self.apply_btn.set_sensitive(False)
        self.update_spinner.start()
        self.update_close_btn.set_sensitive(False)
        progress_verb = ("Removing" if (rm_pac_aur_early or rm_pac_aur_late or rm_flatpak or rm_snap)
                          and not (pac or aur or snp or flt_update or flt_new_by_remote)
                          else "Updating")
        self.update_status_lbl.set_text(f"{progress_verb} {len(sel)} package(s)…")
        self.footer.set_text(f"{progress_verb} {len(sel)} package(s)…")
        self.update_revealer.set_reveal_child(True)

        # AUR installs need to run with no controlling terminal at all
        use_vte = _HAVE_VTE and not aur
        self._last_run_used_vte = use_vte
        if self.vte_sc:
            self.vte_sc.set_visible(use_vte)
        self.textview_sc.set_visible(not use_vte)

        if use_vte:
            self.vte_term.reset(True, True)
            self.vte_term.spawn_async(
                Vte.PtyFlags.DEFAULT,
                str(Path.home()),
                ["/bin/bash", "-lc", runner],
                None,   # inherit the current environment
                GLib.SpawnFlags.DEFAULT,
                None, None,
                -1,
                None,
                self._on_vte_spawned,
            )
        else:
            self._run_update_pty_fallback(runner)

    def _on_vte_spawned(self, terminal, pid, error):
        if error:
            self.update_status_lbl.set_text(f"Failed to start update: {error}")
            self.update_spinner.stop()
            self.update_close_btn.set_sensitive(True)
            self.apply_btn.set_sensitive(True)
            return
        # Mirror the terminal's last line onto the always-visible status footer
        self._vte_poll_id = GLib.timeout_add(400, self._poll_vte_status)

    def _poll_vte_status(self):
        try:
            text = self.vte_term.get_text()[0]
        except Exception:
            self._vte_poll_id = None
            return False
        last = next((ln.strip() for ln in reversed(text.split("\n")) if ln.strip()), None)
        if last and "assword" not in last:
            snippet = last[:100]
            self.update_status_lbl.set_text(snippet)
            self.footer.set_text(snippet)
        return True

    def _on_update_child_exited(self, terminal, status):
        poll_id = getattr(self, "_vte_poll_id", None)
        if poll_id:
            GLib.source_remove(poll_id)
            self._vte_poll_id = None
        self._update_finished(status)

    # ── Pty-less fallback (used when Vte isn't installed, and always for AUR installs even when it is — see _do_apply's comment on why) ───────────────────────────────

    def _run_update_pty_fallback(self, runner: str):
        buf = self.update_textview.get_buffer()
        buf.set_text("")
        try:
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", runner],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True, bufsize=1,
                start_new_session=True,
            )
        except Exception as e:
            self.update_status_lbl.set_text(f"Failed to start update: {e}")
            self.update_spinner.stop()
            self.update_close_btn.set_sensitive(True)
            self.apply_btn.set_sensitive(True)
            return
        threading.Thread(target=self._read_update_output,
                          args=(proc,), daemon=True).start()

    def _read_update_output(self, proc):
        for line in proc.stdout:
            GLib.idle_add(self._append_update_output, line)
        proc.stdout.close()
        status = proc.wait()
        GLib.idle_add(self._update_finished, status)

    def _append_update_output(self, line: str):
        # Strip ANSI codes even without a pty
        line = _ANSI_ESCAPE_RE.sub("", line).rstrip("\n")
        if line.strip():
            tb = self.update_textview.get_buffer()
            tb.insert(tb.get_end_iter(), line + "\n")
            self.update_textview.scroll_mark_onscreen(tb.get_insert())

            # Surface the current line as the visible status
            if "assword" not in line:
                snippet = line.strip()[:100]
                self.update_status_lbl.set_text(snippet)
                self.footer.set_text(snippet)
        return False

    def _show_toast(self, title: str, timeout: int = 8, button_label: str = None, button_cb=None):
        toast = Adw.Toast(title=title, timeout=timeout)
        if button_label and button_cb:
            toast.set_button_label(button_label)
            toast.connect("button-clicked", button_cb)
        self.toast_overlay.add_toast(toast)

    def _last_error_snippet(self) -> str:
        """Pull the most useful line from the log for the failure toast."""
        text = ""
        if getattr(self, "_last_run_used_vte", False):
            try:
                text = self.vte_term.get_text()[0] or ""
            except Exception:
                text = ""
        else:
            buf = self.update_textview.get_buffer()
            text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
        lines = [_ANSI_ESCAPE_RE.sub("", ln).strip() for ln in text.splitlines()]
        lines = [ln for ln in lines if ln and "assword" not in ln]
        for ln in reversed(lines):
            if "error:" in ln.lower() or ln.lower().startswith("==> error"):
                return ln
        for ln in reversed(lines):
            if ln != "[pakchan] Done.":
                return ln
        return ""

    def _reveal_update_log(self, *_a):
        self.update_revealer.set_reveal_child(True)
        self.update_log_box.set_visible(True)
        self.update_expand_btn.set_icon_name("pan-up-symbolic")
        self.update_expand_btn.set_tooltip_text("Hide details")

    def _update_finished(self, status: int):
        self.update_spinner.stop()
        self.update_close_btn.set_sensitive(True)
        self.apply_btn.set_sensitive(True)
        askpass_dir = getattr(self, "_askpass_dir", None)
        if askpass_dir:
            shutil.rmtree(askpass_dir, ignore_errors=True)
            self._askpass_dir = None
        ok = (status == 0)
        msg = ("Update finished — refreshing package list…" if ok else
               f"Update process exited with an error (code {status}) — "
               f"refreshing package list anyway…")
        self.update_status_lbl.set_text(msg)
        self.footer.set_text(msg)
        if not ok:
            # A toast survives the status label getting overwritten
            detail = self._last_error_snippet()
            toast_msg = f"Action failed: {detail}" if detail else f"Action failed (exit code {status})"
            self._show_toast(toast_msg, timeout=0,
                              button_label="View log", button_cb=self._reveal_update_log)
        # Re-read real package state instead of trusting stale UI flags
        self._load_packages()
        return False


# ─── Entry point ──────────────────────────────────────────────────────────────

def _on_exit():
    """Fix #2: Flush changelog DB on clean exit."""
    _cl_db_flush(force=True)


if __name__ == "__main__":
    import atexit
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _load_mappings_from_cache()   # disk-only at startup, instant
    _load_aur_meta_from_cache()   # Same pattern, for AUR search-to-install
    _cl_db_load()
    atexit.register(_on_exit)     # always flush on exit
    app = PakchanApp()
    sys.exit(app.run(sys.argv))
