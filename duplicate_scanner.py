#!/usr/bin/env python3
"""
Duplicate File & Folder Scanner for macOS
==========================================
Scans an external HDD (or any directory) for:
  1. Duplicate FOLDERS (entire folder copies)
  2. Duplicate FILES (photos/videos with identical content)

Generates an HTML report for review, then interactively deletes on approval.

Usage:
    python3 duplicate_scanner.py /Volumes/YourHDD
    python3 duplicate_scanner.py /Volumes/YourHDD --report-only
    python3 duplicate_scanner.py /Volumes/YourHDD --min-size 1048576  # skip files < 1MB
"""

import os
import sys
import json
import hashlib
import argparse
import time
import shutil
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MEDIA_EXTENSIONS = {
    # Images
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
    '.heic', '.heif', '.webp', '.raw', '.cr2', '.nef', '.arw',
    '.dng', '.svg', '.ico', '.psd',
    # Videos
    '.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.m4v',
    '.mpg', '.mpeg', '.3gp', '.webm', '.ts', '.mts', '.m2ts',
    '.vob', '.divx',
}

SCAN_ALL = False  # Set True to scan ALL file types, not just media

CHUNK_SIZE = 8192  # For hashing
PARTIAL_HASH_SIZE = 64 * 1024  # 64KB for quick partial hash

# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def human_size(num_bytes):
    """Convert bytes to human-readable string."""
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def is_media_file(path):
    """Check if a file is a photo or video based on extension."""
    if SCAN_ALL:
        return True
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


def is_hidden(path):
    """Skip hidden files/folders (starting with .)"""
    return any(part.startswith('.') for part in Path(path).parts)


def file_hash(filepath, partial=False):
    """Compute SHA-256 hash of a file. If partial=True, hash only first 64KB."""
    h = hashlib.sha256()
    try:
        with open(filepath, 'rb') as f:
            if partial:
                data = f.read(PARTIAL_HASH_SIZE)
                if data:
                    h.update(data)
            else:
                while True:
                    data = f.read(CHUNK_SIZE)
                    if not data:
                        break
                    h.update(data)
    except (OSError, PermissionError):
        return None
    return h.hexdigest()


def folder_content_signature(folder_path):
    """
    Create a signature for a folder based on:
    - Relative file paths (sorted)
    - File sizes
    - Partial hashes of each file
    This identifies folders with identical content even if renamed.
    """
    entries = []
    try:
        for root, dirs, files in os.walk(folder_path):
            # Skip hidden subdirs
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fname in sorted(files):
                if fname.startswith('.'):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, folder_path)
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    continue
                entries.append((rel, size))
    except (OSError, PermissionError):
        return None

    if not entries:
        return None

    entries.sort()
    # Hash the structure: relative paths + sizes
    sig = hashlib.sha256()
    for rel, size in entries:
        sig.update(f"{rel}|{size}\n".encode())
    return sig.hexdigest()


