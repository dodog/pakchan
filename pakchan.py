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

# Vte gives us a real embedded terminal (proper pty, so `sudo`/makepkg
# password & y/n prompts work exactly like in a normal terminal window).
# It's optional: distros package it under different GI versions depending
# on GTK4 support, and some systems won't have it at all — in that case
# we fall back to a plain pty-backed text panel (see _run_update_subprocess).
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

# Disable WebKit process sandbox when user namespaces are unavailable
# (avoids "CanCreateUserNamespace() clone() failure: EPERM" on some systems)
import gzip, html, json, os, re, shlex, shutil, sys, tarfile, tempfile, threading, time

# Used only by the no-Vte update fallback: strips ANSI/terminal control
# sequences (cursor show/hide, colors, etc.) from raw pty output before
# it's shown in a plain GtkTextView, which — unlike a real terminal —
# doesn't interpret them and would otherwise display them as literal
# text (e.g. a stray "[?25h").
_ANSI_ESCAPE_RE = re.compile(
    r'\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])')

os.environ.setdefault("WEBKIT_DISABLE_SANDBOX", "1")
# Force GTK's software (Cairo) renderer instead of its default GL/Vulkan
# path. On systems without a working Vulkan driver, GTK4 can fall back to
# Zink (an OpenGL-over-Vulkan translation layer) and fail noisily —
# "libEGL warning: ... MESA-LOADER ...", "ZINK: vkCreateInstance failed" —
# even though the app still renders fine via software fallback. Forcing
# Cairo up front avoids that driver probing (and its warnings) entirely;
# for a widget-heavy app like this one there's no real performance cost.
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
    is_dep:         bool = False
    has_desktop_entry: bool = False   # True if a .desktop launcher exists
    icon_name:      str = ""          # icon theme name to look up for this package's row
    size_bytes:     int = 0           # raw installed size, used for sorting (installed_size is display-only)
    changelog:      Optional[dict] = None

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


def run_git(cmd: list, timeout: int = 10) -> tuple:
    """Run git command with shorter timeout (git can hang on blocked repos).
    Default timeout is 10s vs 30s for general commands.
    """
    return run(cmd, timeout=timeout)


# ─── Debug tracing ────────────────────────────────────────────────────────────
#
# A lightweight, always-on trace of every step the changelog resolver tries
# for the currently-viewed package, so problems can be diagnosed directly
# in the UI instead of guessing from the final result alone.

_debug_trace: list[str] = []

def _dbg(msg: str):
    _debug_trace.append(msg)

def _dbg_reset():
    _debug_trace.clear()

def _dbg_get() -> list[str]:
    return list(_debug_trace)


def http_get(url: str, timeout: int = 14) -> Optional[str]:
    """
    Many release-note sites (gimp.org, filezilla-project.org, etc.) reject
    or redirect requests carrying an obviously non-browser User-Agent.
    Sending realistic browser headers significantly improves success rate.
    This is for fetching HTML PAGES — for JSON APIs, use http_get_json()
    below, which sends a proper Accept: application/json header instead.
    """
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
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 406:
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.9",
                })
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    return r.read().decode("utf-8", errors="replace")
            except Exception:
                return None
        return None
    except Exception:
        return None


def http_get_json(url: str, timeout: int = 14):
    """
    Fetch and parse a JSON API endpoint. Uses its own request (rather than
    delegating to http_get) because API endpoints — especially GitLab's
    /api/v4/ routes behind bot-protection layers like Anubis — can return
    406 Not Acceptable when sent an HTML-oriented Accept header. Sending
    Accept: application/json first, with a normal browser User-Agent,
    avoids both failure modes at once.
    """
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "Accept-Language": "en-US,en;q=0.9",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
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
                    body = r.read().decode("utf-8", errors="replace")
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
    """Detect common bot-protection services returning challenge pages.
    Returns True if the page looks like Anubis, Cloudflare, or similar.
    """
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


def _read_sync_db_names() -> tuple[set, bool]:
    """
    Fix #4: Read names directly from /var/lib/pacman/sync/*.db (zlib tar files).
    Returns (set_of_names, ok_flag).  Falls back to `pacman -Slq` if needed.
    """
    names: set[str] = set()
    db_files = list(PACMAN_SYNC.glob("*.db")) if PACMAN_SYNC.exists() else []

    if db_files:
        for db_path in db_files:
            try:
                with tarfile.open(db_path, "r:gz") as tf:
                    for member in tf.getmembers():
                        # Each entry is "name-version/desc" or "name-version/"
                        parts = member.name.split("/")
                        if parts:
                            # Strip version suffix: last hyphen-separated segment
                            pkg_ver = parts[0]
                            # name is everything before the last two hyphen groups
                            segments = pkg_ver.rsplit("-", 2)
                            if len(segments) >= 3:
                                names.add(segments[0])
                            elif len(segments) == 2:
                                names.add(segments[0])
                            else:
                                names.add(pkg_ver)
            except Exception:
                pass
        if names:
            return names, True

    # Fallback: subprocess
    out, _, rc = run(["pacman", "-Slq"], timeout=12)
    if rc == 0 and out:
        return set(out.splitlines()), True
    return set(), False


# ─── Update detection ─────────────────────────────────────────────────────────

def _pending_pacman_updates_from_sync(local_db: dict) -> dict:
    """
    Fallback update detection that doesn't depend on the pacman-contrib
    `checkupdates` tool. `checkupdates` does its own background sync to a
    temp copy of the databases, which needs network access and a writable
    temp dir — if that's missing, blocked, or pacman-contrib simply isn't
    installed, it fails (or isn't found) and the caller previously just
    got an empty dict back with no way to tell "no updates" apart from
    "couldn't check". This reads versions directly out of the databases
    already synced to /var/lib/pacman/sync/*.db (the same files
    _read_sync_db_names uses for package names) — no network needed —
    and compares against the installed version with pacman's own
    `vercmp` for a canonically correct result (handles epoch/pkgrel
    exactly the way pacman itself does).
    """
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
        # Identical strings can never be an update — skip without paying
        # for a vercmp call; only genuinely differing versions need the
        # real comparison (epoch/pkgrel-aware, not a plain string compare).
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
    # checkupdates found nothing — but that's indistinguishable here
    # from checkupdates being missing entirely, or its background sync
    # having failed silently. Don't treat that as "definitely no
    # updates"; cross-check directly against the already-synced local
    # databases instead, which needs no extra tool and no new network
    # activity of its own.
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
    """
    Bulk-fetch installed Flatpak versions via a single `flatpak list`
    call. Previously versions were read from each app's per-app
    `metadata` file, but that file's format has no version= key at all
    — it always fell through to a generic "installed" placeholder for
    every Flatpak app. Only non-empty versions are recorded here: many
    Flatpak apps don't declare an explicit version and just version by
    branch (e.g. "stable"), which _flatpak_installed_version falls back
    to when nothing is found here.
    """
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

def _parse_desktop_icon(desktop_path: Path) -> Optional[str]:
    """Read the Icon= value from a .desktop file's [Desktop Entry] section."""
    try:
        text = desktop_path.read_text(errors="replace")
    except Exception:
        return None
    in_section = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_section = (line == "[Desktop Entry]")
            continue
        if in_section and line.startswith("Icon="):
            val = line.split("=", 1)[1].strip()
            return val or None
    return None


def _desktop_entries_info() -> dict[str, str]:
    """
    For each installed package, determine whether it owns a .desktop
    launcher file — the same signal PAMAC uses to separate "applications
    you'd actually launch" from libraries/CLI tools/background services —
    and, if so, read its declared icon name so the package list can show
    a real app icon (again, like PAMAC) instead of a generic placeholder.

    Reads /var/lib/pacman/local/<pkg-ver>/files directly (already on disk,
    no subprocess) to find the .desktop file's path, then reads that file
    itself for its Icon= key.

    Returns {pkg_name: icon_name}. icon_name is "" when a desktop file
    exists but has no (or an unparseable) Icon= key — the key's mere
    presence in the dict is what signals "this package has a launcher",
    distinct from a package that owns no .desktop file at all.
    """
    result: dict[str, str] = {}
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
        name = segments[0] if len(segments) >= 2 else pkg_ver
        icon_name = ""
        for rel in desktop_rel_paths:
            parsed = _parse_desktop_icon(Path("/" + rel.lstrip("/")))
            if parsed:
                icon_name = parsed
                break
        result[name] = icon_name
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
        desktop_icons   = f_gui.result()

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
            has_desktop_entry=(name in desktop_icons),
            icon_name=desktop_icons.get(name, ""),
        ))
    return pkgs, pacman_pending, aur_pending


def _load_flatpak() -> list:
    if not cmd_exists("flatpak"):
        return []
    fp_pending  = _pending_flatpak_updates()
    fp_versions = _installed_flatpak_versions()
    flatpak_dirs = [d for d in [
        Path("/var/lib/flatpak/app"),
        Path.home() / ".local/share/flatpak/app",
    ] if d.exists()]
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
            pkgs.append(Package(
                name=app_id, version=ver,
                new_version=fp_pending.get(app_id, ""),
                description="", repo="flatpak",
                has_desktop_entry=True,   # Flatpak apps always ship a .desktop file
                icon_name=app_id,         # Flatpak exports its icon under the app ID
            ))
    return pkgs


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
    """
    Prefer the version from `flatpak list` (see _installed_flatpak_versions
    — bulk-fetched once, not per-app). Many Flatpak apps don't declare an
    explicit version at all and just version by branch (e.g. "stable",
    "23.08"), in which case fall back to the installed branch name so
    there's still something meaningful shown instead of a generic
    "installed" placeholder.
    """
    if cli_version:
        return cli_version
    try:
        for branch_dir in app_dir.iterdir():
            return branch_dir.name
    except Exception:
        pass
    return "installed"


