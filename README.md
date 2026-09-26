
[![GitHub Pages](https://img.shields.io/badge/Web%20Form-Live-brightgreen)](https://dodog.github.io/pakchan/web/)
[![Packages](https://img.shields.io/badge/Packages-171-blue)](https://dodog.github.io/pakchan/data/mappings.json)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)


<picture>
  <!-- Use this image for dark mode -->
  <source media="(prefers-color-scheme: dark)" srcset="/web/img/pakchan-logo-wide_dark.png">
  <!-- Fallback image for light mode and other clients -->
  <img src="/web/img/pakchan-logo-wide.png" alt="PAKCHAN">
</picture>

#

**Pakchan** is a GTK4 package manager for Manjaro and Arch Linux based system that fetches **real changelogs** for package updates (Pacman, AUR, Flatpak, Snap). It also includes a community-driven changelog source database.


## ❓ Why I created Pakchan

Most package managers tell you **an update exists**, but not **what changed**. I've always wondered what's new in the update.
Pakchan solves this by locating and showing changelogs from actual upstream sources, including git tags, release notes pages, AUR commit history, and Flathub metadata.

## 🚀 Features

- Changelog support for `pacman`, `aur`, `flatpak`, and `snap`
- No API key required for supported sources
- Community-maintained changelog mapping database
- Web-based submission form for new mappings

## 🛠️ Install dependencies

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

---

## ⚙️ How changelogs work

Pakchan resolves changelogs using a layered, source-aware process rather than relying on a single package metadata field. Pakchan stores changelog source mappings in `data/mappings.json`, which is fetched from GitHub in the background, cached locally, and consumed by the app to resolve the correct source for each package. `data/mappings.json` has four top-level sections: `github`, `gitlab`, `release_pages`, and `custom`.

### Add a package mapping

- Use the [web form](https://dodog.github.io/pakchan/web/) to submit a package and changelog source.
- Or open a GitHub issue: [Create an issue](https://github.com/dodog/pakchan/issues/new?labels=submission&template=add-package.yml).

### General strategy

Every source type checks these first, in order:

1. Check `data/mappings.json` for a known mapping — a `github` or `gitlab` repo, a `custom` parser entry, or a `release_pages` URL.
2. Use local AppStream metadata (`/usr/share/metainfo`, `/usr/share/appdata`) for desktop apps, when available.
3. Resolve GitHub/GitLab repos directly from the package's own URL (pacman/AUR), or scan the package homepage for an upstream repo link.
4. Beyond that, each package source has its own last-resort fallback — see below. Pacman-repo packages simply report the changelog as unavailable; AUR, Flatpak, and Snap each have their own further fallback.

### Pacman packages

- Starts with `data/mappings.json` and local AppStream metadata.
- Reads package metadata from `pacman -Si` to get the upstream homepage, if not already known.
- Looks for a direct GitHub/GitLab URL in the package's homepage, then scrapes the homepage itself for an upstream repo link if needed.
- If nothing upstream can be resolved, Pakchan reports the changelog as unavailable, with a link to the package's homepage if one is known — there is no packaging-history fallback for regular repo packages.

This means Pakchan prefers real upstream release notes, and simply says "not found" (with a homepage link when possible) rather than guessing at a substitute source.

### AUR packages

- Starts with `data/mappings.json` and local AppStream metadata.
- Fetches the package homepage URL from the AUR RPC API, if not already known.
- Tries a direct GitHub/GitLab URL, then homepage-based repo discovery.
- If no upstream changelog can be resolved, falls back to the AUR package's own cgit commit log (`aur.archlinux.org/cgit`) — the PKGBUILD's git history.

AUR fallback data is treated as packaging commit history, not the upstream project's official changelog.

### Flatpak / Flathub

- Starts with `data/mappings.json`.
- Queries the Flathub REST API for release metadata and notes.
- If the API has no release notes, it parses the Flathub AppStream XML from the CDN.
- If Flathub still doesn't provide usable notes, it tries the app's homepage URL for upstream GitHub/GitLab release data.

### Snap packages

- Starts with `data/mappings.json`.
- Queries the Snap Store API for version/channel metadata (the Snap Store itself has no changelog field).
- For the actual changelog, it tries a list of candidate URLs in order — the snap's source-code link, issues link, and website link from the Store API, then `snap info`'s own website line — checking each for upstream GitHub/GitLab release data until one works.
- If none of them yield a changelog, Pakchan reports that the Snap Store doesn't provide release notes, with a link to the best candidate page it found.

### Custom parsers

For packages with edge-case sources, a `custom` entry in `data/mappings.json` can select one of these parser types:

- `mozilla` — Mozilla's product-details JSON API, used for Firefox/Thunderbird
- `filezilla` — FileZilla's `changelog.php` release page
- `text_file` — a plain text changelog file (also auto-detects Markdown-style headings and switches to the Markdown parser)
- `github_raw` — a raw changelog file served straight from GitHub, e.g. `CHANGELOG.md`
- `gitlab` — force GitLab-style release resolution for a given `host`/`repo`, bypassing the top-level `gitlab` mapping section

These custom entries let Pakchan interpret release notes that standard parsing would miss. Separately, a `release_pages` mapping simply points at a page to scrape with Pakchan's generic, best-effort release-notes scraper — useful for sites that don't fit any of the parser types above.

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



## ❤️ Support
Do you find Pakchan useful? You can buy me a [coffee ☕](https://ko-fi.com/dodog)

## License

MIT — see [LICENSE](LICENSE)
