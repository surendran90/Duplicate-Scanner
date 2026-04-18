#!/usr/bin/env python3
"""
Duplicate Scanner — Local Web App
===================================
Scans for duplicate folders & files, shows an interactive report
in your browser, and deletes selected duplicates with one click.

Usage:
    python3 duplicate_scanner_app.py /Volumes/YourHDD
    python3 duplicate_scanner_app.py /Volumes/YourHDD --port 9090
    python3 duplicate_scanner_app.py /Volumes/YourHDD --all-files

Requirements:
    pip3 install flask

Then open http://localhost:8080 in your browser.
"""

import os
import sys
import json
import hashlib
import argparse
import time
import shutil
import threading
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MEDIA_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
    '.heic', '.heif', '.webp', '.raw', '.cr2', '.nef', '.arw',
    '.dng', '.svg', '.ico', '.psd',
    '.mp4', '.mov', '.avi', '.mkv', '.wmv', '.flv', '.m4v',
    '.mpg', '.mpeg', '.3gp', '.webm', '.ts', '.mts', '.m2ts',
    '.vob', '.divx',
}

CHUNK_SIZE = 8192
PARTIAL_HASH_SIZE = 64 * 1024

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
scan_state = {
    'status': 'idle',       # idle, scanning, complete, error
    'phase': '',
    'progress': '',
    'percent': 0,
    'dup_folders': [],
    'dup_files': [],
    'root_dir': '',
    'scan_all': False,
    'min_size': 1024,
    'skip_folders': False,
    'skip_files': False,
    'start_time': 0,
    'elapsed': 0,
    'deletion_log': [],
}

# ---------------------------------------------------------------------------
# Scanning functions (same logic, with progress updates)
# ---------------------------------------------------------------------------
def human_size(num_bytes):
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def is_media_file(path):
    if scan_state['scan_all']:
        return True
    return Path(path).suffix.lower() in MEDIA_EXTENSIONS


def is_hidden(path):
    return any(part.startswith('.') for part in Path(path).parts)


def file_hash(filepath, partial=False):
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
    entries = []
    try:
        for root, dirs, files in os.walk(folder_path):
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
    sig = hashlib.sha256()
    for rel, size in entries:
        sig.update(f"{rel}|{size}\n".encode())
    return sig.hexdigest()


def folder_full_hash(folder_path):
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


def scan_duplicate_folders(root_dir):
    scan_state['phase'] = 'Scanning folders...'
    all_dirs = []
    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        if is_hidden(root):
            continue
        for d in dirs:
            all_dirs.append(os.path.join(root, d))

    total = len(all_dirs)
    scan_state['progress'] = f'Found {total} directories to analyze'

    sig_map = defaultdict(list)
    for i, dpath in enumerate(all_dirs):
        pct = int((i + 1) / total * 100) if total else 0
        scan_state['percent'] = pct
        scan_state['progress'] = f'Analyzing folder {i+1}/{total}'
        sig = folder_content_signature(dpath)
        if sig:
            sig_map[sig].append(dpath)

    dup_folder_groups = []
    for sig, paths in sig_map.items():
        if len(paths) > 1:
            full_hash_map = defaultdict(list)
            for p in paths:
                fh = folder_full_hash(p)
                if fh:
                    full_hash_map[fh].append(p)
            for fh, verified_paths in full_hash_map.items():
                if len(verified_paths) > 1:
                    sizes, file_counts = [], []
                    for vp in verified_paths:
                        t, c = 0, 0
                        for r, ds, fs in os.walk(vp):
                            for f in fs:
                                try:
                                    t += os.path.getsize(os.path.join(r, f))
                                    c += 1
                                except OSError:
                                    pass
                        sizes.append(t)
                        file_counts.append(c)
                    dup_folder_groups.append({
                        'hash': fh[:16], 'paths': verified_paths,
                        'sizes': sizes, 'file_counts': file_counts,
                    })

    dup_folder_groups.sort(key=lambda g: sum(g['sizes'][1:]), reverse=True)
    return dup_folder_groups