def get_all_packages_fast() -> tuple[list, bool]:
    """
    Fix #5: Parallel loading. Returns (packages, sync_names_ok).
    Pacman/AUR local DB read is instant; update checks run in parallel with
    Flatpak/Snap enumeration.
    """
    local_db            = _read_local_db()
    sync_names, sync_ok = _read_sync_db_names()
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
    return all_pkgs, sync_ok


# ─── On-demand enrichment ─────────────────────────────────────────────────────

def _flatpak_appstream_component(app_id: str) -> dict:
    """
    Fix #13: Re-enabled AppStream XML with correct per-component extraction.
    Searches local appstream cache dirs for the component block.
    """
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
            if result:
                return result
    return {}


def _local_appstream_releases(pkg_name: str) -> Optional[dict]:
    """
    Desktop apps installed via pacman/AUR usually ship an AppStream
    metainfo/appdata XML in /usr/share/metainfo/ or /usr/share/appdata/
    containing a <releases> block — the same structured release data
    Flatpak/Flathub uses, but already on disk.

    Matching is done against the reverse-DNS AppStream ID's individual
    dot-separated components (e.g. "krita" matches org.kde.krita.appdata.xml
    via its "krita" component), NOT a raw substring search — a substring
    check would (and did) match unrelated files like
    io.github.realmazharhussain.GdmSettings.metainfo.xml for the package
    "gdm", because "gdm" is a substring of "GdmSettings".
    """
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
                # AppStream IDs are dot-separated, e.g.
                # "io.github.realmazharhussain.GdmSettings.metainfo" or
                # "org.kde.krita.appdata" — split on dots and require an
                # EXACT (case-insensitive) match against one component,
                # not a substring match against the whole filename.
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

        # Match each <release ...> tag regardless of attribute order or
        # whether it's self-closing — extract attrs and body separately.
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
        # 1. Local AppStream XML (fix #13)
        if not pkg.description or not pkg.url:
            info = _flatpak_appstream_component(pkg.name)
            if info.get("description") and not pkg.description:
                pkg.description = info["description"]
            if info.get("url") and not pkg.url:
                pkg.url = info["url"]

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

KNOWN_GITHUB_REPOS:  dict[str, str]             = {}
KNOWN_GITLAB_REPOS:  dict[str, tuple[str, str]] = {}
KNOWN_RELEASE_PAGES: dict[str, str]             = {}
DEFAULT_CUSTOM: dict[str, dict] = {
    "firefox": {"parser": "mozilla", "url": "https://www.mozilla.org/en-US/firefox/releases/"},
    "thunderbird": {"parser": "mozilla", "url": "https://www.thunderbird.net/en-US/thunderbird/releases/"},
    # Note: Krita previously had a dedicated custom parser here, but its
    # output was unreliable in practice and has been removed — it now
    # falls through to the generic release-page scraper (or a plain link)
    # via the "krita" entry in mappings.json's "release_pages" section.
    "scribus": {
        "parser": "mantisbt",
        "url": "https://bugs.scribus.net/changelog_page.php",
    },
    # Use the Atom newsfeed which contains release announcements and summaries
    "filezilla": {"parser": "filezilla", "url": "https://filezilla-project.org/newsfeed.php"},
}
KNOWN_CUSTOM:        dict[str, dict]            = DEFAULT_CUSTOM.copy()   # custom parsers (merged with mappings)

# Known GitLab-like hosts that do not literally contain "gitlab" in the hostname
# (invent.kde.org, source.kde.org, etc). Extend this list if you find more.
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
    # Merge any remotely-provided custom mappings with local defaults.
    # Do not discard default metadata such as host/repo when the remote
    # mapping only provides a parser or URL override.
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
    with ThreadPoolExecutor(max_workers=min(len(urls), 6)) as ex:
        futs = {ex.submit(http_get, u, timeout): u for u in urls}
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

    url    = entry.get("url", "")
    if not url:
        return None
    body = http_get(url, timeout=16)
    if not body:
        return None
    if parser == "mantisbt":
        result = _scrape_mantisbt(body, url)
        if result and result.get("versions"):
            return result
        host = entry.get("host", "")
        repo = entry.get("repo", "")
        if host and repo:
            gitlab_result = _gitlab_releases(host, repo, pkg_name, target_version)
            if gitlab_result and gitlab_result.get("versions"):
                return gitlab_result
        # Detect if the page is a bot-protection challenge (Anubis, Cloudflare, etc)
        # and try git fallback instead of giving up
        if _is_bot_protection_page(body):
            _dbg(f"[mantisbt] bot-protection page detected, trying git fallback")
            host = entry.get("host", "")
            repo = entry.get("repo", "")
            if host and repo:
                gitlab_result = _gitlab_releases(host, repo, pkg_name, target_version)
                if gitlab_result and gitlab_result.get("versions"):
                    return gitlab_result
        if url:
            return {
                "versions": [{"version": pkg_name, "date": "",
                              "changes": [f"See {url} for details."]}],
                "source": f"Custom (mantisbt) — {url}",
                "_link_only": True,
                "_link_url": url,
            }
        return None
    if parser == "text_file":
        # Some "text_file" mappings actually point at markdown-formatted
        # NEWS/RELEASE-NOTES files — e.g. Electrum's RELEASE-NOTES uses
        # "# Release X.Y.Z (date)" headings, the same single-# pattern
        # PipeWire's NEWS file uses. Detect that regardless of what the
        # mapping declares and use the markdown-aware parser when it
        # applies, since it handles headings (and version extraction)
        # far more reliably than the plain-text heuristic parser.
        if _looks_like_markdown_changelog(body):
            return _scrape_github_raw_changelog(body)
        return _scrape_text_file(body)
    if parser == "github_raw":
        return _scrape_github_raw_changelog(body)
    if parser == "mozilla":
        return _scrape_mozilla(url, body)
    if parser == "filezilla":
        return _scrape_filezilla_changelog(body, url)
    # Unknown parser type — nothing we know how to parse; caller falls
    # back to showing a direct link to `url`.
    return None


def _scrape_mantisbt(body: str, url: str) -> Optional[dict]:
    """
    Parse MantisBT changelog pages like xnview.com/mantisbt/changelog_page.php

    Real structure (confirmed against the live page): each release is a
    link whose href contains a 'version_id=' query parameter and whose
    LINK TEXT is the version number itself, e.g.:
        <a href="changelog_page.php?version_id=123">2.45</a>
    followed by a list of issue entries (bug/feature summaries) until the
    next such link. There is no dedicated "version heading" tag/class —
    earlier attempts assuming a <td class="version"> or <h2> structure
    were matching unrelated numbers (issue IDs, dates) instead.
    """
    # Find every (version_text, start_offset, end_offset) for version_id links
    anchors = []
    for m in re.finditer(
            r'<a[^>]+href="[^"]*version_id=\d+[^"]*"[^>]*>\s*'
            r'([\d]+\.[\d.]+(?:\s*\([^)]*\))?)\s*</a>',
            body, re.IGNORECASE):
        ver = re.sub(r'\s*\([^)]*\)\s*$', '', m.group(1)).strip()  # drop "(Not yet released)" etc.
        anchors.append((ver, m.start(), m.end()))

    if not anchors:
        return None

    # Deduplicate consecutive identical versions (MantisBT sometimes lists
    # the same version twice — once as a TOC entry, once as a section start)
    deduped = []
    for ver, start, end in anchors:
        if deduped and deduped[-1][0] == ver:
            continue
        deduped.append((ver, start, end))

    versions = []
    for i, (ver, start, end) in enumerate(deduped[:8]):
        next_start = deduped[i + 1][1] if i + 1 < len(deduped) else len(body)
        segment = body[end:next_start]

        # Issue entries are typically list items or table rows containing
        # an issue ID like "0003291:" followed by a one-line summary.
        items = re.findall(r'<li[^>]*>(.*?)</li>', segment, re.DOTALL)
        if not items:
            items = re.findall(r'<td[^>]*>(.*?)</td>', segment, re.DOTALL)

        changes = []
        for item in items:
            text = _strip_html(item).strip()
            # Strip a leading "0003291: [Bug] " style prefix down to the
            # readable description, but keep the [Bug]/[New] tag — it's
            # useful context (bugfix vs new feature).
            text = re.sub(r'^\d{5,}:\s*', '', text)
            if 5 < len(text) < 300:
                changes.append(text)

        versions.append({
            "version": ver,
            "date": "",
            "changes": changes[:10] or [f"Release {ver}"],
        })

    return {"versions": versions, "source": f"MantisBT — {url}"} if versions else None


