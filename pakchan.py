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

# Disable WebKit process sandbox when user namespaces are unavailable
# (avoids "CanCreateUserNamespace() clone() failure: EPERM" on some systems)
import gzip, html, json, os, re, shlex, shutil, sys, tarfile, tempfile, threading, time
os.environ.setdefault("WEBKIT_DISABLE_SANDBOX", "1")
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


# ─── HTTP caching (short-lived session cache to avoid re-fetching) ──────────

_http_cache: dict[str, tuple[str, float]] = {}
_HTTP_CACHE_TTL = 30  # seconds

def http_get_cached(url: str, timeout: int = 14) -> Optional[str]:
    """Fetch URL with short-lived session cache (30s TTL).
    Avoids re-fetching the same URL multiple times in quick succession.
    """
    now = time.time()
    if url in _http_cache:
        body, ts = _http_cache[url]
        if now - ts < _HTTP_CACHE_TTL:
            return body
    body = http_get(url, timeout)
    if body:
        _http_cache[url] = (body, now)
    return body


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
            "size":    " ".join(fields.get("isize", [""])),
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

def _pending_pacman_updates() -> dict:
    out, _, rc = run(["checkupdates"], timeout=45)
    result = {}
    if rc == 0 and out:
        for line in out.splitlines():
            p = line.split()
            if len(p) >= 4:
                result[p[0]] = p[3]
    return result


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


# ─── Package enumeration (parallelised — fix #5) ──────────────────────────────

def _packages_with_desktop_entries() -> set[str]:
    """
    Determine which installed packages own a .desktop launcher file —
    the same signal PAMAC uses to separate "applications you'd actually
    launch" from libraries/CLI tools/background services.

    Reads /var/lib/pacman/local/<pkg-ver>/files directly (already on disk,
    no subprocess) rather than calling `pacman -Ql` for every package.
    """
    result: set[str] = set()
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
        if "share/applications/" in text and ".desktop" in text:
            # Package name is the dir name minus the trailing "-version-rel"
            pkg_ver = pkg_dir.name
            segments = pkg_ver.rsplit("-", 2)
            name = segments[0] if len(segments) >= 2 else pkg_ver
            result.add(name)
    return result


def _load_pacman_aur(local_db: dict, sync_names: set,
                     aur_helper: Optional[str]) -> tuple[list, dict, dict]:
    """Returns (packages, pacman_pending, aur_pending)."""
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_pac = ex.submit(_pending_pacman_updates)
        f_aur = ex.submit(_pending_aur_updates, aur_helper) if aur_helper else None
        f_gui = ex.submit(_packages_with_desktop_entries)
        pacman_pending  = f_pac.result()
        aur_pending     = f_aur.result() if f_aur else {}
        desktop_owners  = f_gui.result()

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
        pkgs.append(Package(
            name=name, version=version, new_version=new_ver,
            description=info.get("desc", ""),
            repo=repo,
            installed_size=_fmt_bytes(info.get("size", "")),
            license=info.get("license", ""),
            url=info.get("url", ""),
            depends=info.get("depends", ""),
            is_dep=(reason == "1"),
            has_desktop_entry=(name in desktop_owners),
        ))
    return pkgs, pacman_pending, aur_pending


def _load_flatpak() -> list:
    if not cmd_exists("flatpak"):
        return []
    fp_pending = _pending_flatpak_updates()
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
            ver = _flatpak_installed_version(app_dir)
            pkgs.append(Package(
                name=app_id, version=ver,
                new_version=fp_pending.get(app_id, ""),
                description="", repo="flatpak",
                has_desktop_entry=True,   # Flatpak apps always ship a .desktop file
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
            ))
    return pkgs


def _flatpak_installed_version(app_dir: Path) -> str:
    try:
        for branch_dir in app_dir.iterdir():
            for arch_dir in branch_dir.iterdir():
                meta = arch_dir / "active" / "metadata"
                if meta.exists():
                    for line in meta.read_text(errors="replace").splitlines():
                        if line.startswith("version="):
                            return line.split("=", 1)[1].strip()
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
    "krita": {"parser": "krita", "url": "https://krita.org/en/"},
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

def _is_gitlab_host(host: str) -> bool:
    """Return True if host is a GitLab instance we should treat as GitLab."""
    if not host:
        return False
    h = host.lower()
    if "gitlab" in h:
        return True
    if any(h.endswith(k) for k in KNOWN_GITLAB_LIKE):
        return True
    return False

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


def _extract_main(html: str) -> Optional[str]:
    """Try to find the main content block of a page."""
    for pattern in [
        r'<main[^>]*>(.*?)</main>',
        r'<article[^>]*>(.*?)</article>',
        r'<div[^>]*class="[^"]*(?:content|main|release|notes|body)[^"]*"[^>]*>(.*?)</div>',
        r'<div[^>]*id="[^"]*(?:content|main|release|notes)[^"]*"[^>]*>(.*?)</div>',
    ]:
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            return m.group(1)
    return None


def _filter_changes(html_items: list[str], min_len: int = 8,
                    max_len: int = 500) -> list[str]:
    """
    Strip HTML from a list of raw <li> inner contents,
    filter out CSS/JS noise and navigation fragments.
    """
    results = []
    for item in html_items:
        text = _strip_html(item).strip()
        if not (min_len < len(text) < max_len):
            continue
        # Skip CSS/JS noise
        if ('{' in text or '}' in text
                or text.startswith('.')
                or text.startswith('@')
                or re.search(r'fill:|behavior:|url\(|^\s*\*\s*\{', text)):
            continue
        # Skip pure navigation fragments (single words / very short)
        if len(text.split()) < 2:
            continue
        results.append(text)
    return results


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