def scan_duplicate_files(root_dir, skip_paths=None, min_size=0):
    skip_paths = skip_paths or set()
    scan_state['phase'] = 'Indexing files...'
    size_map = defaultdict(list)
    total_files = 0

    for root, dirs, files in os.walk(root_dir):
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        if is_hidden(root):
            continue
        if any(root.startswith(sp) for sp in skip_paths):
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
            if total_files % 2000 == 0:
                scan_state['progress'] = f'Indexed {total_files} files...'

    candidates = {s: p for s, p in size_map.items() if len(p) > 1}
    candidate_count = sum(len(p) for p in candidates.values())
    scan_state['progress'] = f'{total_files} files indexed, {candidate_count} size-matched'

    # Partial hash
    scan_state['phase'] = 'Partial hashing...'
    partial_map = defaultdict(list)
    done = 0
    for size, paths in candidates.items():
        for fpath in paths:
            ph = file_hash(fpath, partial=True)
            if ph:
                partial_map[(size, ph)].append(fpath)
            done += 1
            if done % 500 == 0:
                scan_state['percent'] = int(done / candidate_count * 100) if candidate_count else 0
                scan_state['progress'] = f'Partial hash {done}/{candidate_count}'

    partial_candidates = {k: v for k, v in partial_map.items() if len(v) > 1}
    partial_count = sum(len(v) for v in partial_candidates.values())

    # Full hash
    scan_state['phase'] = 'Full hashing...'
    full_map = defaultdict(list)
    done = 0
    for key, paths in partial_candidates.items():
        for fpath in paths:
            fh = file_hash(fpath)
            if fh:
                full_map[fh].append(fpath)
            done += 1
            if done % 200 == 0:
                scan_state['percent'] = int(done / partial_count * 100) if partial_count else 0
                scan_state['progress'] = f'Full hash {done}/{partial_count}'

    dup_file_groups = []
    for fh, paths in full_map.items():
        if len(paths) > 1:
            sizes, mod_times = [], []
            for p in paths:
                try:
                    st = os.stat(p)
                    sizes.append(st.st_size)
                    mod_times.append(st.st_mtime)
                except OSError:
                    sizes.append(0)
                    mod_times.append(0)
            dup_file_groups.append({
                'hash': fh[:16], 'paths': paths, 'sizes': sizes,
                'mod_times': mod_times, 'ext': Path(paths[0]).suffix.lower(),
            })

    dup_file_groups.sort(key=lambda g: g['sizes'][0] * (len(g['paths']) - 1), reverse=True)
    return dup_file_groups


def run_scan():
    """Background scan thread."""
    try:
        scan_state['status'] = 'scanning'
        scan_state['start_time'] = time.time()
        scan_state['dup_folders'] = []
        scan_state['dup_files'] = []
        scan_state['deletion_log'] = []

        root = scan_state['root_dir']

        # Phase 1
        if not scan_state['skip_folders']:
            scan_state['phase'] = 'Phase 1: Duplicate Folders'
            scan_state['percent'] = 0
            scan_state['dup_folders'] = scan_duplicate_folders(root)

        # Phase 2
        skip_paths = set()
        for g in scan_state['dup_folders']:
            for p in g['paths'][1:]:
                skip_paths.add(p)

        if not scan_state['skip_files']:
            scan_state['phase'] = 'Phase 2: Duplicate Files'
            scan_state['percent'] = 0
            scan_state['dup_files'] = scan_duplicate_files(
                root, skip_paths, scan_state['min_size']
            )

        scan_state['elapsed'] = time.time() - scan_state['start_time']
        scan_state['status'] = 'complete'
        scan_state['phase'] = 'Scan complete'
        scan_state['percent'] = 100

    except Exception as e:
        scan_state['status'] = 'error'
        scan_state['phase'] = f'Error: {str(e)}'
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Deletion handler
# ---------------------------------------------------------------------------
def delete_paths(paths):
    """Delete list of file/folder paths. Returns results."""
    results = []
    for p in paths:
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
                results.append({'path': p, 'status': 'deleted', 'type': 'folder'})
            elif os.path.isfile(p):
                os.remove(p)
                results.append({'path': p, 'status': 'deleted', 'type': 'file'})
            else:
                results.append({'path': p, 'status': 'not_found', 'type': 'unknown'})
        except PermissionError:
            results.append({'path': p, 'status': 'permission_denied', 'type': 'unknown'})
        except OSError as e:
            results.append({'path': p, 'status': f'error: {str(e)}', 'type': 'unknown'})
    return results