def folder_full_hash(folder_path):
    """Full content hash of every file in the folder (for verification)."""
    h = hashlib.sha256()
    entries = []
    try:
        for root, dirs, files in os.walk(folder_path):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for fname in sorted(files):
                if fname.startswith('.'):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, folder_path)
                entries.append((rel, fpath))
    except (OSError, PermissionError):
        return None

    entries.sort()
    for rel, fpath in entries:
        h.update(rel.encode())
        fh = file_hash(fpath)
        if fh:
            h.update(fh.encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Phase 1: Scan for Duplicate Folders
# ---------------------------------------------------------------------------
def scan_duplicate_folders(root_dir, progress_callback=None):
    """Find duplicate folders by content signature."""
    print("\n📁 Phase 1: Scanning for duplicate folders...")

    # Collect all directories (bottom-up so we find leaf duplicates first)
    all_dirs = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        if is_hidden(root):
            continue
        # Only consider dirs that have at least 1 file (directly or nested)
        for d in dirs:
            dpath = os.path.join(root, d)
            all_dirs.append(dpath)

    print(f"   Found {len(all_dirs)} directories to analyze...")

    # Group by content signature
    sig_map = defaultdict(list)
    for i, dpath in enumerate(all_dirs):
        if progress_callback:
            progress_callback(i + 1, len(all_dirs), dpath)
        if i % 100 == 0:
            print(f"   Analyzing directory {i+1}/{len(all_dirs)}...", end='\r')

        sig = folder_content_signature(dpath)
        if sig:
            sig_map[sig].append(dpath)

    print(f"\n   Computed signatures for {len(all_dirs)} directories.")

    # Filter to groups with duplicates
    dup_folder_groups = []
    for sig, paths in sig_map.items():
        if len(paths) > 1:
            # Verify with full hash to avoid false positives
            full_hash_map = defaultdict(list)
            for p in paths:
                fh = folder_full_hash(p)
                if fh:
                    full_hash_map[fh].append(p)

            for fh, verified_paths in full_hash_map.items():
                if len(verified_paths) > 1:
                    # Calculate folder size
                    sizes = []
                    file_counts = []
                    for vp in verified_paths:
                        total = 0
                        count = 0
                        for r, ds, fs in os.walk(vp):
                            for f in fs:
                                try:
                                    total += os.path.getsize(os.path.join(r, f))
                                    count += 1
                                except OSError:
                                    pass
                        sizes.append(total)
                        file_counts.append(count)

                    dup_folder_groups.append({
                        'hash': fh[:16],
                        'paths': verified_paths,
                        'sizes': sizes,
                        'file_counts': file_counts,
                    })

    # Sort by total wasted space (descending)
    dup_folder_groups.sort(key=lambda g: sum(g['sizes'][1:]), reverse=True)
    print(f"   ✅ Found {len(dup_folder_groups)} duplicate folder groups.")
    return dup_folder_groups


# ---------------------------------------------------------------------------
# Phase 2: Scan for Duplicate Files
# ---------------------------------------------------------------------------
def scan_duplicate_files(root_dir, skip_paths=None, min_size=0):
    """Find duplicate files using size → partial hash → full hash pipeline."""
    skip_paths = skip_paths or set()
    print("\n📄 Phase 2: Scanning for duplicate files...")

    # Step 1: Group by size
    print("   Step 1/3: Grouping files by size...")
    size_map = defaultdict(list)
    total_files = 0
    skipped = 0

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        if is_hidden(root):
            continue

        # Skip directories already identified as duplicates (user will handle those)
        if any(root.startswith(sp) for sp in skip_paths):
            skipped += 1
            continue

        for fname in files:
            if fname.startswith('.'):
                continue
            fpath = os.path.join(root, fname)

            if not is_media_file(fpath):
                continue

            try:
                size = os.path.getsize(fpath)
                if size < min_size:
                    continue
                size_map[size].append(fpath)
                total_files += 1
            except OSError:
                continue

            if total_files % 5000 == 0:
                print(f"   Indexed {total_files} files...", end='\r')

    # Filter to sizes with >1 file
    candidates = {s: paths for s, paths in size_map.items() if len(paths) > 1}
    candidate_count = sum(len(p) for p in candidates.values())
    print(f"\n   Scanned {total_files} media files. {candidate_count} are size-matched candidates.")

    # Step 2: Partial hash
    print("   Step 2/3: Computing partial hashes...")
    partial_map = defaultdict(list)
    done = 0
    for size, paths in candidates.items():
        for fpath in paths:
            ph = file_hash(fpath, partial=True)
            if ph:
                partial_map[(size, ph)].append(fpath)
            done += 1
            if done % 1000 == 0:
                print(f"   Partial-hashed {done}/{candidate_count} files...", end='\r')

    partial_candidates = {k: v for k, v in partial_map.items() if len(v) > 1}
    partial_count = sum(len(v) for v in partial_candidates.values())
    print(f"\n   {partial_count} files remain after partial hash filter.")

    # Step 3: Full hash
    print("   Step 3/3: Computing full hashes (this may take a while for large files)...")
    full_map = defaultdict(list)
    done = 0
    for key, paths in partial_candidates.items():
        for fpath in paths:
            fh = file_hash(fpath)
            if fh:
                full_map[fh].append(fpath)
            done += 1
            if done % 500 == 0:
                print(f"   Full-hashed {done}/{partial_count} files...", end='\r')

    # Build duplicate groups
    dup_file_groups = []
    for fh, paths in full_map.items():
        if len(paths) > 1:
            sizes = []
            mod_times = []
            for p in paths:
                try:
                    stat = os.stat(p)
                    sizes.append(stat.st_size)
                    mod_times.append(stat.st_mtime)
                except OSError:
                    sizes.append(0)
                    mod_times.append(0)

            dup_file_groups.append({
                'hash': fh[:16],
                'paths': paths,
                'sizes': sizes,
                'mod_times': mod_times,
                'ext': Path(paths[0]).suffix.lower(),
            })

    dup_file_groups.sort(key=lambda g: g['sizes'][0] * (len(g['paths']) - 1), reverse=True)
    total_waste = sum(g['sizes'][0] * (len(g['paths']) - 1) for g in dup_file_groups)
    print(f"\n   ✅ Found {len(dup_file_groups)} duplicate file groups.")
    print(f"   💾 Potential space savings: {human_size(total_waste)}")
    return dup_file_groups


# ---------------------------------------------------------------------------
# HTML Report Generator
# ---------------------------------------------------------------------------
def generate_report(root_dir, dup_folders, dup_files, output_path):
    """Generate an interactive HTML report."""
    print("\n📊 Generating HTML report...")

    total_folder_waste = sum(
        sum(g['sizes'][1:]) for g in dup_folders
    )
    total_file_waste = sum(
        g['sizes'][0] * (len(g['paths']) - 1) for g in dup_files
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Duplicate Scanner Report</title>
<style>
    * {{ margin: 0; padding: 0; box-sizing: border-box; }}
    body {{
        font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
        background: #1a1a2e;
        color: #e0e0e0;
        line-height: 1.6;
        padding: 20px;
    }}
    .container {{ max-width: 1200px; margin: 0 auto; }}

    h1 {{
        font-size: 2rem;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 5px;
    }}
    .subtitle {{ color: #888; margin-bottom: 30px; font-size: 0.9rem; }}

    .stats {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
        gap: 15px;
        margin-bottom: 30px;
    }}
    .stat-card {{
        background: #16213e;
        border-radius: 12px;
        padding: 20px;
        border: 1px solid #2a2a4a;
    }}
    .stat-card .label {{ color: #888; font-size: 0.8rem; text-transform: uppercase; letter-spacing: 1px; }}
    .stat-card .value {{ font-size: 1.8rem; font-weight: 700; color: #667eea; margin-top: 5px; }}
    .stat-card .value.warning {{ color: #f7b731; }}
    .stat-card .value.danger {{ color: #fc5c65; }}

    .section {{
        background: #16213e;
        border-radius: 12px;
        padding: 25px;
        margin-bottom: 20px;
        border: 1px solid #2a2a4a;
    }}
    .section h2 {{
        font-size: 1.3rem;
        margin-bottom: 15px;
        display: flex;
        align-items: center;
        gap: 10px;
    }}
    .badge {{
        font-size: 0.75rem;
        padding: 3px 10px;
        border-radius: 20px;
        background: #2a2a4a;
        color: #aaa;
    }}

    .group {{
        background: #1a1a2e;
        border-radius: 8px;
        padding: 15px;
        margin-bottom: 10px;
        border: 1px solid #2a2a4a;
    }}
    .group-header {{
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-bottom: 10px;
        flex-wrap: wrap;
        gap: 8px;
    }}
    .group-header .tag {{
        font-size: 0.75rem;
        padding: 2px 8px;
        border-radius: 4px;
        background: #764ba2;
        color: #fff;
    }}
    .group-header .tag.size {{
        background: #2a2a4a;
        color: #f7b731;
    }}

    .file-entry {{
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 8px 12px;
        margin: 4px 0;
        border-radius: 6px;
        background: #16213e;
        font-size: 0.85rem;
        word-break: break-all;
    }}
    .file-entry input[type=checkbox] {{
        accent-color: #fc5c65;
        width: 16px;
        height: 16px;
        flex-shrink: 0;
    }}
    .file-entry .path {{ flex: 1; color: #ccc; }}
    .file-entry .meta {{ color: #888; font-size: 0.75rem; white-space: nowrap; }}
    .file-entry.keep {{ border-left: 3px solid #26de81; }}

    .actions {{
        position: sticky;
        bottom: 0;
        background: #16213e;
        border-top: 2px solid #764ba2;
        padding: 15px 25px;
        display: flex;
        justify-content: space-between;
        align-items: center;
        border-radius: 12px 12px 0 0;
        margin-top: 20px;
        z-index: 100;
    }}
    .btn {{
        padding: 10px 25px;
        border: none;
        border-radius: 8px;
        cursor: pointer;
        font-weight: 600;
        font-size: 0.9rem;
        transition: all 0.2s;
    }}
    .btn-danger {{ background: #fc5c65; color: #fff; }}
    .btn-danger:hover {{ background: #eb3b5a; transform: translateY(-1px); }}
    .btn-export {{ background: #2a2a4a; color: #ccc; }}
    .btn-export:hover {{ background: #3a3a5a; }}
    .btn:disabled {{ opacity: 0.4; cursor: not-allowed; transform: none; }}

    .select-controls {{
        display: flex;
        gap: 8px;
        margin-bottom: 10px;
    }}
    .select-controls button {{
        padding: 4px 12px;
        border: 1px solid #2a2a4a;
        background: transparent;
        color: #888;
        border-radius: 4px;
        cursor: pointer;
        font-size: 0.8rem;
    }}
    .select-controls button:hover {{ border-color: #667eea; color: #667eea; }}

    .empty {{ text-align: center; padding: 40px; color: #666; }}

    .collapse-btn {{
        background: none;
        border: 1px solid #2a2a4a;
        color: #888;
        padding: 2px 8px;
        border-radius: 4px;
        cursor: pointer;
        font-size: 0.8rem;
    }}
    .collapse-btn:hover {{ border-color: #667eea; color: #667eea; }}
    .collapsed .group-body {{ display: none; }}

    #selectedCount {{ font-size: 1rem; color: #f7b731; }}
    #selectedSize {{ color: #fc5c65; }}
</style>
</head>
<body>
<div class="container">

<h1>🔍 Duplicate Scanner Report</h1>
<p class="subtitle">Scanned: {root_dir} &mdash; {datetime.now().strftime('%Y-%m-%d %H:%M')}</p>

<div class="stats">
    <div class="stat-card">
        <div class="label">Duplicate Folder Groups</div>
        <div class="value">{len(dup_folders)}</div>
    </div>
    <div class="stat-card">
        <div class="label">Duplicate File Groups</div>
        <div class="value">{len(dup_files)}</div>
    </div>
    <div class="stat-card">
        <div class="label">Folder Waste</div>
        <div class="value warning">{human_size(total_folder_waste)}</div>
    </div>
    <div class="stat-card">
        <div class="label">File Waste</div>
        <div class="value danger">{human_size(total_file_waste)}</div>
    </div>
    <div class="stat-card">
        <div class="label">Total Reclaimable</div>
        <div class="value danger">{human_size(total_folder_waste + total_file_waste)}</div>
    </div>
</div>

<!-- ======================== DUPLICATE FOLDERS ======================== -->
<div class="section">
    <h2>📁 Duplicate Folders <span class="badge">{len(dup_folders)} groups</span></h2>
    <div class="select-controls">
        <button onclick="autoSelectFolders()">Auto-select duplicates (keep first)</button>
        <button onclick="clearAll('folder')">Clear all</button>
    </div>
"""

    if not dup_folders:
        html += '<div class="empty">No duplicate folders found. 🎉</div>'
    else:
        for gi, group in enumerate(dup_folders):
            waste = human_size(sum(group['sizes'][1:]))
            html += f"""
    <div class="group" id="folder-group-{gi}">
        <div class="group-header">
            <span><strong>Group {gi+1}</strong> &mdash; {len(group['paths'])} copies, {group['file_counts'][0]} files each</span>
            <span class="tag size">Waste: {waste}</span>
            <button class="collapse-btn" onclick="toggleGroup(this)">collapse</button>
        </div>
        <div class="group-body">
"""
            for fi, fpath in enumerate(group['paths']):
                size_str = human_size(group['sizes'][fi])
                keep_class = ' keep' if fi == 0 else ''
                checked = '' if fi == 0 else ''
                label = ' (oldest — auto-keep)' if fi == 0 else ''
                html += f"""
            <div class="file-entry{keep_class}">
                <input type="checkbox" class="del-check folder-check" data-path="{fpath}" data-size="{group['sizes'][fi]}" data-group="folder-{gi}" onchange="updateCount()" {checked}>
                <span class="path">{fpath}{label}</span>
                <span class="meta">{size_str}</span>
            </div>
"""
            html += "    </div></div>\n"

    html += "</div>\n"

    # ======================== DUPLICATE FILES ========================
    html += f"""
<div class="section">
    <h2>📄 Duplicate Files <span class="badge">{len(dup_files)} groups</span></h2>
    <div class="select-controls">
        <button onclick="autoSelectFiles()">Auto-select duplicates (keep first)</button>
        <button onclick="clearAll('file')">Clear all</button>
    </div>
"""

    if not dup_files:
        html += '<div class="empty">No duplicate files found. 🎉</div>'
    else:
        for gi, group in enumerate(dup_files):
            waste = human_size(group['sizes'][0] * (len(group['paths']) - 1))
            ext = group['ext']
            html += f"""
    <div class="group" id="file-group-{gi}">
        <div class="group-header">
            <span><strong>Group {gi+1}</strong> &mdash; {len(group['paths'])} copies</span>
            <span class="tag">{ext}</span>
            <span class="tag size">Waste: {waste}</span>
            <button class="collapse-btn" onclick="toggleGroup(this)">collapse</button>
        </div>
        <div class="group-body">
"""
            for fi, fpath in enumerate(group['paths']):
                size_str = human_size(group['sizes'][fi])
                mtime = datetime.fromtimestamp(group['mod_times'][fi]).strftime('%Y-%m-%d %H:%M')
                keep_class = ' keep' if fi == 0 else ''
                label = ' (keep)' if fi == 0 else ''
                html += f"""
            <div class="file-entry{keep_class}">
                <input type="checkbox" class="del-check file-check" data-path="{fpath}" data-size="{group['sizes'][fi]}" data-group="file-{gi}" onchange="updateCount()">
                <span class="path">{fpath}{label}</span>
                <span class="meta">{size_str} &middot; {mtime}</span>
            </div>
"""
            html += "    </div></div>\n"

    html += "</div>\n"

    # ======================== ACTION BAR ========================
    html += """
<div class="actions">
    <div>
        <span id="selectedCount">0 items selected</span>
        &mdash; <span id="selectedSize">0 B</span> to free
    </div>
    <div style="display:flex;gap:10px;">
        <button class="btn btn-export" onclick="exportDeletionList()">Export deletion list</button>
        <button class="btn btn-danger" id="deleteBtn" onclick="generateDeleteScript()" disabled>Generate delete script</button>
    </div>
</div>

</div><!-- container -->

<script>
function updateCount() {
    const checks = document.querySelectorAll('.del-check:checked');
    let totalSize = 0;
    checks.forEach(c => { totalSize += parseInt(c.dataset.size || 0); });
    document.getElementById('selectedCount').textContent = checks.length + ' items selected';
    document.getElementById('selectedSize').textContent = humanSize(totalSize);
    document.getElementById('deleteBtn').disabled = checks.length === 0;
}

function humanSize(bytes) {
    const units = ['B','KB','MB','GB','TB'];
    let i = 0;
    let b = bytes;
    while (b >= 1024 && i < units.length - 1) { b /= 1024; i++; }
    return b.toFixed(1) + ' ' + units[i];
}

function autoSelectFolders() {
    document.querySelectorAll('.folder-check').forEach(cb => {
        const entries = document.querySelectorAll(`[data-group="${cb.dataset.group}"]`);
        const idx = Array.from(entries).indexOf(cb);
        cb.checked = idx > 0;  // keep first, select rest
    });
    updateCount();
}

function autoSelectFiles() {
    document.querySelectorAll('.file-check').forEach(cb => {
        const entries = document.querySelectorAll(`[data-group="${cb.dataset.group}"]`);
        const idx = Array.from(entries).indexOf(cb);
        cb.checked = idx > 0;
    });
    updateCount();
}

function clearAll(type) {
    document.querySelectorAll(`.${type}-check`).forEach(cb => { cb.checked = false; });
    updateCount();
}

function toggleGroup(btn) {
    const group = btn.closest('.group');
    group.classList.toggle('collapsed');
    btn.textContent = group.classList.contains('collapsed') ? 'expand' : 'collapse';
}

function getSelectedPaths() {
    const paths = [];
    document.querySelectorAll('.del-check:checked').forEach(cb => {
        paths.push(cb.dataset.path);
    });
    return paths;
}

function exportDeletionList() {
    const paths = getSelectedPaths();
    if (paths.length === 0) { alert('No items selected.'); return; }
    const text = paths.join('\\n');
    const blob = new Blob([text], {type:'text/plain'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'deletion_list.txt';
    a.click();
}

function generateDeleteScript() {
    const paths = getSelectedPaths();
    if (paths.length === 0) { alert('No items selected.'); return; }

    const confirmed = confirm(
        `⚠️ You are about to generate a delete script for ${paths.length} items.\\n\\n` +
        `This will create a .sh file you can review and run.\\nProceed?`
    );
    if (!confirmed) return;

    let script = '#!/bin/bash\\n';
    script += '# Duplicate deletion script\\n';
    script += '# Generated: ' + new Date().toISOString() + '\\n';
    script += '# Review carefully before running!\\n\\n';
    script += 'set -e\\n\\n';

    // Separate folders and files
    const folderChecks = document.querySelectorAll('.folder-check:checked');
    const fileChecks = document.querySelectorAll('.file-check:checked');

    if (folderChecks.length > 0) {
        script += '# --- Duplicate Folders ---\\n';
        folderChecks.forEach(cb => {
            const p = cb.dataset.path.replace(/'/g, "'\\''");
            script += `rm -rf '${p}'\\n`;
        });
        script += '\\n';
    }

    if (fileChecks.length > 0) {
        script += '# --- Duplicate Files ---\\n';
        fileChecks.forEach(cb => {
            const p = cb.dataset.path.replace(/'/g, "'\\''");
            script += `rm '${p}'\\n`;
        });
    }

    script += '\\necho "✅ Deletion complete."\\n';

    const blob = new Blob([script], {type:'text/x-sh'});
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'delete_duplicates.sh';
    a.click();

    alert('Script downloaded! Review it, then run with:\\n  chmod +x delete_duplicates.sh\\n  ./delete_duplicates.sh');
}
</script>

</body>
</html>
"""

    with open(output_path, 'w') as f:
        f.write(html)

    print(f"   ✅ Report saved to: {output_path}")
    return output_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='🔍 Duplicate Photo/Video & Folder Scanner',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 duplicate_scanner.py /Volumes/MyHDD
    python3 duplicate_scanner.py /Volumes/MyHDD --report-only
    python3 duplicate_scanner.py /Volumes/MyHDD --min-size 100000 --all-files
    python3 duplicate_scanner.py ~/Pictures --output ~/Desktop/report.html
        """
    )
    parser.add_argument('path', help='Root directory to scan (e.g., /Volumes/YourHDD)')
    parser.add_argument('--output', '-o', help='Output report path (default: ~/Desktop/duplicate_report.html)',
                        default=os.path.expanduser('~/Desktop/duplicate_report.html'))
    parser.add_argument('--min-size', type=int, default=1024,
                        help='Minimum file size in bytes to consider (default: 1KB)')
    parser.add_argument('--report-only', action='store_true',
                        help='Generate report only, skip interactive deletion')
    parser.add_argument('--all-files', action='store_true',
                        help='Scan ALL file types, not just photos/videos')
    parser.add_argument('--skip-folders', action='store_true',
                        help='Skip duplicate folder detection (faster)')
    parser.add_argument('--skip-files', action='store_true',
                        help='Skip duplicate file detection')

    args = parser.parse_args()

    global SCAN_ALL
    SCAN_ALL = args.all_files

    root_dir = os.path.abspath(args.path)
    if not os.path.isdir(root_dir):
        print(f"❌ Error: '{root_dir}' is not a valid directory.")
        sys.exit(1)

    print("=" * 60)
    print("🔍 Duplicate Scanner")
    print("=" * 60)
    print(f"   Target:    {root_dir}")
    print(f"   Min size:  {human_size(args.min_size)}")
    print(f"   Mode:      {'All files' if SCAN_ALL else 'Photos & Videos only'}")
    print(f"   Report:    {args.output}")
    print("=" * 60)

    start_time = time.time()

    # Phase 1: Duplicate folders
    dup_folders = []
    if not args.skip_folders:
        dup_folders = scan_duplicate_folders(root_dir)

    # Collect paths of duplicate folders to optionally skip in file scan
    dup_folder_paths = set()
    for g in dup_folders:
        for p in g['paths'][1:]:  # skip the "keep" copy
            dup_folder_paths.add(p)

    # Phase 2: Duplicate files
    dup_files = []
    if not args.skip_files:
        dup_files = scan_duplicate_files(root_dir, skip_paths=dup_folder_paths, min_size=args.min_size)

    elapsed = time.time() - start_time

    # Generate report
    report_path = generate_report(root_dir, dup_folders, dup_files, args.output)

    print("\n" + "=" * 60)
    print(f"⏱️  Scan completed in {elapsed:.1f} seconds")
    print(f"📁 Duplicate folder groups: {len(dup_folders)}")
    print(f"📄 Duplicate file groups:   {len(dup_files)}")
    print("=" * 60)

    # Open report
    print(f"\n🌐 Opening report in browser...")
    webbrowser.open('file://' + os.path.abspath(report_path))

    if not args.report_only:
        print("\n📋 Review the report in your browser.")
        print("   • Select items to delete using checkboxes")
        print("   • Click 'Generate delete script' to create a .sh file")
        print("   • Review the script, then run it in Terminal")
        print("\n   Alternatively, export a deletion list as .txt")


if __name__ == '__main__':
    main()