def _scrape_custom(pkg_name: str, entry: dict) -> Optional[dict]:
    """Dispatch to custom parser based on entry['parser'] field."""
    parser = entry.get("parser", "")
    if parser == "gitlab":
        host = entry.get("host", "")
        repo = entry.get("repo", "")
        if host and repo:
            return _gitlab_releases(host, repo, pkg_name)
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
            gitlab_result = _gitlab_releases(host, repo, pkg_name)
            if gitlab_result and gitlab_result.get("versions"):
                return gitlab_result
        # Detect if the page is a bot-protection challenge (Anubis, Cloudflare, etc)
        # and try git fallback instead of giving up
        if _is_bot_protection_page(body):
            _dbg(f"[mantisbt] bot-protection page detected, trying git fallback")
            host = entry.get("host", "")
            repo = entry.get("repo", "")
            if host and repo:
                gitlab_result = _gitlab_releases(host, repo, pkg_name)
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
        return _scrape_text_file(body)
    if parser == "github_raw":
        return _scrape_github_raw_changelog(body)
    if parser == "mozilla":
        return _scrape_mozilla(url, body)
    if parser == "krita":
        return _scrape_krita(body, url)
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


def _scrape_github_raw_changelog(body: str) -> Optional[dict]:
    """Parse a raw CHANGELOG/RELEASE-NOTES file from GitHub (Markdown or plain text)."""
    versions = []
    # Match: ## [X.Y.Z] - YYYY-MM-DD   or   ## X.Y.Z   or   # vX.Y.Z
    for ver, date, block in re.findall(
            r'^#{1,3}\s+\[?v?([\d]+\.[\d.]+[^\]\s]*)\]?'
            r'(?:\s*[-–]\s*(\d{4}-\d{2}-\d{2}))?\s*\n(.*?)(?=^#{1,3}\s|\Z)',
            body, re.DOTALL | re.MULTILINE)[:6]:
        changes = _parse_md_changelog(block)
        versions.append({"version": ver, "date": date,
                         "changes": changes[:10] or [f"Release {ver}"]})
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


def _scrape_krita(body: str, url: str) -> Optional[dict]:
    """Parse Krita release posts from the Krita website."""
    if not body:
        return None

    def _extract_post_area(html: str) -> str:
        m = re.search(r'<div[^>]+class=["\']?post["\']?[^>]*>', html, re.IGNORECASE)
        if not m:
            return html
        start = m.start()
        segment = html[start:]
        depth = 0
        pos = 0
        while pos < len(segment):
            if segment[pos:pos+4].lower() == '<div':
                depth += 1
                pos += 4
                continue
            if segment[pos:pos+6].lower() == '</div>':
                depth -= 1
                pos += 6
                if depth == 0:
                    return segment[:pos]
                continue
            pos += 1
        return segment

    candidates = []
    for href in re.findall(r'href=["\']?([^"\'\s>]+)["\']?', body, re.IGNORECASE):
        if re.search(r'/en/posts/\d{4}/krita-[^/]+-released/?$', href, re.IGNORECASE):
            candidates.append(urllib.parse.urljoin(url, href))
    candidates = list(dict.fromkeys(candidates))[:8]

    versions = []
    for post_url in candidates:
        post_body = http_get(post_url, timeout=12)
        if not post_body:
            continue

        title = ""
        m = re.search(r'<h1[^>]*>(.*?)</h1>', post_body, re.IGNORECASE | re.DOTALL)
        if m:
            title = _strip_html(m.group(1)).strip()

        ver = ""
        if title:
            vm = re.search(r'Krita\s*([0-9]+(?:\.[0-9]+)+)', title, re.IGNORECASE)
            ver = vm.group(1) if vm else title

        date = ""
        dm = re.search(
            r'<meta[^>]+property=["\']article:published_time["\'][^>]+content=["\']([^"\']+)["\']',
            post_body, re.IGNORECASE)
        if not dm:
            dm = re.search(r'<span[^>]*>([^<]*\d{4})</span>', post_body, re.IGNORECASE)
        if dm:
            date = dm.group(1)[:10]

        post_area = _extract_post_area(post_body)
        changes = []

        for li in re.findall(r'<li[^>]*>(.*?)</li>', post_area, re.IGNORECASE | re.DOTALL):
            text = _strip_html(li).strip()
            if text and len(text) > 10:
                changes.append(text)
        if not changes:
            for p in re.findall(r'<p[^>]*>(.*?)</p>', post_area, re.IGNORECASE | re.DOTALL):
                text = _strip_html(p).strip()
                if text and len(text) > 20:
                    changes.extend([line.strip() for line in text.splitlines() if line.strip()])
                    break

        if ver and changes:
            versions.append({"version": ver, "date": date, "changes": changes[:10]})
        elif title and changes:
            versions.append({"version": title, "date": date, "changes": changes[:10]})

    return {"versions": versions, "source": f"Krita release posts — {url}"} if versions else None


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
                repo = repo.rstrip(".git").rstrip("/")
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
                repo = repo.rstrip(".git").rstrip("/")
                _dbg(f"[homepage scan] gitlab candidate -> host={ghost} repo={repo}")
                return ("gitlab", ghost, repo)
            else:
                _dbg(f"[homepage scan] gitlab candidate rejected (not group/project): {raw_full}")
                continue

    _dbg("[homepage scan] no repo link found")
    return None



def _find_github_via_homepage(url: str, pkg_name: str = "") -> Optional[str]:
    """Legacy GitHub-only helper, kept for callers that only know how to
    use a GitHub repo path (most call sites). See _find_repo_via_homepage
    for the GitHub+GitLab-aware version used by the main resolution chain.
    """
    found = _find_repo_via_homepage(url, pkg_name)
    if found and found[0] == "github":
        return found[1]
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


# -- Probe common GitLab hosts using sensible guesses derived from pkg name --
# Try this when homepage scanning failed to discover a repo link (covers
# JS-rendered homepages like apps.gnome.org).
_EXTRA_GITLAB_HOSTS = [
    "gitlab.gnome.org",
    "invent.kde.org",
    "source.kde.org",
    "gitlab.com",
    "gitlab.archlinux.org",
]