# ---------------------------------------------------------------------------
# HTTP Server (no Flask dependency!)
# ---------------------------------------------------------------------------
class DupScannerHandler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        # Suppress default request logging noise
        pass

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(get_html().encode())

        elif path == '/api/status':
            self.send_json({
                'status': scan_state['status'],
                'phase': scan_state['phase'],
                'progress': scan_state['progress'],
                'percent': scan_state['percent'],
            })

        elif path == '/api/results':
            folders = scan_state['dup_folders']
            files = scan_state['dup_files']
            total_folder_waste = sum(sum(g['sizes'][1:]) for g in folders)
            total_file_waste = sum(g['sizes'][0] * (len(g['paths']) - 1) for g in files)

            # Convert mod_times to strings for JSON
            files_serializable = []
            for g in files:
                files_serializable.append({
                    **g,
                    'mod_times_str': [
                        datetime.fromtimestamp(mt).strftime('%Y-%m-%d %H:%M')
                        for mt in g['mod_times']
                    ]
                })

            self.send_json({
                'status': scan_state['status'],
                'elapsed': round(scan_state.get('elapsed', 0), 1),
                'root_dir': scan_state['root_dir'],
                'dup_folders': folders,
                'dup_files': files_serializable,
                'total_folder_waste': total_folder_waste,
                'total_file_waste': total_file_waste,
                'folder_waste_human': human_size(total_folder_waste),
                'file_waste_human': human_size(total_file_waste),
                'total_waste_human': human_size(total_folder_waste + total_file_waste),
            })

        elif path == '/api/scan':
            if scan_state['status'] != 'scanning':
                t = threading.Thread(target=run_scan, daemon=True)
                t.start()
                self.send_json({'ok': True, 'message': 'Scan started'})
            else:
                self.send_json({'ok': False, 'message': 'Scan already running'})

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == '/api/delete':
            content_len = int(self.headers.get('Content-Length', 0))
            body = self.rfile.read(content_len).decode()
            data = json.loads(body)
            paths = data.get('paths', [])

            if not paths:
                self.send_json({'ok': False, 'message': 'No paths provided'}, 400)
                return

            results = delete_paths(paths)
            scan_state['deletion_log'].extend(results)

            deleted = sum(1 for r in results if r['status'] == 'deleted')
            failed = sum(1 for r in results if r['status'] != 'deleted')

            self.send_json({
                'ok': True,
                'deleted': deleted,
                'failed': failed,
                'results': results,
            })
        else:
            self.send_response(404)
            self.end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()


# ---------------------------------------------------------------------------
# HTML UI
# ---------------------------------------------------------------------------
def get_html():
    return '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Duplicate Scanner</title>