def _scrape_text_file(body: str) -> Optional[dict]:
    """
    Parse a plain-text changelog/release-notes file (no HTML at all).
    Handles formats like:
      eID klient 5.31 (2024-11-20)     ← app-name prefixed, English
      eID klient verzia 5.31           ← Slovak "verzia" = "version"
      Version 5.31 / v5.31 / [5.31] / 5.31 - 2024-11-20
    This is kept as a dedicated parser (rather than folded into the
    universal HTML scraper) because plain text has no tags at all —
    a fundamentally different format, not just a different site layout.
    """
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
    """
    True if the file uses Markdown-style headings (single "#", "##", or
    "###") to separate release entries, rather than a purely plain-text
    NEWS format. Previously this only checked for "##" specifically,
    which missed files (like PipeWire's NEWS) that use a single "#" per
    release — those got routed to the much less capable plain-text
    parser instead of the markdown-aware one.
    """
    return bool(re.search(r'(?m)^#{1,3}[ \t]', body[:2000]))


def _scrape_github_raw_changelog(body: str) -> Optional[dict]:
    """Parse a raw CHANGELOG/RELEASE-NOTES/NEWS file (Markdown headings)
    into per-version entries.

    Handles headings like:
        ## [1.2.3] - 2024-01-01
        ## 1.2.3
        # v1.2.3
        # PipeWire 1.6.0 (2026-02-19)      <- project-name prefix

    The project-name-prefix form was previously unsupported (the old
    regex required a version number immediately after the "#" marker),
    which meant files using it — like PipeWire's NEWS — fell straight
    through to the much less reliable plain-text parser and could miss
    the true latest entry entirely, letting some unrelated fragment
    further down get misparsed as the "newest" version instead.
    """
    versions = []
    heading_re = re.compile(
        r'^(#{1,3}[ \t]+[^\n]*)\n'      # group 1: the whole heading line (single line only)
        r'(.*?)'                        # group 2: body until next heading/EOF
        r'(?=^#{1,3}[ \t]+|\Z)',
        re.DOTALL | re.MULTILINE)
    # Version number must appear within the first few words of the
    # heading (at most 3 leading project-name-like words) — this avoids
    # false positives on unrelated headings that merely mention a number
    # somewhere in a sentence, e.g. "## Requirements: GTK 4.0 or later".
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

def _scrape_mozilla(url: str, body: str) -> Optional[dict]:
    """
    Priority:
    1. product-details.mozilla.org JSON API (structured, most reliable)
    2. Scrape releases index for version links → fetch each notes page in parallel
    """
    # Thunderbird check must come first — thunderbird.net URLs also contain no "firefox"
    is_thunderbird = "thunderbird" in url
    prod  = "thunderbird" if is_thunderbird else "firefox"
    base  = "https://www.thunderbird.net" if is_thunderbird else "https://www.mozilla.org"

    # 1. Try product-details JSON
    pd = http_get_json(f"https://product-details.mozilla.org/1.0/{prod}.json")
    if pd and isinstance(pd, dict):
        releases = pd.get("releases", {})
        items = sorted(
            [(k, v) for k, v in releases.items()
             if isinstance(v, dict) and v.get("date")
             and v.get("category") in ("major", "stability", "esr")],
            key=lambda x: x[1].get("date", ""),
            reverse=True
        )[:5]
        if items:
            note_urls = [f"{base}/en-US/{prod}/{v.get('version', k)}/releasenotes/"
                         for k, v in items]
            pages     = _fetch_parallel(note_urls, timeout=12)
            versions  = []
            for (k, info), note_url in zip(items, note_urls):
                ver     = str(info.get("version", k))
                date    = str(info.get("date", ""))[:10]
                notes   = pages.get(note_url) or ""
                changes = _parse_mozilla_notes(notes)
                versions.append({"version": ver, "date": date,
                                  "changes": changes[:10] or [f"Release {ver}"]})
            if versions:
                return {"versions": versions,
                        "source": "Mozilla product-details + release notes"}

    # 2. Scrape the releases index page body
    clean     = _strip_noise_blocks(body)
    ver_links = list(dict.fromkeys(re.findall(
        rf'/{prod}/([\d]+\.[\d.]+(?:esr)?)/releasenotes/', clean)))[:5]

    if not ver_links:
        ver_links = list(dict.fromkeys(re.findall(
            r'>([\d]+\.[\d]+(?:\.[\d]+)?(?:esr)?)<', clean)))[:5]

    if not ver_links:
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
    """
    Extract actual change entries from a Mozilla/Thunderbird release notes page.
    The page has sections like 'New', 'Fixed', 'Changed', 'Security fixes'.
    We must skip navigation, CSS, JavaScript, and header/footer noise.
    """
    if not html_text:
        return []

    # Step 1: Remove obvious noise blocks before any parsing
    # Strip <head>, <nav>, <header>, <footer>, <script>, <style>
    clean = html_text
    for tag in ("head", "nav", "header", "footer", "script", "style"):
        clean = re.sub(rf'<{tag}[^>]*>.*?</{tag}>', '', clean,
                       flags=re.DOTALL | re.IGNORECASE)

    # Step 2: Try to find the main content area
    # Mozilla notes pages have <main> or <div class="*notes*"> or <article>
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
    # Modern Mozilla pages: <section> or <div> with class containing new/fixed/changed/security
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
    """Parse FileZilla's changelog.php page into versions.

    This parser looks for headings containing version-like strings and
    collects nearby list items or paragraphs as change entries.
    """
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


# ─── Generic release-notes scraper (for arbitrary "release_pages" sites) ────
#
# Sites in mappings.json's "release_pages" section (Blender, GIMP, KeePass,
# nano, Samba, VLC, VirtualBox, etc.) have no shared structure, so a
# dedicated per-site parser for each one doesn't scale. Instead, this looks
# for the *pattern* nearly all of them share: a repeating heading (h1-h4) or
# table row containing a version number, followed by descriptive text or
# list items until the next one.
#
# Because this is inherently heuristic, it only returns a result when it
# finds at least MIN_ENTRIES plausible, distinct version sections with real
# content. Otherwise it returns None, and the caller falls back to the
# simple "see <url> for details" link — which is always correct, even when
# this scraper isn't confident enough to trust its own output.

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
    """Pull descriptive lines out of the HTML between one version heading
    (or table row) and the next — list items if present, else paragraphs."""
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
    """
    Strategy: some changelog pages mark each release with bold text or a
    <dt> term rather than a real <hN> heading — e.g. MediaWiki-rendered
    wikis like VirtualBox's Changelog page, which uses
    "<b>VirtualBox 7.2.14</b> (released ...)" followed by a <ul> of fixes,
    not an actual heading tag. Tried only as a fallback when heading-based
    detection finds nothing meaningful, since <b>/<strong> are common for
    plain emphasis too and are a noisier signal than real headings — the
    block window is capped so a false match can't swallow huge amounts of
    unrelated page content.
    """
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
    """Strategy 2: a simple table of releases (version/date/notes columns)
    — common on "list of all versions" pages that aren't really a
    changelog, just an index (e.g. Samba's history page)."""
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
    """
    Strategy 3 input: index/list pages where each version is just a link
    with no inline content of its own — e.g. GIMP's release-notes page,
    which lists "3.2", "3.0", "2.10", ... as links to per-version
    subpages rather than showing any changelog text directly. Returns
    (version, href) pairs, in document order, so the caller can follow
    the newest one.
    """
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
    """
    Best-effort, site-agnostic scraper for arbitrary "release notes" pages.
    Tries, in order: heading-based sections, bold/dt-based sections
    (MediaWiki-style pages), a table-row layout, and finally — if the page
    turns out to be a bare index of version links with no inline content —
    following the newest-looking link one hop deep and scraping that.
    Returns None (triggering the plain-link fallback) unless it ends up
    with at least one genuinely descriptive version entry; a low-
    confidence or wrong result is worse than an honest link, so the bar
    is "found real content", not "found several entries" — a page whose
    only release notes are for the single latest version (e.g. VS Code's
    updates page, which redirects straight to the current release) is a
    perfectly valid, if minimal, result.
    """
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

    # Maybe this is just an index of links to per-version pages
    # (e.g. GIMP's release-notes page) rather than a changelog itself.
    index_links = _scrape_index_links_for_versions(clean)
    if len(index_links) < _GENERIC_MIN_ENTRIES:
        return None

    # Try two candidates for "the newest": first in document order (most
    # index/news pages list newest-first — a more robust signal than
    # parsed-version sorting, which noisy extraction can throw off) and
    # the highest by parsed version, if that's a different link.
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
            # The whole subpage IS the notes for this one version — use
            # its list items/paragraphs directly rather than requiring a
            # version-labelled heading.
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
    """
    Sanity check before trusting a repo discovered by scanning a homepage
    for GitHub/GitLab links: the repo's own name (last path segment) must
    actually relate to the package name. Without this, scanning a generic
    wiki/project page (e.g. GDM's homepage, which links to the unrelated
    third-party "gdm-settings" tool) can silently attach the wrong
    project's changelog to a completely different package.
    """
    repo_name = repo_path.rstrip("/").split("/")[-1].lower()
    pkg_lower = pkg_name.lower()
    # Normalise common separators so "gnome-shell" ~ "gnomeshell" etc. match
    norm_repo = re.sub(r'[-_.]', '', repo_name)
    norm_pkg  = re.sub(r'[-_.]', '', pkg_lower)
    if norm_pkg == norm_repo:
        return True
    # Allow the package name to be a prefix/suffix of the repo (e.g. pkg
    # "gtk4" vs repo "gtk"), but require at least 4 shared characters to
    # avoid trivial false positives on very short names.
    if len(norm_pkg) >= 4 and (norm_repo.startswith(norm_pkg) or norm_pkg.startswith(norm_repo)):
        return True
    return False