def _probe_known_gitlab_hosts(pkg_name: str) -> Optional[dict]:
    """
    Try a few host+path guesses derived from pkg_name against common GitLab
    instances. If a project exists, call _gitlab_releases() and return its result.
    Logs attempts with _dbg so the debug expander shows what was tried.
    """
    name = (pkg_name or "").strip()
    if not name:
        return None

    candidates: list[str] = []
    # Keep original package name
    candidates.append(name)
    # Try without common prefixes
    for pref in ("gnome-", "gdm-", "lib", "libgnome-"):
        if name.startswith(pref):
            candidates.append(name[len(pref):])
    # Try GNOME group prefix (very common for GNOME apps)
    candidates.append(f"GNOME/{name}")
    # Also try GNOME group with the stripped name if we made one
    if name.startswith("gnome-"):
        stripped = name[len("gnome-"):]
        candidates.append(f"GNOME/{stripped}")

    # Deduplicate but keep order
    seen_cand = []
    for c in candidates:
        cclean = c.strip("/")
        if cclean and cclean not in seen_cand:
            seen_cand.append(cclean)
    candidates = seen_cand

    for host in _EXTRA_GITLAB_HOSTS:
        for cand in candidates:
            _dbg(f"[host-probe] trying https://{host}/{cand}")
            # Query the GitLab project endpoint to check existence
            enc = urllib.parse.quote(cand, safe="")
            proj = http_get_json(f"https://{host}/api/v4/projects/{enc}")
            if proj is None:
                _dbg(f"[host-probe] {host}/{cand} -> no project (or API returned nothing)")
                continue
            # Project exists — fetch releases/tags via existing logic
            _dbg(f"[host-probe] {host}/{cand} -> project exists, attempting _gitlab_releases")
            try:
                r = _gitlab_releases(host, cand, pkg_name)
                if r and r.get("versions"):
                    _dbg(f"[host-probe] {host}/{cand} -> releases found ✓")
                    return r
                _dbg(f"[host-probe] {host}/{cand} -> project exists but no release info")
            except Exception as e:
                _dbg(f"[host-probe] {host}/{cand} -> exception in _gitlab_releases: {e}")
    return None

def _fetch_github_changelog_file(repo: str) -> Optional[dict]:
    """
    Suggestion #2: Many projects don't use GitHub Releases at all —
    they maintain a CHANGELOG.md / NEWS / HISTORY file in the repo root.
    Try the most common filenames against the default branch via raw.githubusercontent.com.
    """
    candidates = [
        "CHANGELOG.md", "CHANGELOG", "Changelog.md", "CHANGES.md", "CHANGES",
        "NEWS.md", "NEWS", "HISTORY.md", "HISTORY",
    ]
    # Try both common default branches
    for branch in ("HEAD", "main", "master"):
        urls = [f"https://raw.githubusercontent.com/{repo}/{branch}/{name}"
                for name in candidates]
        pages = _fetch_parallel(urls, timeout=8)
        for name, url in zip(candidates, urls):
            body = pages.get(url)
            if body and len(body) > 50 and "404" not in body[:20]:
                result = _scrape_github_raw_changelog(body)
                if result and result.get("versions"):
                    result["source"] = f"GitHub {name} — {repo}"
                    return result
        # If we found the file on this branch (even with no parseable versions),
        # no need to try other branches
        if any(pages.values()):
            break
    return None


