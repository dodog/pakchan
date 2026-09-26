
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
