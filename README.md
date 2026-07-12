# 📦 Pakchan

Pakchan is a GTK4 package manager for Manjaro/Arch Linux that shows real changelogs for Pacman, AUR, Flatpak, and Snap.
It also includes a community-maintained changelog source database for Pakchan itself.

[![GitHub Pages](https://img.shields.io/badge/Web%20Form-Live-brightgreen)](https://dodog.github.io/pakchan/web/)
[![Packages](https://img.shields.io/badge/Packages-18-blue)](https://dodog.github.io/pakchan/data/mappings.json)
[![Contributions Welcome](https://img.shields.io/badge/Contributions-Welcome-brightgreen.svg)](https://github.com/dodog/pakchan/issues)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What is Pakchan?

Pakchan is a desktop application for Manjaro/Arch that fetches **real changelogs** for package updates with its own community-driven changelog source database, so changelog sources can be discovered reliably.

## Why it exists

Most package managers tell you **an update exists**, but not **what changed**.
Pakchan solves this by locating and showing changelogs from actual upstream sources, including git tags, release notes pages, AUR commit history, and Flathub metadata.

## Features

- Changelog support for `pacman`, `aur`, `flatpak`, and `snap`
- No API key required for supported sources
- Community-maintained changelog mapping database
- Web-based submission form for new mappings

## Install dependencies

```bash
sudo pacman -S python-gobject gtk4 libadwaita pacman-contrib
```

### Optional: AUR support

Install `yay` or `paru` to enable AUR update detection:

```bash
# yay
git clone https://aur.archlinux.org/yay.git && cd yay && makepkg -si

# or paru
git clone https://aur.archlinux.org/paru.git && cd paru && makepkg -si
```

### Optional: Flatpak support

```bash
sudo pacman -S flatpak
flatpak remote-add --if-not-exists flathub https://dl.flathub.org/repo/flathub.flatpakrepo
```

---

## Run Pakchan

```bash
python3 pakchan.py
```

---

## How changelogs work

Pakchan resolves changelogs using a layered, source-aware process rather than relying on a single package metadata field.

### General strategy

1. Check `data/mappings.json` first for a custom source mapping.
2. Use local AppStream metadata for desktop apps when available.
3. Resolve known GitHub/GitLab repos from mappings or package URLs.
4. Scan the package homepage for upstream repo links.
5. Fall back to packaging or commit history only when upstream changelog sources are unavailable.

### Pacman packages

- Starts with `data/mappings.json` and local AppStream metadata.
- Reads package metadata from `pacman -Si` to get the upstream homepage.
- Uses known GitHub/GitLab repo mappings and direct GitHub/GitLab URLs.
- Scrapes the homepage to find the upstream repository if needed.
- As a last resort, fetches Arch Linux packaging repo tags/commits from `gitlab.archlinux.org`.

This means Pakchan prefers real upstream release notes, and only uses Arch packaging history when no better source is found.

### AUR packages

- Starts with `data/mappings.json` and local AppStream metadata.
- Fetches the package homepage URL from the AUR RPC API.
- Tries known GitHub/GitLab mappings, direct repo links, and homepage-based repo discovery.
- If no upstream changelog can be resolved, falls back to the AUR cgit PKGBUILD commit log.

AUR fallback data is treated as packaging commit history, not the upstream project's official changelog.

### Flatpak / Flathub

- Starts with `data/mappings.json`.
- Queries the Flathub REST API for release metadata and notes.
- If the API has no release notes, it parses Flathub AppStream XML from the CDN.
- If Flathub still does not provide usable notes, it tries the package homepage for upstream GitHub/GitLab release data.

### Snap packages

- Starts with `data/mappings.json`.
- Queries the Snap Store API for release/version metadata.
- If the Snap Store data is insufficient, it tries the package homepage for upstream release notes.

### Custom parsers

For packages with edge-case sources, Pakchan can use custom parser types like:

- `mozilla` for Mozilla-style release pages
- `krita` for Krita release post pages
- `mantisbt` for MantisBT changelog pages
- `filezilla` for FileZilla news feeds
- `text_file` for plain text changelog files
- `github_raw` for raw GitHub changelog files like `CHANGELOG.md`

These custom entries are defined in `data/mappings.json` and let Pakchan interpret release notes that standard GitHub/GitLab parsing would miss.
                                                                                                                                                  

---

## Create a desktop launcher (optional)

```bash
cat > ~/.local/share/applications/pakchan.desktop << 'DESK'
[Desktop Entry]
Name=Pakchan
Comment=Package manager with changelogs
Exec=python3 /path/to/pakchan.py
Icon=system-software-update
Terminal=false
Type=Application
Categories=System;PackageManager;
DESK
```

---

## Troubleshooting

**No updates show up?**
- Pacman: ensure `pacman-contrib` is installed (`checkupdates` command)
- AUR: install `yay` or `paru`
- Flatpak: install `flatpak` and add the Flathub remote

**Changelog shows "not available"?**
- Some smaller AUR packages have minimal git history
- Flatpak apps not on Flathub won't have AppStream data
- Network access is required to fetch changelog data

**App won't launch?**

```bash
sudo pacman -S python-gobject gtk4 libadwaita
```

---

## Package mapping database

Pakchan stores changelog source mappings in `data/mappings.json`, which is consumed by the app to resolve the correct source for each package.

### Add a package mapping

- Use the [web form](https://dodog.github.io/pakchan/web/) to submit a package and changelog source.
- Or open a GitHub issue: [Create an issue](https://github.com/dodog/pakchan/issues/new?labels=submission&template=add-package.yml).

### Supported source types

| Type | Description | Example |
|------|-------------|---------|
| `github_releases` | GitHub Releases API | `videolan/vlc` |
| `gitlab_releases` | GitLab Releases API | `gitlab.gnome.org/GNOME/gimp` |
| `html_page` | Any HTML release notes page | `https://krita.org/en/release-notes/` |
| `mozilla_releases` | Mozilla-style release notes | Firefox, Thunderbird |
| `text_file` | Plain text changelog file | `https://example.com/CHANGELOG.txt` |


## Repository structure

```
pakchan.py            # GTK4 application entry point
data/mappings.json    # Changelog source database
web/index.html        # Submission form for new mappings
scripts/              # Helper scripts
.github/              # GitHub actions and workflows
```


## License

MIT — see [LICENSE](LICENSE)