def _find_repo_link_in_page(url: str) -> Optional[tuple]:
    """
    Scan a homepage for the project's own source-code repository link.
    Returns ("github", "owner/repo") or ("gitlab", "host", "owner[/subgroup]/repo"),
    or None. Logs every candidate via _dbg for debugging.
    Uses shorter timeout (8s) to avoid blocking on slow/redirecting homepages.
    """
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
    """
    Resolve a package's source repo by following its homepage URL.
    Returns ("github", "owner/repo") or ("gitlab", "host", "owner/repo").
    Handles: direct GitHub/GitLab URLs, github.io pages and other generic
    homepages that link to the real repo, and SourceForge project pages.
    """
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
    """
    Detect if changelog content is actually meaningful or just boilerplate.
    Returns False if changes are only links, generic text, or generic "see releases" messages.
    """
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
    """
    Normalise a tag name into a readable, comparison-friendly version
    string. Handles:
    - Simple semver: "v3.2.1" -> "3.2.1"
    - GNOME-style: "GNOME_COLOR_MANAGER_3_11_90" -> "3.11.90"
    - Release prefixes: "release-2.5" -> "2.5"
    - Project/component-name-prefixed tags some repos use as their own
      convention: "cardpeak-0.8.4" -> "0.8.4"

    Without this last case, a tag like "cardpeak-0.8.4" or
    "release-5.6.0" was stored verbatim as the "version" — which still
    sorted/selected correctly as the newest tag, but never matched the
    installed/pending version during the exact-match confirmation check
    (_versions_contain_target), since "cardpeak-0.8.4" and "0.8.4" don't
    compare as the same version even though they clearly are. Stripping
    happens iteratively (release- prefix, then a generic word- prefix)
    and stops as soon as the remainder looks like a clean version on its
    own — or after a few attempts, so a genuinely messy legacy tag (e.g.
    an old RPM-packaging-style tag with no clean version hiding inside
    it) isn't mangled further than it already is.
    """
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
    """
    Parse a version-ish string into a tuple that sorts correctly in
    semantic-version order, e.g. "1.10.0" > "1.6.8" > "1.0" > "0.3.27".

    This exists because GitLab's own tag/release ordering can't be
    trusted at face value — mirrored repos can have all their tags
    bulk-imported with the same "updated" timestamp, so the API's
    default sort becomes effectively arbitrary. Every GitLab candidate
    list is re-sorted with this key rather than trusting API order.

    NOTE: used directly by _target_version_satisfied, which relies on
    this returning a flat tuple whose *length* reflects how many
    dot-separated segments the version has (it truncates both sides to
    the shorter length before comparing, to tolerate packaging-added
    suffixes like pacman's pkgver "3.0.23_2" vs upstream's "3.0.23").
    Don't change this return shape — see _tag_selection_key below for a
    separate, differently-purposed comparison.
    """
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
    """
    True if a tag looks like a straightforward version number (optional
    v-prefix, dot-separated digits, optionally one pre-release-style or
    numeric-build suffix) rather than a differently-scoped or legacy tag
    a repo may carry alongside its real releases — e.g. spice-space's
    GitLab repo has ancient RPM-packaging-style tags like
    "spice-server-0.4.2-10.el6" mixed in with its real "0.16.0"-style
    releases.
    """
    return bool(_CLEAN_VERSION_RE.match((tag or "").strip()))


def _tag_selection_key(ver: str) -> tuple:
    """
    Sort key for choosing the best (most likely genuinely newest) tag
    among several candidates FROM THE SAME SOURCE — e.g. picking the
    newest tag out of a repo's own tag list. NOT for comparing against
    an external target version; use _version_sort_key /
    _target_version_satisfied for that instead.

    Prioritises clean-looking version tags over legacy/differently-
    scoped ones. Without this, a tag like "spice-server-0.4.2-10.el6"
    would outrank the real "0.16.0" release under plain numeric-
    component comparison: _version_sort_key deliberately ranks any tag
    with alphabetic segments above any purely-numeric one (so
    pre-release suffixes like "-rc1" compare sensibly against "1.0.0"),
    but that same rule means a tag from a completely different, older
    naming scheme can win purely by containing letters — regardless of
    its actual embedded numbers.
    """
    return (_looks_like_clean_version(ver), _version_sort_key(ver))


def _strip_pacman_epoch_pkgrel(v: str) -> str:
    """
    Pacman version strings are formatted [epoch:]pkgver-pkgrel, e.g.
    "1:1.6.8-1" for PipeWire (the "1:" is an epoch, "-1" is the pkgrel).
    Neither is part of the upstream version number, so both must be
    stripped before comparing against a tag/release version like
    "1.6.8" — left in place, the epoch's ":" makes the leading token
    non-numeric, which _version_sort_key then always ranks *below* any
    purely-numeric upstream version. That silently broke every
    target-version check for epoched packages: every correctly-parsed
    candidate looked "too old" and was rejected, no matter how new it
    actually was.
    """
    v = (v or "").strip()
    if ":" in v:
        v = v.split(":", 1)[1]
    # pkgver itself cannot contain "-" per Arch packaging conventions,
    # so the final "-" (if any remains) always separates it from pkgrel.
    if "-" in v:
        v = v.rsplit("-", 1)[0]
    return v


def _target_version_satisfied(versions: list[dict], target_version: str) -> bool:
    """
    Sanity check: does the best (already sorted newest-first) version in
    `versions` look at least as new as `target_version` — the version
    pacman/AUR/Flatpak/Snap actually reports as installed or pending?
    If either side can't be parsed into a meaningful key, we can't
    validate, so return True rather than block on an odd version string.

    Comparison is truncated to the shorter of the two parsed keys before
    comparing. Without this, a packaging-added suffix with no upstream
    equivalent — e.g. VLC's pacman pkgver "3.0.23_2" vs. the upstream
    page's plain "3.0.23" — would parse to a *longer* key than the
    upstream version, and Python tuple comparison then treats the
    shorter, otherwise-identical prefix as "less than" it: a real match
    would be wrongly flagged as a mismatch on every such package.
    """
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
    """
    Does the target version (the one pacman/AUR/Flatpak/Snap actually
    reports as installed or pending) appear, essentially verbatim, among
    the returned changelog entries? This is a stronger positive signal
    than _target_version_satisfied's "the newest entry is at least as
    new" check — a changelog's top entry can outrank the target
    numerically (an "Unreleased" section, a future-dated heading, a
    rolling-release testing build newer than what's actually installed)
    without the changelog actually documenting the specific version the
    user has. Uses the same epoch/pkgrel stripping and shared-prefix
    truncation as _target_version_satisfied, so e.g. pacman's
    "1:1.6.8-1" still matches an upstream "1.6.8" entry.
    """
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
    """
    Priority (each candidate list is re-sorted by parsed semantic
    version, newest first, and checked against `target_version` — the
    version pacman/AUR/Flatpak/Snap actually reports as installed or
    pending. A result is only returned immediately if its newest entry
    looks at least as new as `target_version`; otherwise it's kept as a
    fallback and the next method is tried).

    This exists because GitLab's own ordering can't be trusted at face
    value: on mirrored/imported repos, tags can share one bulk-import
    "updated" timestamp, so the API's default sort is effectively
    arbitrary — and the "does this tag have a usable message" filter
    below can end up preferring an old tag with a nicely-written message
    over the real latest tag, which may have none. Without this check,
    a project like PipeWire could show a changelog for "1.0" or
    "0.3.27" even when 1.6.8 is actually current.

    1. For known bot-protected hosts, try git fallback first (API blocked).
    2. GitLab Releases API (/releases) — formal Release objects.
    3. Tags API (/repository/tags) with real changelog text.
    4. A NEWS/CHANGELOG file on the repo's default branch.
    5. Raw git tags (git ls-remote + each tag's annotation message) —
       last resort, mainly useful for hosts that block the REST API.
    """
    best_stale: Optional[dict] = None        # newest entry looked older than target
    best_unconfirmed: Optional[dict] = None  # satisfies target, but exact version not literally listed

    def _consider(result: Optional[dict]) -> Optional[dict]:
        """Sort a candidate result's versions newest-first. A result with
        the exact target version confirmed present returns immediately —
        the best possible outcome. A result that only satisfies the
        weaker "newest entry looks at least as new" check is stashed as
        a fallback rather than returned right away, so a later, better
        method (e.g. an actual NEWS file) still gets a chance to produce
        a fully-confirmed match instead of settling for the first
        plausible-looking one."""
        nonlocal best_stale, best_unconfirmed
        if not result or not result.get("versions"):
            return None
        result["versions"].sort(
            key=lambda v: _tag_selection_key(v.get("version", "")), reverse=True)
        if _target_version_satisfied(result["versions"], target_version):
            if target_version and not _versions_contain_target(result["versions"], target_version):
                # Newest entry is at least as new as the target, but the
                # exact target version isn't in the list — often fine
                # (rolling-release testing builds, a changelog that
                # skips versions), but also how a completely different
                # release lineage (e.g. a project's next major version,
                # numbered independently of the one actually installed)
                # can look like a plausible match. Keep searching for a
                # confirmed result before settling for this.
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

    # 2. Releases API — pull a wider window (20, not 6) so a real
    # release isn't missed just because GitLab's own ordering puts it
    # outside the first few entries.
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

    # 3. Tags API — same treatment: pull a wider window and re-sort by
    # parsed version rather than trusting GitLab's "updated" ordering.
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
                       # Drop the tag's own generic "Release version X.Y.Z"
                       # line — it repeats the version number with no
                       # actual changelog content.
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

    # 4. NEWS/CHANGELOG file in the repo root (very common for GNOME
    # and other C/Meson projects that skip GitLab Releases entirely).
    r = _consider(_fetch_gitlab_news_file(host, repo))
    if r:
        return r

    r = _consider(_gitlab_git_fallback(host, repo, _pkg_name))
    if r:
        return r

    # Nothing produced a fully-confirmed match. Prefer a result that at
    # least satisfied the "newest entry looks new enough" check over one
    # that didn't — showing the best available result, clearly labelled,
    # beats nothing at all.
    if best_unconfirmed:
        return best_unconfirmed
    if best_stale:
        best_stale["source"] += "  [may not include the latest release]"
        best_stale["_version_mismatch"] = True
        return best_stale
    return None