def _github_releases(repo: str, _pkg_name: str) -> Optional[dict]:
    data = http_get_json(f"https://api.github.com/repos/{repo}/releases?per_page=8")
    if data and isinstance(data, list) and data:
        versions = []
        for rel in data[:6]:
            ver  = (rel.get("tag_name") or "").lstrip("vV")
            date = (rel.get("published_at") or "")[:10]
            body = rel.get("body") or ""
            versions.append({"version": ver, "date": date,
                             "changes": _parse_md_changelog(body)[:10] or [f"Release {ver}"]})
        if versions:
            return {"versions": versions, "source": f"GitHub Releases — {repo}"}

    # Suggestion #2: no Releases — try CHANGELOG.md/NEWS file in repo root
    changelog_result = _fetch_github_changelog_file(repo)
    if changelog_result:
        return changelog_result

    # Last resort: bare tags with no content
    data = http_get_json(f"https://api.github.com/repos/{repo}/tags?per_page=8")
    if data and isinstance(data, list) and data:
        return {"versions": [{"version": t.get("name","").lstrip("v"),
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
    Normalise a tag name into a readable version string. Handles:
    - Simple semver: "v3.2.1" -> "3.2.1"
    - GNOME-style: "GNOME_COLOR_MANAGER_3_11_90" -> "3.11.90"
    - Release prefixes: "release-2.5" -> "2.5"
    """
    t = tag_name.lstrip("vV")
    # Remove common release- prefix
    t = re.sub(r'^release[-_]', '', t, flags=re.I)
    # GNOME-style: PROJECT_NAME_X_Y_Z -> trailing numeric run with dots
    m = re.search(r'((?:\d+_)+\d+)$', t)
    if m:
        return m.group(1).replace("_", ".")
    return t


# ─── GitLab hosts with known API blocking but git access working ──────────────
_GIT_FIRST_HOSTS = {"invent.kde.org", "source.kde.org"}


def _gitlab_releases(host: str, repo: str, _pkg_name: str) -> Optional[dict]:
    """
    Priority:
    1. For known bot-protected hosts, try git fallback first (API blocked).
    2. GitLab Releases API (/releases) — formal Release objects.
    3. Tags API (/repository/tags) with real changelog text.
    4. A NEWS/CHANGELOG file in the repo root.
    5. Commit log — last resort.
    """
    # For known problematic hosts, try git access before API calls
    if host in _GIT_FIRST_HOSTS:
        git_result = _gitlab_git_fallback(host, repo, _pkg_name)
        if git_result and git_result.get("versions"):
            return git_result
    
    encoded = urllib.parse.quote(repo, safe="")

    data = http_get_json(f"https://{host}/api/v4/projects/{encoded}/releases?per_page=6")
    if data and isinstance(data, list) and data:
        versions = []
        for rel in data[:6]:
            ver  = _extract_version_from_tag(rel.get("tag_name") or "")
            date = (rel.get("released_at") or rel.get("created_at") or "")[:10]
            desc = rel.get("description") or ""
            changes = _parse_md_changelog(desc)
            versions.append({"version": ver, "date": date,
                             "changes": changes[:10] or [desc[:120].replace("\n"," ")] or [f"Release {ver}"]})
        # Check if we have meaningful content before returning
        if versions and any(_is_meaningful_changelog(v.get("changes", [])) for v in versions):
            return {"versions": versions, "source": f"GitLab Releases — {host}/{repo}"}

    # Fallback: tags with real commit/annotation messages
    tags = http_get_json(f"https://{host}/api/v4/projects/{encoded}/repository/tags?per_page=8")
    if tags and isinstance(tags, list) and tags:
        versions = []
        for tag in tags[:6]:
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
                versions.append({
                    "version": ver,
                    "date": ((tag.get("commit") or {}).get("created_at") or "")[:10],
                    "changes": changes[:8],
                })
        # Only return if we have meaningful versions (not just version numbers with no changes)
        if versions and any(_is_meaningful_changelog(v.get("changes", [])) for v in versions):
            return {"versions": versions, "source": f"GitLab tags — {host}/{repo}"}

    # Fallback: NEWS/CHANGELOG file in the repo root (very common for
    # GNOME and other C/Meson projects that skip GitLab Releases entirely)
    news = _fetch_gitlab_news_file(host, repo)
    if news:
        return news

    # Some GitLab instances block direct raw-file fetches, but git access may
    # still work. Try reading NEWS/CHANGELOG via git if raw HTTP fetch fails.
    news_git = _gitlab_fetch_news_file_via_git(host, repo)
    if news_git:
        return news_git

    git_fallback = _gitlab_git_fallback(host, repo, _pkg_name)
    if git_fallback:
        return git_fallback

    # Last resort: raw commit log, filtered the same way the Arch GitLab
    # fallback is — drops PGP noise and non-meaningful housekeeping commits.
    commits = http_get_json(
        f"https://{host}/api/v4/projects/{encoded}/repository/commits?per_page=15")
    if commits and isinstance(commits, list) and commits:
        versions, seen = [], set()
        for c in commits:
            title = c.get("title", "")
            date  = (c.get("committed_date") or "")[:10]
            if _is_pgp_garbage(title) or not title.strip():
                continue
            if not _is_meaningful_commit(title):
                continue
            m   = re.search(r"(\d+[\.\d]+(?:-\d+)?)", title)
            ver = m.group(1) if m else date
            if ver not in seen:
                seen.add(ver)
                versions.append({"version": ver, "date": date, "changes": [title]})
            if len(versions) >= 6:
                break
        if versions:
            return {"versions": versions, "source": f"GitLab commits — {host}/{repo}"}

    return None


def _fetch_gitlab_news_file(host: str, repo: str) -> Optional[dict]:
    """Try NEWS/CHANGELOG files via GitLab's raw-file endpoint, across
    common default branch names and filenames."""
    encoded = urllib.parse.quote(repo, safe="")
    filenames = ["NEWS", "CHANGELOG", "NEWS.md", "CHANGELOG.md",
                 "CHANGES", "CHANGES.md", "HISTORY", "HISTORY.md"]
    # Try common branch patterns; include develop/dev for active projects
    branches  = ["main", "master", "develop", "dev", "HEAD", "release"]
    urls = [
        f"https://{host}/{repo}/-/raw/{branch}/{fname}"
        for branch in branches for fname in filenames
    ]
    pages = _fetch_parallel(urls, timeout=10)
    for url in urls:
        body = pages.get(url)
        if body and len(body) > 50:
            low = body.lower()
            if "<html" in low or _is_bot_protection_page(body):
                continue
            result = _scrape_text_file(body) if "\n##" not in body[:2000] \
                     else _scrape_github_raw_changelog(body)
            if result and result.get("versions"):
                fname = url.rsplit("/", 1)[-1]
                result["source"] = f"GitLab {fname} — {host}/{repo}"
                return result
    return None


def _gitlab_fetch_news_file_via_git(host: str, repo: str) -> Optional[dict]:
    if not cmd_exists("git"):
        return None
    repo_url = f"https://{host}/{repo}.git"
    filenames = ["NEWS", "CHANGELOG", "NEWS.md", "CHANGELOG.md",
                 "CHANGES", "CHANGES.md", "HISTORY", "HISTORY.md"]
    branches = ["main", "master", "develop", "dev", "release", "HEAD"]

    with tempfile.TemporaryDirectory(prefix="pakchan-git-") as tmpdir:
        if run(["git", "init", "--bare", tmpdir])[2] != 0:
            return None
        git = ["git", "-C", tmpdir]
        if run(git + ["remote", "add", "origin", repo_url])[2] != 0:
            return None

        for branch in branches:
            fetch_rc = run(git + ["fetch", "--quiet", "--depth", "1",
                                  "origin", f"refs/heads/{branch}:refs/remotes/origin/{branch}"])[2]
            if fetch_rc != 0:
                continue
            for fname in filenames:
                show_rc = run(git + ["show", f"refs/remotes/origin/{branch}:{fname}"])
                if show_rc[2] != 0 or not show_rc[0]:
                    continue
                body = show_rc[0]
                result = _scrape_text_file(body) if "\n##" not in body[:2000] \
                         else _scrape_github_raw_changelog(body)
                if result and result.get("versions"):
                    result["source"] = f"GitLab {fname} via git — {host}/{repo}"
                    return result
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

    def version_key(tag_name: str):
        t = tag_name.lstrip("vV")
        parts = re.split(r"[._-]", t)
        key: list[tuple[int, object]] = []
        for part in parts:
            if part.isdigit():
                # Numeric parts sort before alpha parts
                key.append((0, int(part)))
            else:
                key.append((1, part.lower()))
        return tuple(key)

    tags.sort(key=lambda tr: version_key(tr[0]), reverse=True)
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
        r = _scrape_custom(pkg_name, entry)
        if r and r.get("versions"): return r
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
        r = _gitlab_releases(gl.group(1), gl.group(2).removesuffix(".git"), pkg_name)
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
            r = _gitlab_releases(found[1], found[2], pkg_name)
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
    Always check custom and release_pages mappings BEFORE any other source.

    For "release_pages" entries, scraping arbitrary third-party sites
    proved too unreliable across different HTML structures — instead we
    show a direct, clickable link to the official changelog page. This is
    simple and always correct, even if it requires one extra click.

    For "custom" entries (mantisbt, text_file, github_raw), the parser
    is still attempted since these are simpler, well-defined formats.
    """
    name = pkg.name.lower()

    # Custom parser (mantisbt, text_file, github_raw, …)
    if name in KNOWN_CUSTOM:
        entry = KNOWN_CUSTOM[name]
        url   = entry.get("url", "")
        r     = _scrape_custom(pkg.name, entry)
        if r and r.get("versions"):
            return r
        # Mapping exists but scraping failed — return URL fallback, not None
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"Custom ({entry.get('parser', '')}) — {url}",
        }

    # Dedicated release page — show a direct link, no scraping attempted.
    if name in KNOWN_RELEASE_PAGES:
        url = KNOWN_RELEASE_PAGES[name]
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"Release page — {url}",
            "_link_only": True,
            "_link_url": url,
        }

    # Known GitLab repo mapping from mappings.json should beat local AppStream.
    if name in KNOWN_GITLAB_REPOS:
        host, repo = KNOWN_GITLAB_REPOS[name]
        r = _gitlab_releases(host, repo, pkg.name)
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

    if name in KNOWN_GITHUB_REPOS:
        repo = KNOWN_GITHUB_REPOS[name]
        r = _github_releases(repo, pkg.name)
        if r and r.get("versions"):
            return r
        url = f"https://github.com/{repo}/releases"
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {url} for details."]}],
            "source": f"GitHub repo mapping — {repo}",
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


# Suggestion #5: sentiment-based commit filtering — separates noisy VCS
# housekeeping commits ("Merge branch", "Bump version", "chore: ...")
# from genuinely user-facing changes, when falling back to raw commit logs.
_NOISE_COMMIT_PATTERNS = [
    r'^Merge (branch|pull request)',
    r'^Bump version',
    r'^Update (changelog|readme|license)',
    r'^\d+\.\d+\.\d+$',          # bare version number
    r'^[Ww]ip\b',
    r'^fixup!',
    r'^squash!',
    r'^[Tt]ypo',
    r'^[Cc]leanup',
    r'^[Rr]efactor',
    r'^(chore|ci|docs|style|test)(\(.*\))?:',   # conventional commits, non feat/fix
    r'^[a-f0-9]{7,}$',           # bare commit hash
]
_USEFUL_COMMIT_PATTERNS = [
    r'^(feat|fix|perf|security)(\(.*\))?:',     # conventional commits
    r'\b(add|fix|remove|improve|update|change|implement|support|allow)\b',
    r'\b(crash|bug|error|issue|problem|vulnerability|CVE)\b',
    r'\b(feature|option|setting|preference|config)\b',
]


def _is_meaningful_commit(message: str) -> bool:
    """Check if a commit message describes a user-visible change."""
    message = message.strip()
    if not message:
        return False
    for pattern in _NOISE_COMMIT_PATTERNS:
        if re.match(pattern, message):
            return False
    for pattern in _USEFUL_COMMIT_PATTERNS:
        if re.search(pattern, message, re.IGNORECASE):
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

    # 3. Fetch URL from pacman -Si if not already set
    if not pkg.url:
        out, _, _ = run(["pacman", "-Si", pkg.name])
        for line in out.splitlines():
            if line.strip().startswith("URL") and ":" in line:
                pkg.url = line.partition(":")[2].strip()
                break
    _dbg(f"[3] package URL: {pkg.url or '(none)'}")

    # 4. Known GitLab repo
    name = pkg.name.lower()
    if name in KNOWN_GITLAB_REPOS:
        host, repo = KNOWN_GITLAB_REPOS[name]
        r = _gitlab_releases(host, repo, pkg.name)
        if r and r.get("versions"):
            _dbg(f"[4] mappings.json gitlab entry: hit ({host}/{repo})")
            return r
        _dbg(f"[4] mappings.json gitlab entry {host}/{repo}: no usable data")
    else:
        _dbg("[4] mappings.json gitlab entry: none")

    # 5. Known GitHub repo
    if name in KNOWN_GITHUB_REPOS:
        r = _github_releases(KNOWN_GITHUB_REPOS[name], pkg.name)
        if r and r.get("versions"):
            _dbg(f"[5] mappings.json github entry: hit ({KNOWN_GITHUB_REPOS[name]})")
            return r
        _dbg(f"[5] mappings.json github entry {KNOWN_GITHUB_REPOS[name]}: no usable data")
    else:
        _dbg("[5] mappings.json github entry: none")

    # 6. Direct GitHub/GitLab URL in package metadata
    if pkg.url:
        gh = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", pkg.url)
        if gh:
            repo = gh.group(1).rstrip("/").removesuffix(".git")
            r = _github_releases(repo, pkg.name)
            if r and r.get("versions"):
                _dbg(f"[6] direct GitHub URL: hit ({repo})")
                return r
            _dbg(f"[6] direct GitHub URL {repo}: no usable data")
        gl = re.search(r"(gitlab\.[^/\s]+)/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", pkg.url)
        if gl:
            host, repo = gl.group(1), gl.group(2).removesuffix(".git")
            r = _gitlab_releases(host, repo, pkg.name)
            if r and r.get("versions"):
                _dbg(f"[6] direct GitLab URL: hit ({host}/{repo})")
                return r
            _dbg(f"[6] direct GitLab URL {host}/{repo}: no usable data")
        if not gh and not gl:
            _dbg("[6] package URL is not a direct GitHub/GitLab link")
    else:
        _dbg("[6] no package URL to check")

    # 7. Homepage scraping for an indirect GitHub/GitLab link (e.g.
    #    apps.gnome.org/Calendar, which links out to gitlab.gnome.org).
    #    This MUST run before the Arch packaging fallback below — Arch's
    #    packaging repo only ever contains packaging metadata (version
    #    bumps, rebuild notes), never the upstream project's real
    #    changelog, so it should be a last resort, not a shortcut that
    #    pre-empts finding the real upstream source.
    fallback_link = None
    if pkg.url:
        found = _find_repo_via_homepage(pkg.url, pkg.name)
        if found:
            if found[0] == "github":
                _dbg(f"[7] homepage scan found GitHub repo: {found[1]}")
                r = _github_releases(found[1], pkg.name)
                if r and r.get("versions"):
                    _dbg("[7] homepage-discovered repo: hit")
                    return r
                fallback_link = f"https://github.com/{found[1]}/releases"
            else:
                _dbg(f"[7] homepage scan found GitLab repo: {found[1]}/{found[2]}")
                r = _gitlab_releases(found[1], found[2], pkg.name)
                if r and r.get("versions"):
                    _dbg("[7] homepage-discovered repo: hit")
                    return r
                fallback_link = f"https://{found[1]}/{found[2]}/-/releases"
            _dbg("[7] homepage-discovered repo: no usable data")
        else:
            _dbg("[7] homepage scan: no repo link found (or rejected by plausibility check)")
    else:
        _dbg("[7] no package URL to scan")

    if fallback_link:
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {fallback_link} for details."]}],
            "source": "Upstream repo link",
            "_link_only": True,
            "_link_url": fallback_link,
        }

    # 8. Arch packaging GitLab — absolute last resort. Filters PGP noise
    # and non-meaningful housekeeping commits, but this only ever reflects
    # *packaging* changes (version bumps, rebuilds), not the real upstream
    # changelog, so every prior step is strictly more useful when it works.
    encoded  = urllib.parse.quote(f"archlinux/packaging/packages/{pkg.name}", safe="")
    base_url = f"https://gitlab.archlinux.org/api/v4/projects/{encoded}"

    tags = http_get_json(f"{base_url}/repository/tags?per_page=6")
    if tags and isinstance(tags, list):
        versions = []
        for tag in tags[:5]:
            ver = (tag.get("name") or "").lstrip("v")
            msg = tag.get("message") or (tag.get("commit") or {}).get("message", "")
            changes = [
                l.strip("- ").strip()
                for l in msg.splitlines()
                if l.strip()
                and not l.strip().startswith("#")
                and not _is_pgp_garbage(l)
                and len(l.strip()) < 200
            ]
            if changes:
                versions.append({
                    "version": ver,
                    "date": ((tag.get("commit") or {}).get("created_at") or "")[:10],
                    "changes": changes[:6],
                })
        if versions:
            _dbg("[8] Arch packaging GitLab tags: hit")
            return {"versions": versions,
                    "source": f"Arch Linux GitLab — packaging/packages/{pkg.name}"}
    _dbg("[8] Arch packaging GitLab tags: no usable data")

    commits = http_get_json(f"{base_url}/repository/commits?per_page=15")
    if commits and isinstance(commits, list):
        versions, seen = [], set()
        for c in commits:
            title = c.get("title", "")
            date  = (c.get("committed_date") or "")[:10]
            # Filter PGP noise AND non-meaningful housekeeping commits
            if _is_pgp_garbage(title) or not title.strip():
                continue
            if not _is_meaningful_commit(title):
                continue
            m   = re.search(r"(\d+[\.\d]+-\d+)", title)
            ver = m.group(1) if m else date
            if ver not in seen:
                seen.add(ver)
                versions.append({"version": ver, "date": date, "changes": [title]})
            if len(versions) >= 5:
                break
        if versions:
            _dbg("[8] Arch packaging GitLab commits: hit")
            return {"versions": versions,
                    "source": f"Arch Linux GitLab — packaging/packages/{pkg.name}"}
    _dbg("[8] Arch packaging GitLab commits: no usable data — giving up")

    return {"versions": [{"version": pkg.version, "date": "",
                          "changes": ["Changelog not found."]}], "source": "unavailable"}


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

    # 3. Fetch URL from AUR RPC if not set
    if not pkg.url:
        data = http_get_json(
            f"https://aur.archlinux.org/rpc/v5/info/{urllib.parse.quote(pkg.name)}")
        if data and data.get("results"):
            pkg.url = data["results"][0].get("URL", "")
    _dbg(f"[3] package URL: {pkg.url or '(none)'}")

    # 4. Known mappings (GitHub/GitLab)
    name = pkg.name.lower()
    if name in KNOWN_GITLAB_REPOS:
        host, repo = KNOWN_GITLAB_REPOS[name]
        r = _gitlab_releases(host, repo, pkg.name)
        if r and r.get("versions"):
            _dbg(f"[4] mappings.json gitlab entry: hit ({host}/{repo})")
            return r
        _dbg(f"[4] mappings.json gitlab entry {host}/{repo}: no usable data")
    else:
        _dbg("[4] mappings.json gitlab entry: none")

    if name in KNOWN_GITHUB_REPOS:
        r = _github_releases(KNOWN_GITHUB_REPOS[name], pkg.name)
        if r and r.get("versions"):
            _dbg(f"[4] mappings.json github entry: hit ({KNOWN_GITHUB_REPOS[name]})")
            return r
        _dbg(f"[4] mappings.json github entry {KNOWN_GITHUB_REPOS[name]}: no usable data")
    else:
        _dbg("[4] mappings.json github entry: none")

    # 5. Direct GitHub/GitLab URL
    if pkg.url:
        gh = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", pkg.url)
        if gh:
            repo = gh.group(1).rstrip("/").removesuffix(".git")
            r = _github_releases(repo, pkg.name)
            if r and r.get("versions"):
                _dbg(f"[5] direct GitHub URL: hit ({repo})")
                return r
            _dbg(f"[5] direct GitHub URL {repo}: no usable data")
        gl = re.search(r"(gitlab\.[^/\s]+)/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", pkg.url)
        if gl:
            host, repo = gl.group(1), gl.group(2).removesuffix(".git")
            r = _gitlab_releases(host, repo, pkg.name)
            if r and r.get("versions"):
                _dbg(f"[5] direct GitLab URL: hit ({host}/{repo})")
                return r
            _dbg(f"[5] direct GitLab URL {host}/{repo}: no usable data")
    else:
        _dbg("[5] no package URL to check")

    # 6. Homepage scraping (GitHub or GitLab) — try this BEFORE the AUR
    # cgit fallback below. AUR cgit only ever shows PKGBUILD packaging
    # commits, never the upstream project's real changelog, so it should
    # be a last resort rather than something that pre-empts finding the
    # real upstream source via the package's homepage.
    fallback_link = None
    if pkg.url:
        found = _find_repo_via_homepage(pkg.url, pkg.name)
        if found:
            if found[0] == "github":
                _dbg(f"[6] homepage scan found GitHub repo: {found[1]}")
                r = _github_releases(found[1], pkg.name)
                if r and r.get("versions"):
                    _dbg("[6] homepage-discovered repo: hit")
                    return r
                fallback_link = f"https://github.com/{found[1]}/releases"
            else:
                _dbg(f"[6] homepage scan found GitLab repo: {found[1]}/{found[2]}")
                r = _gitlab_releases(found[1], found[2], pkg.name)
                if r and r.get("versions"):
                    _dbg("[6] homepage-discovered repo: hit")
                    return r
                fallback_link = f"https://{found[1]}/{found[2]}/-/releases"
            _dbg("[6] homepage-discovered repo: no usable data")
        else:
            _dbg("[6] homepage scan: no repo link found (or rejected by plausibility check)")
    else:
        _dbg("[6] no package URL to scan")

    if fallback_link:
        return {
            "versions": [{"version": pkg.version, "date": "",
                          "changes": [f"See {fallback_link} for details."]}],
            "source": "Upstream repo link",
            "_link_only": True,
            "_link_url": fallback_link,
        }

    # 7. AUR cgit fallback (PKGBUILD commit history) — absolute last resort
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
        _dbg("[7] AUR cgit log: hit")
    else:
        _dbg("[7] AUR cgit log: no usable data — giving up")
        versions = [{"version": pkg.version, "date": "",
                     "changes": ["No commit history found on AUR."]}]
    return {"versions": versions, "source": "AUR cgit log"}


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

