# 📦 Pakchan

Community-maintained changelog source mappings for the [Pakku](https://github.com/dodog/pakku) package manager.

[![GitHub Pages](https://img.shields.io/badge/Web%20Form-Live-brightgreen)](https://dodog.github.io/pakchan/web/)
[![Packages](https://img.shields.io/badge/Packages-18-blue)](https://dodog.github.io/pakchan/data/mappings.json)
[![Contributions Welcome](https://img.shields.io/badge/Contributions-Welcome-brightgreen.svg)](https://github.com/dodog/pakchan/issues)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

## What is Pakchan?

Pakchan is a database that tells [Pakku](https://github.com/dodog/pakku) (a PAMAC-like package manager for Arch Linux) exactly **where to find changelogs** for each package.

### The Problem

Most package managers only show you **that** an update is available, but not **what changed**. Pakku solves this by fetching real changelogs, but finding them is hard — some projects use GitHub Releases, others have custom release notes pages, plain text files, or wiki pages.

### The Solution

Pakchan maps package names to their changelog sources. Pakku checks Pakchan first, so it always knows where to look.

## 🚀 How It Works
┌─────────────┐     ┌─────────────┐     ┌─────────────┐  
│ Community   │     │   Pakchan   │     │    Pakku    │  
│ submits     │ ──▶ │  Database   │ ──▶ │   fetches   │  
│ mappings    │     │   (JSON)    │     │ changelogs  │  
└─────────────┘     └─────────────┘     └─────────────┘


1. **Community** adds package mappings via the [web form](https://dodog.github.io/pakchan/web/)
2. **Pakchan** stores them in `data/mappings.json`
3. **Pakku** downloads the database and fetches real changelogs

## 📋 Add a Package

### Via Web Form (Easiest)
👉 **[Submit a Package](https://dodog.github.io/pakchan/web/)**

Just enter the package name and its changelog source URL — a GitHub issue is created automatically.

### Via GitHub Issue
[Create an issue](https://github.com/dodog/pakchan/issues/new?labels=submission&template=add-package.yml) with the package details.

### Supported Source Types

| Type | Description | Example |
|------|-------------|---------|
| `github_releases` | GitHub Releases API | `videolan/vlc` |
| `gitlab_releases` | GitLab Releases API | `gitlab.gnome.org/GNOME/gimp` |
| `html_page` | Any HTML release notes page | `https://krita.org/en/release-notes/` |
| `mozilla_releases` | Mozilla-style release notes | Firefox, Thunderbird |
| `text_file` | Plain text changelog file | `https://example.com/CHANGELOG.txt` |

## 📊 Currently Mapped Packages


*[View all packages](https://dodog.github.io/pakchan/data/mappings.json)*

## 🔧 For Pakku Users

Pakku automatically uses Pakchan — no configuration needed. The database is cached locally and updated every 24 hours.


## 🤝 Contributing

### Adding a Package

1.  Go to the [submission form](https://dodog.github.io/pakchan/web/)
    
2.  Fill in the package name and changelog source
    
3.  Submit — it creates a GitHub issue
    
4.  After review, it's added to the database
    

### Improving an Existing Mapping

- [Open an issue](https://github.com/dodog/pakchan/issues/new) describing the improvement
    
- Or submit a pull request directly to `data/mappings.json`
    

### Format

Each package entry looks like:

```
{
  "package-name": {
    "name": "Display Name",
    "sources": [
      {
        "type": "github_releases",
        "repo": "owner/repository",
        "priority": 1
      }
    ]
  }
}
```
📁 Repository Structure
```
pakchan/  
├── data/  
│ └── mappings.json ← The database (this is what Pakku downloads)  
├── web/  
│ └── index.html ← Submission form  
├── scripts/  
│ └── process_submission.py  
└── .github/  
└── workflows/ ← Automated processing
```
## 🔗 Related Projects

- [Pakku](https://github.com/dodog/pakku) — PAMAC-like package manager for Arch Linux
    
   

## 📄 License

MIT — see [LICENSE](https://license/) file

* * *