def _gitlab_default_branch(host: str, repo: str) -> Optional[str]:
    """
    Look up the project's actual default branch via the GitLab API, so
    NEWS/CHANGELOG lookups try the real default branch first instead of
    only guessing common names — avoids picking up a stale file from an
    unrelated branch that happens to be tried earlier in the guess list.
    """
    encoded = urllib.parse.quote(repo, safe="")
    data = http_get_json(f"https://{host}/api/v4/projects/{encoded}")
    if data and isinstance(data, dict):
        db = data.get("default_branch")
        if db:
            return db
    return None


def _fetch_gitlab_news_file(host: str, repo: str) -> Optional[dict]:
    """Try NEWS/CHANGELOG files via GitLab's raw-file endpoint, on the
    project's real default branch only (looked up via the API; falls
    back to "main" as a single guess if that lookup itself fails)."""
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
                # Files aren't guaranteed to list entries strictly
                # newest-first (merges/edits can leave them out of
                # order) — re-sort by parsed version to be sure.
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
    repo_url = f"https://{host}/{repo}.git"
    # Use shorter timeout for git operations; some repos may be slow/blocked
    out, _, rc = run_git(["git", "ls-remote", "--tags", "--refs", repo_url], timeout=10)
    if rc != 0 or not out:
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

    # Use the shared version key (also used to re-sort GitLab API results)
    # rather than a separate local copy.
    tags.sort(key=lambda tr: _tag_selection_key(tr[0]), reverse=True)
    tags = tags[:6]
    versions = []
    with tempfile.TemporaryDirectory(prefix="pakchan-git-") as tmpdir:
        init_rc = run(["git", "init", "--bare", tmpdir])[2]
        if init_rc != 0:
            return None
        git = ["git", "-C", tmpdir]
        if run(git + ["remote", "add", "origin", repo_url])[2] != 0:
            return None

        for tag, _sha in tags:
            fetch_rc = run(git + ["fetch", "--quiet", "--depth", "1", "origin",
                                  f"refs/tags/{tag}:refs/tags/{tag}"])[2]
            if fetch_rc != 0:
                continue
            date_out, _, date_rc = run(git + ["show", "-s", "--format=%cI", f"refs/tags/{tag}"])
            body_out, _, body_rc = run(git + ["show", "-s", "--format=%B", f"refs/tags/{tag}"])
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
    # NOTE: mappings are checked centrally by callers via `_check_mappings_first()`.
    # `_upstream_changelog` therefore only tries direct repo URLs and homepage
    # discovery (no mappings duplication) and returns a link-only fallback if
    # discovery finds a repo but no usable releases.
    # Preserve custom parser and release-page entries so callers that invoke
    # `_upstream_changelog` directly (tests and integrations) still work.
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

    # Known GitLab/GitHub mappings: return a link-only fallback here so callers
    # invoking `_upstream_changelog` directly still get a result without
    # duplicating release scraping (scraping is handled by `_check_mappings_first`).
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
    # 5. Direct GitLab URL
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
    """
    Always check every mapping type BEFORE any other source, in this
    order: github -> gitlab -> custom (mantisbt/text_file/github_raw/
    mozilla/filezilla) -> release_pages.

    For "release_pages" entries, scraping arbitrary third-party sites
    proved too unreliable across different HTML structures — instead we
    show a direct, clickable link to the official changelog page. This is
    simple and always correct, even if it requires one extra click.

    For "custom" entries, the parser is still attempted since these are
    simpler, well-defined formats.
    """
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

    # Custom parser (mantisbt, text_file, github_raw, …)
    if name in KNOWN_CUSTOM:
        entry = KNOWN_CUSTOM[name]
        url   = entry.get("url", "")
        r     = _scrape_custom(pkg.name, entry, target_version)
        # Unlike the gitlab parser branch (which validates internally via
        # _gitlab_releases), the other custom parsers (text_file,
        # github_raw, mozilla, filezilla) had no target-version check at
        # all — a wrong/unrelated page match would be shown unvalidated.
        if r and r.get("versions") and _target_version_satisfied(r["versions"], target_version):
            if target_version and not _versions_contain_target(r["versions"], target_version):
                r["_version_unconfirmed"] = True
            return r
        # Mapping exists but scraping failed, or didn't pass the version
        # check — return URL fallback, not the unvalidated result.
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"Custom ({entry.get('parser', '')}) — {url}",
        }

    # Dedicated release page — try the generic heuristic scraper first;
    # only fall back to a plain link if it isn't confident enough to trust.
    if name in KNOWN_RELEASE_PAGES:
        url = KNOWN_RELEASE_PAGES[name]
        r = _generic_release_page_scraper(url, pkg.name)
        # Unlike the GitLab resolver, there's no further fallback method
        # to try here — so a version mismatch means we likely scraped the
        # wrong thing entirely (a different app's blog post, an old news
        # item, etc). Showing that with a warning label wasn't enough in
        # practice: wrong content is worse than an honest link, so this
        # discards the result and falls through to the plain link instead.
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

    # Steps 4/5 (known GitLab/GitHub mapping) are skipped here: this
    # point is only reached when _check_mappings_first (step 1) already
    # returned nothing, which — by construction — means the package
    # name isn't in KNOWN_GITLAB_REPOS or KNOWN_GITHUB_REPOS either (that
    # function checks both exhaustively and always returns a result,
    # real or link-fallback, whenever either matches). Re-checking them
    # here could never do anything.
    name = pkg.name.lower()
    target_version = pkg.new_version or pkg.version

    # 3. Direct GitHub/GitLab URL — pkg.url is already loaded (read from
    # the local pacman database at package-load time, same URL shown in
    # the Info tab); pacman -Si is only queried as a fallback on the rare
    # chance it's genuinely missing, not as a matter of course.
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

    # 4. Homepage scraping for an indirect GitHub/GitLab link (e.g.
    #    apps.gnome.org/Calendar, which links out to gitlab.gnome.org).
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

    # Nothing found. Unlike AUR (which has its own PKGBUILD history via
    # AUR's cgit log as a last resort), pacman packages don't get an
    # "Arch packaging GitLab" fallback here — that repo only ever
    # reflects packaging changes (version bumps, rebuilds), not the
    # actual upstream changelog, and wasn't judged useful enough to be
    # worth the extra network round-trip for official-repo packages.
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

    # Step 4 (known GitLab/GitHub mapping) is skipped here: this point
    # is only reached when _check_mappings_first (step 1) already
    # returned nothing, which — by construction — means the package
    # name isn't in KNOWN_GITLAB_REPOS or KNOWN_GITHUB_REPOS either.
    name = pkg.name.lower()
    target_version = pkg.new_version or pkg.version

    # 3. Direct GitHub/GitLab URL — pkg.url is already loaded (fetched at
    # package-load time via AUR RPC, same URL shown in the Info tab); a
    # fresh RPC call is only made as a fallback if it's genuinely missing.
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

    # 4. Homepage scraping (GitHub or GitLab) — try this BEFORE the AUR
    # cgit fallback below. AUR cgit only ever shows PKGBUILD packaging
    # commits, never the upstream project's real changelog, so it should
    # be a last resort rather than something that pre-empts finding the
    # real upstream source via the package's homepage.
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

    versions = []
    headers  = {"User-Agent": "Pakchan/2.0",
                 "Snap-Device-Series": "16",
                 "Snap-Device-Architecture": "amd64"}
    try:
        req = urllib.request.Request(
            f"https://api.snapcraft.io/v2/snaps/info/{urllib.parse.quote(pkg.name)}",
            headers=headers)
        with urllib.request.urlopen(req, timeout=14) as r:
            data = json.loads(r.read())
    except Exception:
        data = None
    if data and isinstance(data, dict):
        seen_ver: set[str] = set()
        for entry in (data.get("channel-map") or []):
            if not isinstance(entry, dict): continue
            ver  = str(entry.get("version") or "")
            rev  = str(entry.get("revision") or "")
            date = str(entry.get("created-at") or "")[:10]
            if not ver or ver in seen_ver: continue
            seen_ver.add(ver)
            versions.append({"version": f"{ver} (rev {rev})" if rev else ver,
                             "date": date,
                             "changes": ["See Snap Store for detailed release notes."]})
            if len(versions) >= 4: break
    if not pkg.url:
        out, _, rc = run(["snap", "info", pkg.name])
        if rc == 0:
            for line in out.splitlines():
                if line.startswith("website:"):
                    pkg.url = line.split(":", 1)[1].strip()
                    break
    if pkg.url:
        r = _upstream_changelog(pkg.url, pkg.name, pkg.version)
        if r and r.get("versions"): return r
    if not versions:
        versions = [{"version": pkg.version, "date": "",
                     "changes": ["Changelog not available via Snap Store API."]}]
        return {"versions": versions, "source": "Snap Store",
                "_manual_check_url": pkg.url or None}
    return {"versions": versions, "source": "Snap Store"}