<style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Text', 'Helvetica Neue', sans-serif;
        background: #0f0f1a;
        color: #e0e0e0;
        line-height: 1.6;
    }

    /* --- Top bar --- */
    .topbar {
        background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
        border-bottom: 1px solid #2a2a4a;
        padding: 16px 24px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        position: sticky;
        top: 0;
        z-index: 200;
    }
    .topbar h1 {
        font-size: 1.3rem;
        background: linear-gradient(135deg, #667eea, #764ba2);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
    }
    .topbar .scan-info { color: #888; font-size: 0.85rem; }

    /* --- Scanning overlay --- */
    .scan-overlay {
        position: fixed; inset: 0;
        background: #0f0f1aee;
        display: flex;
        align-items: center;
        justify-content: center;
        z-index: 300;
    }
    .scan-card {
        background: #16213e;
        border: 1px solid #2a2a4a;
        border-radius: 16px;
        padding: 40px 50px;
        text-align: center;
        min-width: 400px;
    }
    .scan-card h2 { color: #667eea; margin-bottom: 20px; }
    .progress-bar {
        background: #2a2a4a;
        border-radius: 8px;
        height: 8px;
        margin: 16px 0;
        overflow: hidden;
    }
    .progress-fill {
        height: 100%;
        background: linear-gradient(90deg, #667eea, #764ba2);
        border-radius: 8px;
        transition: width 0.3s;
    }
    .scan-phase { color: #aaa; font-size: 0.9rem; margin-bottom: 6px; }
    .scan-progress { color: #666; font-size: 0.8rem; }

    /* --- Layout --- */
    .container { max-width: 1300px; margin: 0 auto; padding: 20px 24px; }

    /* --- Stats --- */
    .stats {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        gap: 12px;
        margin-bottom: 24px;
    }
    .stat-card {
        background: #16213e;
        border-radius: 12px;
        padding: 16px 20px;
        border: 1px solid #2a2a4a;
    }
    .stat-card .label {
        color: #888; font-size: 0.7rem;
        text-transform: uppercase; letter-spacing: 1px;
    }
    .stat-card .value {
        font-size: 1.6rem; font-weight: 700;
        color: #667eea; margin-top: 4px;
    }
    .stat-card .value.warn { color: #f7b731; }
    .stat-card .value.danger { color: #fc5c65; }

    /* --- Tab bar --- */
    .tabs {
        display: flex; gap: 0;
        border-bottom: 2px solid #2a2a4a;
        margin-bottom: 16px;
    }
    .tab-btn {
        padding: 10px 24px;
        background: none; border: none;
        color: #888; cursor: pointer;
        font-size: 0.9rem; font-weight: 500;
        border-bottom: 2px solid transparent;
        margin-bottom: -2px;
        transition: all 0.2s;
    }
    .tab-btn:hover { color: #ccc; }
    .tab-btn.active {
        color: #667eea;
        border-bottom-color: #667eea;
    }

    /* --- Controls --- */
    .controls {
        display: flex; gap: 8px;
        align-items: center;
        margin-bottom: 12px;
        flex-wrap: wrap;
    }
    .ctrl-btn {
        padding: 6px 14px;
        border: 1px solid #2a2a4a;
        background: transparent;
        color: #aaa;
        border-radius: 6px;
        cursor: pointer;
        font-size: 0.8rem;
        transition: all 0.15s;
    }
    .ctrl-btn:hover { border-color: #667eea; color: #667eea; }
    .ctrl-btn.danger { border-color: #fc5c65; color: #fc5c65; }
    .ctrl-btn.danger:hover { background: #fc5c65; color: #fff; }
    .ctrl-btn:disabled { opacity: 0.3; cursor: not-allowed; }

    .search-box {
        padding: 6px 14px;
        border: 1px solid #2a2a4a;
        background: #1a1a2e;
        color: #ccc;
        border-radius: 6px;
        font-size: 0.8rem;
        width: 250px;
        outline: none;
    }
    .search-box:focus { border-color: #667eea; }

    /* --- Groups --- */
    .group {
        background: #16213e;
        border-radius: 10px;
        margin-bottom: 8px;
        border: 1px solid #2a2a4a;
        overflow: hidden;
    }
    .group-head {
        padding: 12px 16px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        cursor: pointer;
        gap: 10px;
    }
    .group-head:hover { background: #1a1a2e; }
    .group-head .left { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
    .group-head .right { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
    .tag {
        font-size: 0.7rem; padding: 2px 8px;
        border-radius: 4px; font-weight: 500;
    }
    .tag.ext { background: #764ba2; color: #fff; }
    .tag.waste { background: #2a2a4a; color: #f7b731; }
    .tag.count { background: #2a2a4a; color: #aaa; }
    .chevron {
        color: #555; transition: transform 0.2s;
        font-size: 0.8rem;
    }
    .group.open .chevron { transform: rotate(90deg); }
    .group-body { display: none; padding: 4px 12px 12px; }
    .group.open .group-body { display: block; }

    /* --- File entries --- */
    .entry {
        display: flex; align-items: center;
        gap: 10px; padding: 8px 12px;
        margin: 3px 0; border-radius: 6px;
        background: #1a1a2e;
        font-size: 0.82rem;
        word-break: break-all;
    }
    .entry input[type=checkbox] {
        accent-color: #fc5c65;
        width: 15px; height: 15px;
        flex-shrink: 0;
    }
    .entry .path { flex: 1; color: #ccc; }
    .entry .meta { color: #666; font-size: 0.72rem; white-space: nowrap; }
    .entry.keep { border-left: 3px solid #26de81; }

    /* --- Bottom action bar --- */
    .action-bar {
        position: fixed; bottom: 0; left: 0; right: 0;
        background: #16213e;
        border-top: 2px solid #764ba2;
        padding: 12px 24px;
        display: flex;
        align-items: center;
        justify-content: space-between;
        z-index: 200;
    }
    .action-bar .info { font-size: 0.9rem; }
    .action-bar .info .count { color: #f7b731; font-weight: 600; }
    .action-bar .info .size { color: #fc5c65; font-weight: 600; }
    .action-bar .btns { display: flex; gap: 10px; }
    .del-btn {
        padding: 10px 28px;
        border: none; border-radius: 8px;
        cursor: pointer; font-weight: 600;
        font-size: 0.9rem; transition: all 0.2s;
    }
    .del-btn.primary { background: #fc5c65; color: #fff; }
    .del-btn.primary:hover { background: #eb3b5a; transform: translateY(-1px); }
    .del-btn.secondary { background: #2a2a4a; color: #ccc; }
    .del-btn.secondary:hover { background: #3a3a5a; }
    .del-btn:disabled { opacity: 0.3; cursor: not-allowed; transform: none; }

    /* --- Toast --- */
    .toast {
        position: fixed; top: 80px; right: 24px;
        background: #26de81; color: #000;
        padding: 12px 24px; border-radius: 8px;
        font-weight: 600; font-size: 0.9rem;
        z-index: 400; display: none;
        box-shadow: 0 4px 20px rgba(38,222,129,0.3);
    }
    .toast.error { background: #fc5c65; color: #fff; }
    .toast.show { display: block; }

    /* --- Delete modal --- */
    .modal-overlay {
        position: fixed; inset: 0;
        background: #000000aa;
        display: none;
        align-items: center;
        justify-content: center;
        z-index: 500;
    }
    .modal-overlay.show { display: flex; }
    .modal {
        background: #16213e;
        border: 1px solid #2a2a4a;
        border-radius: 16px;
        padding: 30px 36px;
        max-width: 500px;
        text-align: center;
    }
    .modal h3 { color: #fc5c65; margin-bottom: 12px; font-size: 1.2rem; }
    .modal p { color: #aaa; margin-bottom: 20px; font-size: 0.9rem; line-height: 1.6; }
    .modal .modal-btns { display: flex; gap: 12px; justify-content: center; }
    .modal .modal-btn {
        padding: 10px 28px; border: none;
        border-radius: 8px; cursor: pointer;
        font-weight: 600; font-size: 0.9rem;
    }
    .modal .modal-btn.cancel { background: #2a2a4a; color: #ccc; }
    .modal .modal-btn.confirm { background: #fc5c65; color: #fff; }

    /* --- Deleting progress --- */
    .del-progress {
        margin-top: 16px;
        display: none;
    }
    .del-progress.show { display: block; }
    .del-progress .del-bar {
        background: #2a2a4a; height: 6px;
        border-radius: 4px; overflow: hidden;
        margin-top: 8px;
    }
    .del-progress .del-fill {
        height: 100%; background: #fc5c65;
        border-radius: 4px; transition: width 0.3s;
    }

    .spacer { height: 80px; }
    .empty { text-align: center; padding: 40px; color: #555; }
</style>
</head>
<body>

<div class="topbar">
    <h1>&#128269; Duplicate Scanner</h1>
    <span class="scan-info" id="scanInfo"></span>
</div>

<!-- Scanning overlay -->
<div class="scan-overlay" id="scanOverlay">
    <div class="scan-card">
        <h2>&#128269; Scanning...</h2>
        <div class="scan-phase" id="scanPhase">Initializing...</div>
        <div class="progress-bar"><div class="progress-fill" id="scanBar" style="width:0%"></div></div>
        <div class="scan-progress" id="scanDetail">Starting scan...</div>
    </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<!-- Confirm modal -->
<div class="modal-overlay" id="confirmModal">
    <div class="modal">
        <h3>&#9888;&#65039; Confirm Deletion</h3>
        <p id="confirmText"></p>
        <div class="del-progress" id="delProgress">
            <div style="color:#aaa;font-size:0.85rem" id="delStatus">Deleting...</div>
            <div class="del-bar"><div class="del-fill" id="delBar" style="width:0%"></div></div>
        </div>
        <div class="modal-btns" id="modalBtns">
            <button class="modal-btn cancel" onclick="closeModal()">Cancel</button>
            <button class="modal-btn confirm" id="confirmBtn" onclick="executeDelete()">Delete Now</button>
        </div>
    </div>
</div>

<div class="container">
    <!-- Stats -->
    <div class="stats" id="statsRow"></div>

    <!-- Tabs -->
    <div class="tabs">
        <button class="tab-btn active" data-tab="folders" onclick="switchTab('folders', this)">&#128193; Folders <span id="folderCount"></span></button>
        <button class="tab-btn" data-tab="files" onclick="switchTab('files', this)">&#128196; Files <span id="fileCount"></span></button>
    </div>

    <!-- Controls -->
    <div class="controls">
        <button class="ctrl-btn" onclick="autoSelect()">Auto-select duplicates</button>
        <button class="ctrl-btn" onclick="clearAll()">Clear all</button>
        <button class="ctrl-btn" onclick="expandAll()">Expand all</button>
        <button class="ctrl-btn" onclick="collapseAll()">Collapse all</button>
        <input class="search-box" type="text" placeholder="Search paths..." oninput="filterGroups(this.value)" id="searchBox">
    </div>

    <!-- Content -->
    <div id="foldersTab"></div>
    <div id="filesTab" style="display:none"></div>

    <div class="spacer"></div>
</div>

<!-- Action bar -->
<div class="action-bar">
    <div class="info">
        <span class="count" id="selCount">0 items</span> selected &mdash;
        <span class="size" id="selSize">0 B</span> to free
    </div>
    <div class="btns">
        <button class="del-btn secondary" onclick="exportList()" id="exportBtn" disabled>Export list</button>
        <button class="del-btn primary" onclick="showConfirm()" id="deleteBtn" disabled>&#128465; Delete Selected</button>
    </div>
</div>

<script>
// --- State ---
let results = null;
let activeTab = 'folders';
let pendingDeletePaths = [];

// --- Init ---
pollScan();

function pollScan() {
    fetch('/api/status').then(r => r.json()).then(d => {
        if (d.status === 'scanning') {
            document.getElementById('scanOverlay').style.display = 'flex';
            document.getElementById('scanPhase').textContent = d.phase;
            document.getElementById('scanDetail').textContent = d.progress;
            document.getElementById('scanBar').style.width = d.percent + '%';
            setTimeout(pollScan, 500);
        } else if (d.status === 'complete') {
            document.getElementById('scanOverlay').style.display = 'none';
            loadResults();
        } else if (d.status === 'idle') {
            // Start scan
            fetch('/api/scan').then(() => setTimeout(pollScan, 500));
        } else if (d.status === 'error') {
            document.getElementById('scanPhase').textContent = d.phase;
            document.getElementById('scanDetail').textContent = 'Check terminal for details';
        }
    });
}

function loadResults() {
    fetch('/api/results').then(r => r.json()).then(d => {
        results = d;
        document.getElementById('scanInfo').textContent =
            d.root_dir + ' — scanned in ' + d.elapsed + 's';
        renderStats();
        renderFolders();
        renderFiles();
        document.getElementById('folderCount').textContent = '(' + d.dup_folders.length + ')';
        document.getElementById('fileCount').textContent = '(' + d.dup_files.length + ')';
    });
}

// --- Stats ---
function renderStats() {
    const d = results;
    document.getElementById('statsRow').innerHTML = `
        <div class="stat-card"><div class="label">Folder Groups</div><div class="value">${d.dup_folders.length}</div></div>
        <div class="stat-card"><div class="label">File Groups</div><div class="value">${d.dup_files.length}</div></div>
        <div class="stat-card"><div class="label">Folder Waste</div><div class="value warn">${d.folder_waste_human}</div></div>
        <div class="stat-card"><div class="label">File Waste</div><div class="value danger">${d.file_waste_human}</div></div>
        <div class="stat-card"><div class="label">Total Reclaimable</div><div class="value danger">${d.total_waste_human}</div></div>
    `;
}

// --- Render groups ---
function humanSize(bytes) {
    const u = ['B','KB','MB','GB','TB'];
    let i = 0, b = bytes;
    while (b >= 1024 && i < u.length-1) { b /= 1024; i++; }
    return b.toFixed(1) + ' ' + u[i];
}

function renderFolders() {
    const el = document.getElementById('foldersTab');
    if (!results.dup_folders.length) {
        el.innerHTML = '<div class="empty">No duplicate folders found &#127881;</div>';
        return;
    }
    let html = '';
    results.dup_folders.forEach((g, gi) => {
        const waste = humanSize(g.sizes.slice(1).reduce((a,b) => a+b, 0));
        html += `<div class="group" data-gtype="folder" data-gidx="${gi}">
            <div class="group-head" onclick="toggleGroup(this)">
                <div class="left">
                    <span class="chevron">&#9654;</span>
                    <strong>Group ${gi+1}</strong>
                    <span class="tag count">${g.paths.length} copies, ${g.file_counts[0]} files</span>
                </div>
                <div class="right">
                    <span class="tag waste">${waste}</span>
                </div>
            </div>
            <div class="group-body">`;
        g.paths.forEach((p, fi) => {
            const sz = humanSize(g.sizes[fi]);
            const keep = fi === 0 ? ' keep' : '';
            const label = fi === 0 ? ' <span style="color:#26de81;font-size:0.7rem">(KEEP)</span>' : '';
            html += `<div class="entry${keep}">
                <input type="checkbox" data-path="${escHtml(p)}" data-size="${g.sizes[fi]}" data-gtype="folder" onchange="updateBar()">
                <span class="path">${escHtml(p)}${label}</span>
                <span class="meta">${sz}</span>
            </div>`;
        });
        html += '</div></div>';
    });
    el.innerHTML = html;
}

function renderFiles() {
    const el = document.getElementById('filesTab');
    if (!results.dup_files.length) {
        el.innerHTML = '<div class="empty">No duplicate files found &#127881;</div>';
        return;
    }
    let html = '';
    results.dup_files.forEach((g, gi) => {
        const waste = humanSize(g.sizes[0] * (g.paths.length - 1));
        html += `<div class="group" data-gtype="file" data-gidx="${gi}">
            <div class="group-head" onclick="toggleGroup(this)">
                <div class="left">
                    <span class="chevron">&#9654;</span>
                    <strong>Group ${gi+1}</strong>
                    <span class="tag ext">${g.ext}</span>
                    <span class="tag count">${g.paths.length} copies</span>
                </div>
                <div class="right">
                    <span class="tag waste">${waste}</span>
                </div>
            </div>
            <div class="group-body">`;
        g.paths.forEach((p, fi) => {
            const sz = humanSize(g.sizes[fi]);
            const mt = g.mod_times_str ? g.mod_times_str[fi] : '';
            const keep = fi === 0 ? ' keep' : '';
            const label = fi === 0 ? ' <span style="color:#26de81;font-size:0.7rem">(KEEP)</span>' : '';
            html += `<div class="entry${keep}">
                <input type="checkbox" data-path="${escHtml(p)}" data-size="${g.sizes[fi]}" data-gtype="file" onchange="updateBar()">
                <span class="path">${escHtml(p)}${label}</span>
                <span class="meta">${sz} &middot; ${mt}</span>
            </div>`;
        });
        html += '</div></div>';
    });
    el.innerHTML = html;
}

function escHtml(s) {
    return s.replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// --- UI interactions ---
function toggleGroup(head) {
    head.closest('.group').classList.toggle('open');
}

function switchTab(tab, btn) {
    activeTab = tab;
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('foldersTab').style.display = tab === 'folders' ? 'block' : 'none';
    document.getElementById('filesTab').style.display = tab === 'files' ? 'block' : 'none';
}

function autoSelect() {
    const tabId = activeTab === 'folders' ? 'foldersTab' : 'filesTab';
    document.querySelectorAll(`#${tabId} .group`).forEach(group => {
        const cbs = group.querySelectorAll('input[type=checkbox]');
        cbs.forEach((cb, i) => { cb.checked = i > 0; });
    });
    updateBar();
}

function clearAll() {
    document.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
    updateBar();
}

function expandAll() {
    const tabId = activeTab === 'folders' ? 'foldersTab' : 'filesTab';
    document.querySelectorAll(`#${tabId} .group`).forEach(g => g.classList.add('open'));
}

function collapseAll() {
    const tabId = activeTab === 'folders' ? 'foldersTab' : 'filesTab';
    document.querySelectorAll(`#${tabId} .group`).forEach(g => g.classList.remove('open'));
}

function filterGroups(query) {
    const q = query.toLowerCase();
    const tabId = activeTab === 'folders' ? 'foldersTab' : 'filesTab';
    document.querySelectorAll(`#${tabId} .group`).forEach(g => {
        const text = g.textContent.toLowerCase();
        g.style.display = text.includes(q) ? '' : 'none';
    });
}

function getSelected() {
    const checked = document.querySelectorAll('input[type=checkbox]:checked');
    const paths = [];
    let totalSize = 0;
    checked.forEach(cb => {
        paths.push(cb.dataset.path);
        totalSize += parseInt(cb.dataset.size || 0);
    });
    return { paths, totalSize, count: paths.length };
}

function updateBar() {
    const sel = getSelected();
    document.getElementById('selCount').textContent = sel.count + ' items';
    document.getElementById('selSize').textContent = humanSize(sel.totalSize);
    document.getElementById('deleteBtn').disabled = sel.count === 0;
    document.getElementById('exportBtn').disabled = sel.count === 0;
}

// --- Export ---
function exportList() {
    const sel = getSelected();
    if (!sel.count) return;
    const blob = new Blob([sel.paths.join('\\n')], { type: 'text/plain' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'deletion_list.txt';
    a.click();
}

// --- Delete flow ---
function showConfirm() {
    const sel = getSelected();
    if (!sel.count) return;
    pendingDeletePaths = sel.paths;
    document.getElementById('confirmText').textContent =
        `You are about to permanently delete ${sel.count} items (${humanSize(sel.totalSize)}). This cannot be undone.`;
    document.getElementById('confirmModal').classList.add('show');
    document.getElementById('delProgress').classList.remove('show');
    document.getElementById('modalBtns').style.display = 'flex';
}

function closeModal() {
    document.getElementById('confirmModal').classList.remove('show');
    pendingDeletePaths = [];
}

async function executeDelete() {
    if (!pendingDeletePaths.length) return;

    document.getElementById('confirmBtn').disabled = true;
    document.getElementById('delProgress').classList.add('show');
    document.getElementById('modalBtns').style.display = 'none';

    const BATCH = 50;
    let deleted = 0, failed = 0;
    const total = pendingDeletePaths.length;

    for (let i = 0; i < total; i += BATCH) {
        const batch = pendingDeletePaths.slice(i, i + BATCH);
        try {
            const res = await fetch('/api/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ paths: batch }),
            });
            const data = await res.json();
            deleted += data.deleted;
            failed += data.failed;
        } catch (e) {
            failed += batch.length;
        }
        const pct = Math.round(((i + batch.length) / total) * 100);
        document.getElementById('delBar').style.width = pct + '%';
        document.getElementById('delStatus').textContent =
            `Deleting... ${i + batch.length}/${total}`;
    }

    // Done
    document.getElementById('confirmModal').classList.remove('show');
    document.getElementById('confirmBtn').disabled = false;
    pendingDeletePaths = [];

    // Uncheck deleted items
    document.querySelectorAll('input[type=checkbox]:checked').forEach(cb => {
        cb.checked = false;
        cb.closest('.entry').style.opacity = '0.3';
        cb.disabled = true;
    });
    updateBar();

    showToast(`Deleted ${deleted} items` + (failed ? `, ${failed} failed` : ''), failed > 0);
}

function showToast(msg, isError) {
    const t = document.getElementById('toast');
    t.textContent = msg;
    t.className = 'toast show' + (isError ? ' error' : '');
    setTimeout(() => t.classList.remove('show'), 4000);
}
</script>
</body>
</html>'''


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description='Duplicate Scanner — Local Web App',
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('path', help='Root directory to scan')
    parser.add_argument('--port', '-p', type=int, default=8080, help='Port (default: 8080)')
    parser.add_argument('--min-size', type=int, default=1024, help='Min file size in bytes')
    parser.add_argument('--all-files', action='store_true', help='Scan all file types')
    parser.add_argument('--skip-folders', action='store_true', help='Skip folder scan')
    parser.add_argument('--skip-files', action='store_true', help='Skip file scan')

    args = parser.parse_args()

    root_dir = os.path.abspath(args.path)
    if not os.path.isdir(root_dir):
        print(f"Error: '{root_dir}' is not a valid directory.")
        sys.exit(1)

    scan_state['root_dir'] = root_dir
    scan_state['scan_all'] = args.all_files
    scan_state['min_size'] = args.min_size
    scan_state['skip_folders'] = args.skip_folders
    scan_state['skip_files'] = args.skip_files

    port = args.port
    server = HTTPServer(('127.0.0.1', port), DupScannerHandler)

    print("=" * 55)
    print("  🔍 Duplicate Scanner — Web App")
    print("=" * 55)
    print(f"  Target:  {root_dir}")
    print(f"  Mode:    {'All files' if args.all_files else 'Photos & Videos'}")
    print(f"  Server:  http://localhost:{port}")
    print("=" * 55)
    print("  Opening browser...")
    print("  Press Ctrl+C to stop.\n")

    webbrowser.open(f'http://localhost:{port}')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == '__main__':
    main()
