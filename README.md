# Duplicate-Scanner
File and Folder Duplicate scanner
# 🔍 Duplicate Scanner — Local Web App

A zero-dependency Python 3 tool for macOS that scans external HDDs (or any directory) for duplicate folders and files, serves an interactive web UI in your browser, and lets you delete duplicates with one click — no generated scripts, no manual terminal commands.

**No pip installs required** — runs on pure Python 3 (pre-installed on macOS).

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [How It Works](#how-it-works)
3. [Architecture](#architecture)
4. [Command Reference](#command-reference)
5. [Web UI Guide](#web-ui-guide)
6. [Deletion Workflow](#deletion-workflow)
7. [Supported File Types](#supported-file-types)
8. [Performance Notes](#performance-notes)
9. [Safety & Precautions](#safety--precautions)
10. [Troubleshooting](#troubleshooting)
11. [Examples](#examples)
12. [Comparison: App vs Script Version](#comparison-app-vs-script-version)
13. [Files in This Project](#files-in-this-project)

---

## Quick Start

```bash
# 1. Plug in your external HDD

# 2. Find your HDD's mount path
ls /Volumes/

# 3. Run the app
python3 duplicate_scanner_app.py /Volumes/YourHDDName

# 4. Browser opens automatically at http://localhost:8080
#    Watch the live scan → Review duplicates → Click Delete

# 5. Press Ctrl+C in terminal when done
```

That's it. No pip install, no Flask, no setup.

---

## How It Works

The app runs in three stages:

### Stage 1 — Scan (automatic on launch)

**Duplicate Folder Detection:**
- Computes a content signature for every folder using relative file paths + sizes
- Verifies matches with full SHA-256 hash of every file inside
- Catches renamed copies (e.g., `Photos` and `Photos (1)` with identical contents)

**Duplicate File Detection (3-stage pipeline):**

```
Stage 1: Group by file size
         Duplicates must be the same size — instant filter
              ↓
Stage 2: Partial hash (first 64KB only)
         Eliminates most false positives cheaply
              ↓
Stage 3: Full SHA-256 hash
         Confirms true duplicates with certainty
```

For a 400GB drive, only a small fraction of files actually get fully hashed.

### Stage 2 — Review (in browser)

An interactive web UI shows all duplicate groups with stats, search, tabs, and checkboxes. You pick what to delete.

### Stage 3 — Delete (one click)

Click "Delete Selected" → confirm in modal → files are deleted in batches directly from the browser. No shell scripts, no terminal commands.

---

## Architecture

```
┌─────────────────────────────────────────────────┐
│                  Your Browser                    │
│         http://localhost:8080                     │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │  Interactive Report UI                    │   │
│  │  • Live scan progress bar                 │   │
│  │  • Folder & File tabs                     │   │
│  │  • Checkboxes + Auto-select               │   │
│  │  • Search/filter                          │   │
│  │  • Delete button → confirmation modal     │   │
│  └──────────┬───────────────────────────────┘   │
│             │ HTTP requests                      │
└─────────────┼───────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────┐
│         Python HTTP Server (localhost)            │
│         (no Flask — uses built-in HTTPServer)     │
│                                                  │
│  GET  /              → Serves HTML UI            │
│  GET  /api/status    → Scan progress (polled)    │
│  GET  /api/scan      → Triggers background scan  │
│  GET  /api/results   → Returns duplicate data    │
│  POST /api/delete    → Deletes selected paths    │
│                                                  │
│  Scan runs in a background thread                │
│  Deletion runs in batches of 50                  │
└─────────────────────────────────────────────────┘
              │
              ▼
┌─────────────────────────────────────────────────┐
│            Your Filesystem                       │
│  /Volumes/MyHDD, ~/Pictures, etc.                │
│  (read during scan, write during delete)         │
└─────────────────────────────────────────────────┘
```

**Key points:**
- Server binds to `127.0.0.1` only — not accessible from other machines
- Scan runs in a background thread so the UI stays responsive
- All data stays local — nothing is uploaded anywhere

---

## Command Reference

```
python3 duplicate_scanner_app.py <path> [options]
```

### Required Argument

| Argument | Description |
|----------|-------------|
| `path` | Root directory to scan (e.g., `/Volumes/MyHDD`, `~/Pictures`) |

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--port`, `-p` | Port for the web server | `8080` |
| `--min-size` | Minimum file size in bytes to consider | `1024` (1 KB) |
| `--all-files` | Scan ALL file types, not just photos/videos | Off |
| `--skip-folders` | Skip duplicate folder detection (faster) | Off |
| `--skip-files` | Skip duplicate file detection | Off |

---

## Web UI Guide

### Scanning Screen

When you launch the app, the browser shows a scanning overlay with:
- **Phase indicator** — which stage the scan is in
- **Progress bar** — animated percentage
- **Detail text** — current operation (e.g., "Partial hash 1200/3400")

The overlay disappears automatically when the scan completes.

### Stats Dashboard

Five cards across the top showing:
- Duplicate folder group count
- Duplicate file group count
- Wasted space from folders
- Wasted space from files
- Total reclaimable space

### Tab Bar

Two tabs to switch between:
- **📁 Folders** — Duplicate folder groups
- **📄 Files** — Duplicate file groups

Each tab shows its count in parentheses.

### Control Bar

| Button | Action |
|--------|--------|
| **Auto-select duplicates** | Checks all copies except the first in each group (current tab) |
| **Clear all** | Unchecks everything |
| **Expand all** | Opens all groups in the current tab |
| **Collapse all** | Closes all groups |
| **Search box** | Filters groups by path text |

### Duplicate Groups

Each group shows:
- **Group number** and copy count
- **File extension** tag (for file groups)
- **Waste** tag showing reclaimable space
- Click the header to **expand/collapse**

Inside each group:
- First entry has a green **"(KEEP)"** label and green left border
- Each entry has a checkbox, full path, size, and modification date
- Check the copies you want to delete

### Bottom Action Bar (sticky)

Always visible at the bottom:
- **Selected count** and total size to free
- **Export list** — downloads a `.txt` file of selected paths
- **🗑 Delete Selected** — opens confirmation modal

---

## Deletion Workflow

```
┌──────────────────────────────┐
│  1. Select items             │
│     Use checkboxes or        │
│     "Auto-select duplicates" │
└──────────┬───────────────────┘
           ↓
┌──────────────────────────────┐
│  2. Click "Delete Selected"  │
│     Bottom action bar        │
└──────────┬───────────────────┘
           ↓
┌──────────────────────────────┐
│  3. Confirmation modal       │
│     Shows count & total size │
│     "This cannot be undone"  │
│                              │
│     [Cancel]  [Delete Now]   │
└──────────┬───────────────────┘
           ↓
┌──────────────────────────────┐
│  4. Batch deletion           │
│     Progress bar in modal    │
│     50 items per batch       │
│     Errors are skipped       │
└──────────┬───────────────────┘
           ↓
┌──────────────────────────────┐
│  5. Results toast            │
│     "Deleted 1200 items"     │
│     or "Deleted 1180, 20     │
│     failed"                  │
│                              │
│     Deleted items greyed out │
│     in the UI                │
└──────────────────────────────┘
```

**Key behaviors:**
- Deletion happens in batches of 50 for reliability
- Permission errors are skipped gracefully — the rest continue
- Deleted entries get greyed out and disabled in the UI
- A toast notification shows the final count
- Folders are deleted recursively (`rm -rf` equivalent)

---

## Supported File Types

By default, only media files are scanned:

### Images
`.jpg` `.jpeg` `.png` `.gif` `.bmp` `.tiff` `.tif` `.heic` `.heif` `.webp` `.raw` `.cr2` `.nef` `.arw` `.dng` `.svg` `.ico` `.psd`

### Videos
`.mp4` `.mov` `.avi` `.mkv` `.wmv` `.flv` `.m4v` `.mpg` `.mpeg` `.3gp` `.webm` `.ts` `.mts` `.m2ts` `.vob` `.divx`

To scan **all** file types, use the `--all-files` flag.

---

## Performance Notes

| Drive Size | Estimated Scan Time | Notes |
|------------|---------------------|-------|
| 50 GB | 5–10 min | Quick scan |
| 200 GB | 15–30 min | Moderate |
| 400 GB | 30–60 min | Depends on duplicate count |
| 1 TB+ | 1–2 hours | Consider `--skip-folders` |

### Tips for faster scans

- Use `--skip-folders` if you only care about file duplicates (folder scanning is the slowest phase)
- Use `--min-size 1048576` to skip files under 1 MB
- Use USB 3.0+ ports — significantly faster than USB 2.0
- Avoid running other disk-heavy tasks during the scan

### Deletion speed

- Batched at 50 items per request
- Typically completes in seconds even for thousands of items
- Permission-denied files are skipped instantly

---

## Safety & Precautions

### Built-in safety

- ✅ **Scan is read-only** — nothing is modified or deleted during scanning
- ✅ **Explicit confirmation required** — modal with count and size before any deletion
- ✅ **Keep-first logic** — first copy in each group is labeled "(KEEP)" and never pre-selected
- ✅ **Batch processing** — if one file fails, the rest still get deleted
- ✅ **Local only** — server binds to `127.0.0.1`, no external access
- ✅ **No dependencies** — pure Python 3, nothing to install
- ✅ **Graceful errors** — permission-denied and missing files are skipped with a count

### Recommended precautions

- 🔸 **Back up critical data** before deleting — deletions are permanent (not moved to Trash)
- 🔸 **Start with `--skip-files`** to review folder duplicates first, then re-run with `--skip-folders`
- 🔸 **Use the search box** to verify paths before bulk deletion
- 🔸 **Test on a small folder first** (e.g., `~/Pictures`) before scanning the whole HDD
- 🔸 **Review auto-selections** — the "Auto-select" button keeps the first copy, but verify the first copy is the one you actually want to keep

### What "permanent" means

Deleted files are removed directly via `os.remove()` and `shutil.rmtree()`. They do **not** go to macOS Trash. If your HDD supports data recovery tools, you may be able to recover recently deleted files, but there is no built-in undo.

---

## Troubleshooting

### Port already in use
```bash
# Use a different port
python3 duplicate_scanner_app.py /Volumes/MyHDD --port 9090
```

### Permission denied errors during scan
```bash
# Grant full disk access to Terminal in:
# System Settings → Privacy & Security → Full Disk Access → Terminal
```

### Permission denied errors during deletion
These are skipped automatically. The toast will show "X failed". Common causes:
- macOS SIP-protected files (e.g., Sony camera index files)
- Files locked by another process
- Read-only filesystem

To force-delete stubborn files manually:
```bash
sudo rm -f "/Volumes/MyHDD/path/to/file"
```

### Browser doesn't open
```bash
# Open manually
open http://localhost:8080
```

### HDD not showing up
```bash
ls /Volumes/
# If not listed, check Disk Utility
diskutil list
```

### Scan seems stuck
The full-hash phase can be slow for large video files (1GB+ each). Check the terminal — it prints progress there too. The browser polls every 500ms so the UI should update regularly.

### "python3 not found"
```bash
# Check installation
which python3

# Install via Homebrew if missing
brew install python3
```

### Want to re-scan without restarting
Currently, you need to stop (`Ctrl+C`) and re-run the command. The scan starts automatically on launch.

---

## Examples

```bash
# Basic scan of external HDD (photos/videos only)
python3 duplicate_scanner_app.py /Volumes/My\ Passport

# Scan on a different port
python3 duplicate_scanner_app.py /Volumes/My\ Passport -p 9090

# Scan everything, not just media files
python3 duplicate_scanner_app.py /Volumes/My\ Passport --all-files

# Skip small files (under 1MB)
python3 duplicate_scanner_app.py /Volumes/My\ Passport --min-size 1048576

# Only find duplicate files, skip folder comparison (faster)
python3 duplicate_scanner_app.py /Volumes/My\ Passport --skip-folders

# Only find duplicate folders
python3 duplicate_scanner_app.py /Volumes/My\ Passport --skip-files

# Scan local Pictures folder
python3 duplicate_scanner_app.py ~/Pictures

# Full scan, all files, different port
python3 duplicate_scanner_app.py /Volumes/My\ Passport --all-files --port 3000
```

---

## Comparison: App vs Script Version

| Feature | Script (`duplicate_scanner.py`) | Web App (`duplicate_scanner_app.py`) |
|---------|--------------------------------|--------------------------------------|
| Dependencies | None | None |
| Report | Static HTML file | Live web UI |
| Scan progress | Terminal text only | Animated progress bar in browser |
| Deletion | Generate `.sh` script → review → run manually | One-click in browser with confirmation |
| Search/filter | No | Yes |
| Error handling | Script crashes on first error (`set -e`) | Skips errors, continues, reports count |
| Special characters in paths | Requires careful shell escaping | Handled automatically |
| Re-scan | Re-run command | Re-run command (same) |
| Offline report | Yes (saved HTML file) | No (runs while server is active) |
| Security | No network | `127.0.0.1` only (localhost) |

**When to use which:**
- Use the **web app** for the best experience — live progress, one-click delete, no script hassles
- Use the **script version** if you want a portable HTML report to review later or share

---

## Files in This Project

```
duplicate_scanner_app.py    ← Web app version (this README)
duplicate_scanner.py        ← Original script version (generates static HTML + .sh)
README.md                   ← This file
```

---

## API Reference (for advanced users)

The web app exposes a simple REST API on localhost:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Serves the HTML UI |
| `/api/status` | GET | Returns scan status, phase, progress, percent |
| `/api/scan` | GET | Triggers a background scan (if not already running) |
| `/api/results` | GET | Returns full duplicate data (folders + files) as JSON |
| `/api/delete` | POST | Deletes paths. Body: `{"paths": ["/path/1", "/path/2"]}` |

### Example: Get results as JSON
```bash
curl http://localhost:8080/api/results | python3 -m json.tool
```

### Example: Delete specific files via API
```bash
curl -X POST http://localhost:8080/api/delete \
  -H "Content-Type: application/json" \
  -d '{"paths": ["/Volumes/MyHDD/duplicate_photo.jpg"]}'
```

---

*Generated with Claude — April 2026*