SORT_OPTIONS = ["Relevance", "A → Z", "Z → A", "Size ↓", "Updates first"]


class PakchanApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.example.Pakchan")
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
        self.current_tab    = "info"
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
        """
        p.load_from_bytes(GLib.Bytes.new(css))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), p, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    # ── UI construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        self._css()
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
        self.paned.set_start_child(left)
        self.paned.set_resize_start_child(True)
        self.paned.set_end_child(self._build_detail_panel())
        self.paned.set_resize_end_child(False)
        self.paned.set_position(780)

        self.stack = Gtk.Stack()
        self.stack.set_vexpand(True)
        self.stack.add_named(self.status_page, "loading")
        self.stack.add_named(self.paned,       "main")
        root.append(self.stack)

        self.footer = Gtk.Label(label="Ready")
        self.footer.set_xalign(0)
        self.footer.add_css_class("dim-label")
        self.footer.set_margin_start(12)
        self.footer.set_margin_top(3)
        self.footer.set_margin_bottom(5)
        root.append(self.footer)

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
        box.append(sc)

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

        self.d_name = Gtk.Label()
        self.d_name.set_markup("<b>Select a package</b>")
        self.d_name.set_xalign(0)
        self.d_name.set_margin_start(12); self.d_name.set_margin_end(12)
        self.d_name.set_margin_top(10);   self.d_name.set_margin_bottom(2)
        self.d_name.set_ellipsize(Pango.EllipsizeMode.END)
        box.append(self.d_name)

        self.d_desc = Gtk.Label(label="Click a package to view details.")
        self.d_desc.set_xalign(0)
        self.d_desc.set_margin_start(12); self.d_desc.set_margin_end(12)
        self.d_desc.set_margin_bottom(8)
        self.d_desc.add_css_class("dim-label")
        self.d_desc.set_wrap(True); self.d_desc.set_max_width_chars(38)
        box.append(self.d_desc)
        box.append(Gtk.Separator())

        self.tabs = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.tabs.set_homogeneous(True)
        self._tab_btns: dict[str, Gtk.ToggleButton] = {}
        for key, label in [("info","Info"),("changelog","Changelog"),("files","Files")]:
            btn = Gtk.ToggleButton(label=label)
            btn.add_css_class("flat")
            btn.connect("clicked", self._on_tab, key)
            self._tab_btns[key] = btn
            self.tabs.append(btn)
        self._tab_btns["info"].set_active(True)
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
        return row

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
            def _sz(p):
                raw = p.installed_size
                mul = {"GiB":1e9,"MiB":1e6,"KiB":1e3,"B":1}.get(raw.split()[-1] if raw else "",1)
                try: return float(raw.split()[0]) * mul
                except: return 0
            return sorted(pool, key=_sz, reverse=True)
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

        n       = len(pool)
        n_upd   = sum(1 for p in pool if p.has_update)
        checked = sum(1 for p in self.all_packages if p.checked)
        parts   = [f"{n} package{'s' if n!=1 else ''}"]
        if flt != "updates" and n_upd:
            parts.append(f"{n_upd} with updates")
        if q:
            parts.append("search results")
        if checked:
            parts.append(f"{checked} selected")
        self.count_lbl.set_text(" · ".join(parts))

        self._update_footer()

        total = sum(1 for p in self.all_packages if p.checked)
        self.apply_btn.set_sensitive(total > 0)
        self.apply_btn.set_label(f"Apply ({total})")

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
        self._update_footer()

    def _on_row_selected(self, lb, row):
        if row is None: return
        pkg = row.pkg
        self.selected_pkg = pkg
        self.d_name.set_markup(f"<b>{GLib.markup_escape_text(pkg.name)}</b>")
        self.d_desc.set_text(pkg.description or "Loading…")
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
            v.set_max_width_chars(32); v.set_selectable(True)
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
            rb.connect("clicked", lambda _: self._retry_cl(pkg))
            self.d_box.append(rb)
            self._append_debug_expander(pkg)
            return

        # If this package only has a release_pages mapping (no scraping
        # attempted), show a direct clickable link at the top and stop —
        # this is the simple, always-correct fallback requested by the user.
        if pkg.changelog.get("_link_only"):
            url = pkg.changelog.get("_link_url", "")
            link_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
            prefix = Gtk.Label(label="See")
            prefix.set_xalign(0)
            link_row.append(prefix)
            link_btn = Gtk.LinkButton(uri=url)
            link_btn.set_label(url)
            inner = link_btn.get_child()
            if inner:
                inner.set_ellipsize(Pango.EllipsizeMode.END)
                inner.set_max_width_chars(34)
            link_row.append(link_btn)
            suffix = Gtk.Label(label="for details.")
            suffix.set_xalign(0)
            link_row.append(suffix)
            link_row.set_margin_bottom(4)
            self.d_box.append(link_row)
            self._append_debug_expander(pkg)
            return

        # Source label + cache indicator
        src_text = f"Source: {pkg.changelog.get('source', '')}"
        if pkg.changelog.get("_from_cache"):
            src_text += "  [cached]"
        src = Gtk.Label(label=src_text)
        src.add_css_class("dim-label"); src.set_xalign(0); src.set_margin_bottom(2)
        self.d_box.append(src)

        # Fix #6: stale warning
        if pkg.changelog.get("_stale"):
            stale_lbl = Gtk.Label(label="⚠ Cached data may be outdated (>7 days)")
            stale_lbl.add_css_class("stale-warn"); stale_lbl.set_xalign(0)
            self.d_box.append(stale_lbl)

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
                cl.set_max_width_chars(38); cl.set_selectable(True)
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

    def _retry_cl(self, pkg: Package):
        pkg.changelog = None
        if self.selected_pkg and self.selected_pkg.name == pkg.name:
            self._render_detail()

    def _force_refresh_cl(self, pkg: Package):
        key = pkg.cl_key
        if key in _CL_DB:
            del _CL_DB[key]
            _cl_db_flush(force=True)
        pkg.changelog = None
        if self.selected_pkg and self.selected_pkg.name == pkg.name:
            self._render_detail()

    def _render_files(self, pkg: Package):
        """Fix #20: walk real Flatpak deploy directory."""
        if pkg.repo == "pacman":
            out, _, rc = run(["pacman", "-Ql", pkg.name])
            if rc == 0:
                for line in out.splitlines()[:80]:
                    parts = line.split(None, 1)
                    path  = parts[1] if len(parts) > 1 else line
                    l = Gtk.Label(label=path)
                    l.add_css_class("mono"); l.set_xalign(0); l.set_selectable(True)
                    self.d_box.append(l)
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

    def _apply_updates(self, btn):
        sel = [p for p in self.all_packages if p.checked]
        if not sel: return
        dlg = Gtk.Dialog()
        dlg.set_transient_for(self)
        dlg.set_title("Apply updates?")
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        lbl = Gtk.Label(label=f"Update {len(sel)} package(s). A terminal will open.")
        lbl.set_wrap(True)
        content.append(lbl)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_margin_top(12)
        button_row.set_halign(Gtk.Align.END)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.add_css_class("secondary")
        cancel_btn.connect("clicked", lambda _btn: dlg.destroy())
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", lambda _btn: self._do_apply(dlg, Gtk.ResponseType.APPLY, sel))
        button_row.append(cancel_btn)
        button_row.append(apply_btn)
        content.append(button_row)

        dlg.set_child(content)
        dlg.present()

    def _do_apply(self, dlg, response, sel: list):
        if response != Gtk.ResponseType.APPLY:
            dlg.destroy()
            return
        dlg.destroy()
        # Fix #7: shlex.quote all package names — prevents shell injection
        pac = [shlex.quote(p.name) for p in sel if p.repo == "pacman"]
        aur = [shlex.quote(p.name) for p in sel if p.repo == "aur"]
        flt = [shlex.quote(p.name) for p in sel if p.repo == "flatpak"]
        snp = [shlex.quote(p.name) for p in sel if p.repo == "snap"]
        cmds = []
        if pac: cmds.append(f"sudo pacman -S --noconfirm {' '.join(pac)}")
        if aur:
            h = "yay" if cmd_exists("yay") else "paru"
            cmds.append(f"{h} -S --noconfirm {' '.join(aur)}")
        if flt: cmds.append(f"flatpak update -y {' '.join(flt)}")
        if snp: cmds.append(f"sudo snap refresh {' '.join(snp)}")
        full = " && ".join(cmds)
        for term in ["kgx", "gnome-terminal", "konsole", "xterm", "alacritty"]:
            if not cmd_exists(term):
                continue
            runner = f'{full}; echo; echo Done. Press Enter to close.; read -r; exit'
            if term == "gnome-terminal":
                os.system(f'{term} -- bash -lc "{runner}" &')
            elif term in {"konsole", "xterm", "alacritty"}:
                os.system(f'{term} -e bash -lc "{runner}" &')
            else:
                os.system(f'{term} -- bash -lc "{runner}" &')
            return
        self.footer.set_text(f"Run manually: {full}")


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
  