def fetch_changelog(pkg: Package) -> dict:
    """Fix #6/#11: keyed by repo:name, respects expiry."""
    key    = pkg.cl_key
    cached = _cl_cache_get(key)
    if cached and not cached.get("_stale"):
        cached["_from_cache"] = True
        return cached

    _dbg_reset()
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
    except Exception as e:
        _dbg(f"EXCEPTION: {e}")
        if cached:      # return stale on error
            cached["_from_cache"] = True
            cached["_debug"] = _dbg_get()
            return cached
        return {"versions": [], "error": str(e), "source": "error", "_debug": _dbg_get()}

    debug_trace = _dbg_get()
    if result.get("versions") and not result.get("_link_only"):
        _cl_cache_set(key, dict(result))   # cache a copy without _debug bloating disk
    result["_debug"] = debug_trace
    return result


# ─── GTK Application ──────────────────────────────────────────────────────────

def _resolve_source_url(changelog: dict) -> Optional[str]:
    """
    Best-effort extraction of a real URL for the "Source:" line, so it can
    be shown as a clickable link instead of plain text. Prefers an explicit
    `_link_url` (already set on link-only results), then a full URL
    embedded directly in the `source` text, then reconstructs one for
    sources that only name a repo path (e.g. "GitHub Releases — owner/repo").
    Returns None if nothing usable can be found — caller falls back to a
    plain (non-clickable) label in that case.
    """
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


class PakchanApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.dodog.Pakchan")
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
        self.current_tab    = "changelog"
        self.current_filter = "all"
        self.current_sort   = SORT_OPTIONS[0]
        self._sync_ok       = True

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
        """
        p.load_from_bytes(GLib.Bytes.new(css))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._css()
        self.icon_theme = Gtk.IconTheme.get_for_display(Gdk.Display.get_default())
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(root)

        # Header bar
        hb = Adw.HeaderBar()
        hb.set_title_widget(Gtk.Label(label="Pakchan"))

        ref = Gtk.Button(icon_name="view-refresh-symbolic")
        ref.set_tooltip_text("Refresh packages")
        ref.connect("clicked", lambda _: self._load_packages())
        hb.pack_start(ref)

        self.apply_btn = Gtk.Button(label="Apply (0)")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.set_sensitive(False)
        self.apply_btn.connect("clicked", self._apply_updates)
        hb.pack_end(self.apply_btn)

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

        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        left.append(self._build_sidebar())
        left.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        pkg_panel = self._build_pkg_panel()
        pkg_panel.set_hexpand(True)
        left.append(pkg_panel)
        # Reasonable floor for the sidebar+list side so the divider can't
        # squeeze it down to almost nothing before shrink is disabled below.
        left.set_size_request(400, -1)
        self.paned.set_start_child(left)
        self.paned.set_resize_start_child(True)
        # By default Gtk.Paned allows shrinking either side all the way to
        # 0 regardless of the child's requested minimum size — that's what
        # let the divider hide a whole column when dragged to an edge.
        # Disabling shrink makes each side's natural minimum a hard floor.
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

        # Integrated update panel — slides up in place of opening an
        # external terminal window. Hidden until an update is applied.
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

        # Global shortcuts: Ctrl+F focuses search (selecting existing text,
        # matching browser-style "type to replace" behavior); Escape clears
        # search if there's anything typed, otherwise returns focus to the
        # package list. Attached to the window so it works regardless of
        # which widget currently has focus.
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
            ("all",     "All",     "view-app-grid-symbolic"),
            ("pacman",  "Pacman",  "system-software-update-symbolic"),
            ("aur",     "AUR",     "applications-development-symbolic"),
            ("flatpak", "Flatpak", "application-x-executable-symbolic"),
            ("snap",    "Snap",    "package-x-generic-symbolic"),
            ("updates", "Updates", "software-update-available-symbolic"),
        ]:
            btn = self._mkbtn(label, icon)
            btn.connect("clicked", self._on_filter, key)
            self._filter_btns[key] = btn
            sb.append(btn)

        self.current_filter = "all"
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
        self.search.set_placeholder_text("Search… (Enter)")
        self.search.set_hexpand(True)
        self.search.connect("activate", lambda _: self._do_search())
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

        # Fix #17: "Update all" button
        self.upd_all_btn = Gtk.Button(label="Update all")
        self.upd_all_btn.add_css_class("suggested-action")
        self.upd_all_btn.connect("clicked", self._on_update_all)
        self.upd_all_btn.set_visible(False)
        tb.append(self.upd_all_btn)

        box.append(tb)
        box.append(Gtk.Separator())

        sc = Gtk.ScrolledWindow()
        sc.set_vexpand(True)
        sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.connect("row-selected", self._on_row_selected)
        # Fix #14: keyboard navigation
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
        """
        Real app icon (PAMAC-style) when one can be resolved from the
        system icon theme, otherwise a generic per-source placeholder —
        never a broken/blank image. Source of the icon name:
          - pacman/AUR: the Icon= key read from the package's installed
            .desktop file (see _desktop_entries_info).
          - Flatpak: the app ID itself (Flatpak exports icons under it).
          - Snap: the snap name, as a best-effort guess.
        """
        img = Gtk.Image()
        img.set_pixel_size(32)
        name = pkg.icon_name
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
        cb.set_active(pkg.checked)
        cb.set_sensitive(pkg.has_update)
        cb.set_visible(self.current_filter == "updates")
        cb.connect("toggled", self._on_pkg_check, pkg)
        hb.append(cb)

        hb.append(self._icon_widget_for(pkg))

        nb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        nb.set_hexpand(True)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        nl  = Gtk.Label(label=pkg.name)
        nl.set_xalign(0); nl.set_ellipsize(Pango.EllipsizeMode.END)
        nl.add_css_class("heading"); top.append(nl)
        badge = Gtk.Label(label=pkg.repo)
        badge.add_css_class(f"badge-{pkg.repo}"); top.append(badge)
        if pkg.is_dep:
            dep = Gtk.Label(label="dep"); dep.add_css_class("dep-tag")
            top.append(dep)
        nb.append(top)

        vb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        vl = Gtk.Label(label=pkg.version)
        vl.add_css_class("dim-label"); vl.set_xalign(0); vb.append(vl)
        if pkg.has_update:
            vb.append(Gtk.Label(label="→"))
            nl2 = Gtk.Label(label=pkg.new_version)
            nl2.add_css_class("has-update"); vb.append(nl2)
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
        """Right-click menu: copy name, open homepage, force-refresh the
        changelog — actions that otherwise require opening the detail
        panel first."""
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
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception:
            self.footer.set_text(f"Could not open {url}")

    def _context_force_refresh(self, pkg: Package):
        self._force_refresh_cl(pkg)
        self.footer.set_text(f"Changelog cache cleared for {pkg.name}.")

    # ── Sort ──────────────────────────────────────────────────────────────────

    def _relevance_score(self, p: Package) -> tuple:
        """
        Lower tuple sorts first. Mirrors PAMAC's "user-facing first"
        heuristic:
          1. Explicit installs before dependency-only packages.
          2. Packages with a desktop launcher (GUI apps you'd actually
             open) before CLI tools / libraries with no .desktop file.
          3. Flatpak/AUR/Snap apps (almost always explicitly chosen by
             the user) rank with explicit pacman installs, not below them.
          4. Alphabetical as the final tiebreaker.
        """
        explicit_rank = 0 if not p.is_dep else 1
        gui_rank      = 0 if p.has_desktop_entry else 1
        return (explicit_rank, gui_rank, p.name.lower())

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
        elif flt == "updates":
            pool = [p for p in self.all_packages if p.has_update]
        elif flt == "all":
            pool = list(self.all_packages)
        else:
            pool = [p for p in self.all_packages if p.repo == flt]

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

        # Fix issue 1: progressive rendering in chunks so UI stays responsive
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

        # Fix #15/#17: show/hide controls based on filter
        is_upd = (flt == "updates")
        self.sel_all.set_visible(is_upd)
        self.upd_all_btn.set_visible(is_upd)
        self.upd_all_btn.set_sensitive(any(p.has_update for p in pool))

        self._update_counts_label()
        self._update_footer()

        total = sum(1 for p in self.all_packages if p.checked)
        self.apply_btn.set_sensitive(total > 0)
        self.apply_btn.set_label(f"Apply ({total})")

    def _update_counts_label(self):
        """Rebuild the "N packages · N selected" label under the list.
        Split out from _populate_list so a single checkbox toggle can
        refresh the selected-count text without re-rendering all rows.
        """
        pool    = self.filtered
        flt     = self.current_filter
        q       = self.search.get_text().lower().strip()
        n       = len(pool)
        n_upd   = sum(1 for p in pool if p.has_update)
        checked = sum(1 for p in self.all_packages if p.checked)
        parts   = [f"{n} package{'s' if n != 1 else ''}"]
        if flt != "updates" and n_upd:
            parts.append(f"{n_upd} with updates")
        if q:
            parts.append("search results")
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
        if flt == "all":
            self.footer.set_text(
                f"{len(pkgs)} packages total · {n_upd} update{'s' if n_upd!=1 else ''} available"
                f" · Pacman {n_p}  AUR {n_a}  Flatpak {n_f}  Snap {n_s}")
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
        pkgs, sync_ok = get_all_packages_fast()
        GLib.idle_add(self._on_loaded, pkgs, sync_ok)

    def _on_loaded(self, pkgs: list, sync_ok: bool):
        self.all_packages = pkgs
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
        return False

    # ── Events ────────────────────────────────────────────────────────────────

    def _on_filter(self, btn, key):
        self.current_filter = key
        self.search.set_text("")   # clear search — restores category browsing
        self._hl_sidebar()
        if key != "updates":
            for p in self.all_packages: p.checked = False
            self.sel_all.set_active(False)
        self._populate_list()

    def _do_search(self):
        q = self.search.get_text().strip()
        if q:
            # Search crosses all sources — reset sidebar highlight to "all"
            # but don't change current_filter so user can go back
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
            if p.has_update: p.checked = btn.get_active()
        self._populate_list()

    def _on_update_all(self, btn):
        """Fix #17: select all updatable packages."""
        for p in self.all_packages:
            p.checked = p.has_update
        self.sel_all.set_active(True)
        self._populate_list()

    def _on_pkg_check(self, cb, pkg: Package):
        pkg.checked = cb.get_active()
        total = sum(1 for p in self.all_packages if p.checked)
        self.apply_btn.set_sensitive(total > 0)
        self.apply_btn.set_label(f"Apply ({total})")
        self._update_counts_label()
        self._update_footer()

    def _on_row_selected(self, lb, row):
        if row is None: return
        pkg = row.pkg
        self.selected_pkg = pkg
        self.d_name.set_markup(f"<b>{GLib.markup_escape_text(pkg.name)}</b>")
        self.d_desc.set_text(pkg.description or "Loading…")
        if pkg.icon_name and self.icon_theme.has_icon(pkg.icon_name):
            self.d_icon.set_from_icon_name(pkg.icon_name)
        else:
            self.d_icon.set_from_icon_name(self._ICON_FALLBACK.get(pkg.repo, "package-x-generic-symbolic"))
        if pkg.repo in ("flatpak", "snap") and (not pkg.description or not pkg.url):
            threading.Thread(target=self._enrich_bg, args=(pkg,), daemon=True).start()
        self._render_detail()

    def _enrich_bg(self, pkg: Package):
        enrich_pkg(pkg)
        GLib.idle_add(self._enrich_done, pkg)

    def _enrich_done(self, pkg: Package):
        if self.selected_pkg and self.selected_pkg.name == pkg.name:
            self.d_desc.set_text(pkg.description or "No description available.")
            if self.current_tab == "info":
                self._render_detail()
        return False

    def _on_tab(self, btn, key):
        self.current_tab = key
        for k, b in self._tab_btns.items():
            b.set_active(k == key)
        self._render_detail()

    # Fix #14: keyboard arrow navigation
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

    def _render_info(self, pkg: Package):
        self._info_row("Source",    pkg.repo.upper())
        self._info_row("Installed", pkg.version)
        if pkg.has_update:     self._info_row("Update to",  pkg.new_version)
        if pkg.installed_size: self._info_row("On disk",    pkg.installed_size)
        if pkg.license:        self._info_row("License",    pkg.license)
        if pkg.url:            self._info_row("URL",        pkg.url, is_url=True)
        if pkg.depends:        self._info_row("Depends",    pkg.depends)
        if pkg.is_dep:
            note = Gtk.Label(label="ⓘ Installed as a dependency")
            note.add_css_class("dim-label"); note.set_xalign(0); note.set_margin_top(6)
            self.d_box.append(note)

    def _render_changelog(self, pkg: Package):
        if pkg.changelog is None:
            sp = Gtk.Spinner(); sp.start()
            sp.set_size_request(24, 24); sp.set_halign(Gtk.Align.CENTER)
            self.d_box.append(sp)
            lbl = Gtk.Label(label="Fetching changelog…")
            lbl.add_css_class("dim-label"); lbl.set_halign(Gtk.Align.CENTER)
            self.d_box.append(lbl)
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

        # If this package only has a release_pages mapping (no scraping
        # attempted), show a direct clickable link at the top and stop —
        # this is the simple, always-correct fallback requested by the user.
        if pkg.changelog.get("_link_only"):
            url = pkg.changelog.get("_link_url", "")
            escaped_url = GLib.markup_escape_text(url)
            link_lbl = Gtk.Label()
            link_lbl.set_markup(f'See <a href="{escaped_url}">{escaped_url}</a> for details.')
            link_lbl.set_xalign(0)
            link_lbl.set_wrap(True); link_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            link_lbl.set_hexpand(True)
            link_lbl.set_margin_bottom(4)
            self.d_box.append(link_lbl)

            ref_btn = Gtk.Button(label="↻ Refresh")
            ref_btn.add_css_class("flat"); ref_btn.set_halign(Gtk.Align.START)
            ref_btn.connect("clicked", lambda _: self._force_refresh_cl(pkg))
            self.d_box.append(ref_btn)

            self._append_debug_expander(pkg)
            return

        # Source label + cache indicator — only the URL portion is
        # clickable (via an inline markup link), not the whole line.
        src_desc = pkg.changelog.get('source', '')
        if pkg.changelog.get("_from_cache"):
            src_desc += "  [cached]"
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
                # Source text may only contain the repo path (e.g.
                # "owner/repo"), not the full reconstructed URL — strip
                # that instead so it isn't shown twice.
                path_part = re.sub(r'^https?://[^/]+/?', '', src_url)
                if path_part and path_part in desc_text:
                    desc_text = desc_text.replace(path_part, "").rstrip(" —-")
            escaped_desc = GLib.markup_escape_text(f"Source: {desc_text}".rstrip())
            escaped_url  = GLib.markup_escape_text(src_url)
            src.set_markup(f'{escaped_desc}  <a href="{escaped_url}">{escaped_url}</a>')
        else:
            src.set_text(f"Source: {src_desc}")
        src.add_css_class("dim-label"); src.set_margin_bottom(2)
        self.d_box.append(src)

        # When automatic changelog detection genuinely found nothing, the
        # Source line above stays "unavailable" (so it's unambiguous that
        # detection failed) — this adds a clickable link to the package's
        # own homepage underneath it, so the user has a manual next step
        # instead of a dead end.
        manual_url = pkg.changelog.get("_manual_check_url")
        if manual_url:
            escaped_url = GLib.markup_escape_text(manual_url)
            manual_lbl = Gtk.Label()
            manual_lbl.set_markup(
                f'You can check manually: <a href="{escaped_url}">{escaped_url}</a>')
            manual_lbl.set_xalign(0)
            manual_lbl.set_wrap(True); manual_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            manual_lbl.set_hexpand(True)
            manual_lbl.add_css_class("dim-label"); manual_lbl.set_margin_bottom(2)
            self.d_box.append(manual_lbl)

        # Fix #6: stale warning
        if pkg.changelog.get("_stale"):
            stale_lbl = Gtk.Label(label="⚠ Cached data may be outdated (>7 days)")
            stale_lbl.add_css_class("stale-warn"); stale_lbl.set_xalign(0)
            self.d_box.append(stale_lbl)

        # Newest version found didn't match the installed/pending version
        # (see _target_version_satisfied) — shown rather than hidden, since
        # a wrong-but-labelled changelog is more useful than a silently
        # misleading one.
        if pkg.changelog.get("_version_mismatch"):
            mismatch_lbl = Gtk.Label(
                label="⚠ This may not be the changelog for the current version")
            mismatch_lbl.add_css_class("stale-warn"); mismatch_lbl.set_xalign(0)
            self.d_box.append(mismatch_lbl)

        # Softer than _version_mismatch: the newest entry looked at
        # least as new as the installed/pending version, but that exact
        # version isn't literally listed — often fine (a rolling-release
        # testing build ahead of what's installed, a changelog that
        # skips versions), but worth a quiet note rather than implying
        # an exact match was confirmed.
        elif pkg.changelog.get("_version_unconfirmed"):
            unconfirmed_lbl = Gtk.Label(
                label="ℹ Exact update version not listed below — "
                      "check the source link above for details")
            unconfirmed_lbl.add_css_class("dim-label"); unconfirmed_lbl.set_xalign(0)
            unconfirmed_lbl.set_wrap(True); unconfirmed_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            unconfirmed_lbl.set_hexpand(True)
            self.d_box.append(unconfirmed_lbl)

        ref_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
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
        """
        Show exactly which resolution steps were tried for this package
        and what each one did — so changelog problems can be diagnosed
        directly from the UI instead of guessing.
        """
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
        if self.selected_pkg and self.selected_pkg.name == pkg.name:
            self._render_detail()

    def _render_files(self, pkg: Package):
        """Fix #20: walk real Flatpak deploy directory.
        Fix: pacman file listing now runs on a background thread —
        `pacman -Ql` can be slow for large packages and was previously
        run synchronously on the GTK main thread, freezing the UI.
        """
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
        if (self.selected_pkg and self.selected_pkg.name == pkg.name
                and self.current_tab == "files"):
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
        pkg.changelog = fetch_changelog(pkg)
        GLib.idle_add(self._cl_done, pkg)

    def _cl_done(self, pkg: Package):
        if (self.selected_pkg and self.selected_pkg.name == pkg.name
                and self.current_tab == "changelog"):
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
        dlg.set_license_type(Gtk.License.GPL_3_0)
        dlg.set_developers(["Pakchan contributors"])
        dlg.set_copyright("© 2025 Pakchan contributors")

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
    # Replaces the old "spawn an external terminal window" approach. The
    # update runs in a real pty (via Vte if available, otherwise a plain
    # pty-backed fallback) inside a panel that slides up at the bottom of
    # the window, so `sudo`/makepkg prompts still work exactly as before,
    # but the whole thing stays inside Pakchan. When the process finishes,
    # we simply reload the package list from disk — that's the reliable
    # way to know what's actually installed now (rather than trying to
    # infer it from parsed terminal output), and it also clears the
    # checkbox/"has update" state for whatever just got updated.

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
        # Collapsed by default: this row alone acts as the status bar.
        # Clicking it reveals the full log below without changing anything
        # about the header itself.
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

        self.update_log_revealer = Gtk.Revealer()
        self.update_log_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.update_log_revealer.set_reveal_child(False)
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        log_box.append(Gtk.Separator())

        if _HAVE_VTE:
            self.vte_term = Vte.Terminal()
            self.vte_term.set_size_request(-1, 220)
            self.vte_term.set_hexpand(True)
            self.vte_term.connect("child-exited", self._on_update_child_exited)
            sc = Gtk.ScrolledWindow()
            sc.set_child(self.vte_term)
            log_box.append(sc)
        else:
            # Fallback: no Vte on this system. Still a real pty underneath
            # (so pkexec/sudo detect a tty correctly) — just rendered as a
            # plain scrolling log instead of a proper terminal. No input
            # box: with --noconfirm everywhere and pkexec handling the
            # password via its own dialog, there's nothing left to type
            # back into the update itself.
            self.update_textview = Gtk.TextView()
            self.update_textview.set_editable(False)
            self.update_textview.set_cursor_visible(False)
            self.update_textview.set_wrap_mode(Gtk.WrapMode.CHAR)
            self.update_textview.add_css_class("update-log")
            sc = Gtk.ScrolledWindow()
            sc.set_child(self.update_textview)
            sc.set_size_request(-1, 220)
            log_box.append(sc)

        self.update_log_revealer.set_child(log_box)
        box.append(self.update_log_revealer)
        return box

    def _on_toggle_update_log(self, btn):
        expanded = self.update_log_revealer.get_reveal_child()
        self.update_log_revealer.set_reveal_child(not expanded)
        btn.set_icon_name("pan-up-symbolic" if not expanded else "pan-down-symbolic")
        btn.set_tooltip_text("Hide details" if not expanded else "Show details")

    def _apply_updates(self, btn):
        sel = [p for p in self.all_packages if p.checked]
        if not sel: return
        dlg = Adw.AlertDialog(
            heading="Apply updates?",
            body=f"Update {len(sel)} package(s). You'll be asked for your "
                 f"password in the usual system prompt.",
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("apply", "Apply")
        dlg.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("apply")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._on_apply_dialog_response, sel)
        dlg.present(self)

    def _on_apply_dialog_response(self, dlg, response, sel: list):
        if response == "apply":
            self._do_apply(sel)

    def _make_sudo_shim(self) -> Optional[str]:
        """Write a tiny `sudo` shim that redirects to `pkexec`, in its own
        temp dir. When that dir is prepended to PATH, any command the
        update script runs — including an AUR helper's own internal
        `sudo` call for the final `pacman -U` step — asks for the
        password via the normal graphical polkit prompt (a separate
        system dialog) instead of pakchan trying to read it from inside
        its own log panel, which was confusing and didn't actually work.
        """
        if not cmd_exists("pkexec"):
            return None
        try:
            d = tempfile.mkdtemp(prefix="pakchan-sudo-")
            shim = Path(d) / "sudo"
            shim.write_text("#!/bin/sh\nexec pkexec \"$@\"\n")
            shim.chmod(0o755)
            return d
        except Exception:
            return None

    def _do_apply(self, sel: list):
        # Fix #7: shlex.quote all package names — prevents shell injection
        pac = [shlex.quote(p.name) for p in sel if p.repo == "pacman"]
        aur = [shlex.quote(p.name) for p in sel if p.repo == "aur"]
        flt = [shlex.quote(p.name) for p in sel if p.repo == "flatpak"]
        snp = [shlex.quote(p.name) for p in sel if p.repo == "snap"]

        # Use pkexec instead of sudo for our own root commands: it pops
        # the standard graphical password dialog (the polkit agent) as
        # its own window, rather than needing a tty-attached prompt we'd
        # have to surface somewhere in our UI.
        have_pkexec = cmd_exists("pkexec")
        root_cmd = "pkexec" if have_pkexec else "sudo"

        cmds = []
        if pac: cmds.append(f"{root_cmd} pacman -S --noconfirm {' '.join(pac)}")
        if aur:
            h = "yay" if cmd_exists("yay") else "paru"
            cmds.append(f"{h} -S --noconfirm {' '.join(aur)}")
        if flt: cmds.append(f"flatpak update -y {' '.join(flt)}")
        if snp: cmds.append(f"{root_cmd} snap refresh {' '.join(snp)}")
        if not cmds:
            return
        full = " && ".join(cmds)

        # If an AUR helper is involved, it'll call plain `sudo` itself
        # for the final install step — route that through the same
        # pkexec shim so it also uses the graphical prompt.
        self._sudo_shim_dir = self._make_sudo_shim() if (aur and have_pkexec) else None
        path_prefix = (f'export PATH="{self._sudo_shim_dir}:$PATH"; '
                        if self._sudo_shim_dir else "")
        runner = (f'printf "\\033[1m$ {full}\\033[0m\\n"; '
                  f'{path_prefix}{full}; echo; echo "[pakchan] Done."')

        self.apply_btn.set_sensitive(False)
        self.update_spinner.start()
        self.update_close_btn.set_sensitive(False)
        self.update_status_lbl.set_text(f"Updating {len(sel)} package(s)…")
        self.footer.set_text(f"Updating {len(sel)} package(s)…")
        self.update_revealer.set_reveal_child(True)
        # Deliberately not touching update_log_revealer here — whether the
        # log is expanded or collapsed carries over from however the user
        # last left it this session (only resets to collapsed if the app
        # itself is restarted, since the panel is rebuilt fresh then).

        if _HAVE_VTE:
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
        # The terminal widget itself only shows anything once the log is
        # expanded, so poll its buffer for the last line and mirror it
        # onto the always-visible status row/footer (pamac-style), the
        # same way the pty-fallback path already does per output chunk.
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

    # ── Fallback path when Vte isn't installed ───────────────────────────────

    def _run_update_pty_fallback(self, runner: str):
        buf = self.update_textview.get_buffer()
        buf.set_text("")
        # No pty here on purpose: pkexec authenticates via its own GUI
        # dialog (not by checking isatty on our stdin), so we don't need
        # one for that anymore. Without a pty, pacman/AUR helpers detect
        # non-interactive output and print plain, complete lines instead
        # of \r-redrawn progress bars — which sidesteps an entire class
        # of terminal-emulation bugs (cursor tricks, partial redraws)
        # rather than trying to hand-parse them.
        try:
            proc = subprocess.Popen(
                ["/bin/bash", "-lc", runner],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
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
        # Belt-and-braces: strip any ANSI codes a tool might still emit
        # even without a pty (a few don't check isatty before coloring).
        line = _ANSI_ESCAPE_RE.sub("", line).rstrip("\n")
        if line.strip():
            tb = self.update_textview.get_buffer()
            tb.insert(tb.get_end_iter(), line + "\n")
            self.update_textview.scroll_mark_onscreen(tb.get_insert())

            # Surface the current line as the visible status — this is
            # what shows in the collapsed panel row, so it needs to
            # actually say what's happening. Password prompts are
            # skipped: pkexec/the sudo shim handles those via a separate
            # system dialog now.
            if "assword" not in line:
                snippet = line.strip()[:100]
                self.update_status_lbl.set_text(snippet)
                self.footer.set_text(snippet)
        return False

    def _update_finished(self, status: int):
        self.update_spinner.stop()
        self.update_close_btn.set_sensitive(True)
        self.apply_btn.set_sensitive(True)
        shim_dir = getattr(self, "_sudo_shim_dir", None)
        if shim_dir:
            shutil.rmtree(shim_dir, ignore_errors=True)
            self._sudo_shim_dir = None
        ok = (status == 0)
        msg = ("Update finished — refreshing package list…" if ok else
               f"Update process exited with an error (code {status}) — "
               f"refreshing package list anyway…")
        self.update_status_lbl.set_text(msg)
        self.footer.set_text(msg)
        # This is the fix for stale "still selected / still shows as
        # updatable" packages: rather than trying to patch each Package's
        # state from parsed terminal output, just re-read the real state
        # from pacman/AUR/flatpak/snap, the same way startup does.
        self._load_packages()
        return False


# ─── Entry point ──────────────────────────────────────────────────────────────

def _on_exit():
    """Fix #2: Flush changelog DB on clean exit."""
    _cl_db_flush(force=True)


if __name__ == "__main__":
    import atexit
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    _load_mappings_from_cache()   # Fix #1: disk-only at startup, instant
    _cl_db_load()
    atexit.register(_on_exit)     # Fix #2: always flush on exit
    app = PakchanApp()
    sys.exit(app.run(sys.argv))
