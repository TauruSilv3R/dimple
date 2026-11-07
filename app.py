import os
import sys
import time
import json
import uuid
import threading
import subprocess
import urllib.parse
import base64
import hashlib
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, make_response

# Lazy FFMPEG loader for instant startup
FFMPEG_PATH_CACHED = None
def get_ffmpeg_path():
    global FFMPEG_PATH_CACHED
    if FFMPEG_PATH_CACHED is None:
        try:
            import imageio_ffmpeg
            FFMPEG_PATH_CACHED = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            FFMPEG_PATH_CACHED = ""
    return FFMPEG_PATH_CACHED or None

# Workspace & Data directories - support both source and frozen PyInstaller standalone
if getattr(sys, 'frozen', False):
    BUNDLE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    DATA_DIR = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'Dimple')
else:
    BUNDLE_DIR = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = BUNDLE_DIR

BASE_DIR = BUNDLE_DIR

os.makedirs(DATA_DIR, exist_ok=True)

app = Flask(
    __name__,
    template_folder=os.path.join(BUNDLE_DIR, "templates"),
    static_folder=os.path.join(BUNDLE_DIR, "static")
)
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True

# Default API Token
DEFAULT_API_TOKEN = "27|qoIncR6oSuBDtVO2UzwTLeYKM1nig88Cogc43KbD"

DOWNLOAD_DIR = os.path.join(DATA_DIR, "downloads")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
JOBS_PATH = os.path.join(DATA_DIR, "jobs.json")
LICENSE_FILE = os.path.join(DATA_DIR, "license.json")
LICENSE_SECRET = "DIMPLE_PRO_OFFLINE_SECRET_2026_MASTER_SIGNATURE"
THUMBNAIL_CACHE_DIR = os.path.join(DATA_DIR, ".thumbnails")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(THUMBNAIL_CACHE_DIR, exist_ok=True)

def verify_license_key(key_str):
    if not key_str or not isinstance(key_str, str):
        return False
    parts = key_str.strip().upper().split('-')
    if len(parts) != 5 or parts[0] != 'DIMPLE':
        return False
    b1, b2, b3, b4 = parts[1], parts[2], parts[3], parts[4]
    expected = hashlib.sha256((LICENSE_SECRET + b1 + b2).encode('utf-8')).hexdigest().upper()
    return b3 == expected[:4] and b4 == expected[4:8]

def get_license_status():
    candidates = [LICENSE_FILE, os.path.join(BUNDLE_DIR, "license.json")]
    for candidate in candidates:
        if os.path.exists(candidate):
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    key = data.get('key', '')
                    if verify_license_key(key):
                        return {
                            "activated": True,
                            "key": key[:11] + "-****-****",
                            "full_key": key,
                            "type": "Lifetime Pro License",
                            "customer": data.get("customer", "Authorized User")
                        }
            except Exception:
                pass
    return {"activated": False, "key": "", "full_key": "", "type": "Activation Required", "customer": ""}

def get_or_create_thumbnail(video_path):
    if not video_path or not os.path.exists(video_path):
        return None
    try:
        file_hash = hashlib.md5(video_path.encode('utf-8', errors='ignore')).hexdigest()
        thumb_path = os.path.join(THUMBNAIL_CACHE_DIR, f"{file_hash}.jpg")
        
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            return thumb_path
            
        ffmpeg = get_ffmpeg_path()
        if ffmpeg and os.path.exists(ffmpeg):
            for seek_time in ['00:00:02', '00:00:00']:
                cmd = [
                    ffmpeg, '-y',
                    '-ss', seek_time,
                    '-i', video_path,
                    '-vframes', '1',
                    '-vf', 'scale=480:-1',
                    '-q:v', '3',
                    thumb_path
                ]
                subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
                if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
                    return thumb_path
    except Exception:
        pass
    return None

# Jobs lock for thread-safe mutations (RLock to allow reentrant locking)
jobs_lock = threading.RLock()

def load_config():
    cfg = {"token": DEFAULT_API_TOKEN, "download_dir": DOWNLOAD_DIR}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    cfg.update(saved)
        except Exception:
            pass
    return cfg

def save_config(data):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving config: {e}")

def get_token():
    cfg = load_config()
    return cfg.get("token", DEFAULT_API_TOKEN)

def get_default_download_dir():
    cfg = load_config()
    target = cfg.get("download_dir", DOWNLOAD_DIR)
    if not target:
        target = DOWNLOAD_DIR
    target = os.path.normpath(target)
    try:
        os.makedirs(target, exist_ok=True)
    except Exception:
        target = DOWNLOAD_DIR
    return target

def load_persisted_jobs():
    if os.path.exists(JOBS_PATH):
        try:
            with open(JOBS_PATH, "r", encoding="utf-8") as f:
                saved = json.load(f)
                if isinstance(saved, dict):
                    # Mark any previously 'downloading' jobs as paused/interrupted on startup
                    for j in saved.values():
                        if j.get('status') in ['downloading', 'queued', 'processing', 'cancelling']:
                            j['status'] = 'interrupted'
                            j['speed'] = ''
                            j['eta'] = ''
                    return saved
        except Exception as e:
            print(f"Error loading jobs: {e}")
    return {}

# In-memory download jobs
jobs = load_persisted_jobs()

def persist_jobs():
    with jobs_lock:
        try:
            with open(JOBS_PATH, "w", encoding="utf-8") as f:
                json.dump(jobs, f, indent=2)
        except Exception as e:
            print(f"Error saving jobs: {e}")

def get_system_presets():
    home = Path.home()
    presets = [
        {
            "id": "project",
            "name": "Project Folder",
            "path": os.path.normpath(DOWNLOAD_DIR),
            "short": "./downloads",
            "icon": "folder"
        },
        {
            "id": "downloads",
            "name": "PC Downloads",
            "path": os.path.normpath(str(home / "Downloads")),
            "short": "Downloads",
            "icon": "download"
        },
        {
            "id": "desktop",
            "name": "Desktop",
            "path": os.path.normpath(str(home / "Desktop")),
            "short": "Desktop",
            "icon": "desktop_windows"
        },
        {
            "id": "videos",
            "name": "Videos",
            "path": os.path.normpath(str(home / "Videos")),
            "short": "Videos",
            "icon": "movie"
        }
    ]
    return presets

def pick_folder_dialog(initial_dir=""):
    init_target = initial_dir if (initial_dir and os.path.exists(initial_dir)) else get_default_download_dir()
    init_target = os.path.normpath(init_target)

    # 1. Tkinter in-process dialog with topmost focus (fast, native, works across all platforms and frozen bundles)
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes('-topmost', 1)
        root.lift()
        root.focus_force()
        folder = filedialog.askdirectory(parent=root, initialdir=init_target, title="Select Download Destination Folder")
        root.destroy()
        if folder and os.path.exists(folder):
            return os.path.normpath(folder)
        if folder == "":
            return None # Explicit cancel
    except Exception as e:
        pass

    # 2. Native Windows Shell COM fallback
    try:
        ps_code = f'$app = New-Object -ComObject Shell.Application; $f = $app.BrowseForFolder(0, "Select Download Destination Folder", 0, "{init_target}"); if ($f) {{ Write-Output $f.Self.Path }}'
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0 # SW_HIDE
        proc = subprocess.run(["powershell", "-NoProfile", "-Command", ps_code], capture_output=True, text=True, timeout=120, startupinfo=si)
        res = proc.stdout.strip()
        if res and os.path.exists(res):
            return os.path.normpath(res)
    except Exception:
        pass

    return None

def get_ydl_base_opts(extra_extractor_args=None):
    cfg = load_config()
    opts = {
        'quiet': True,
        'no_warnings': True,
    }
    ffmpeg = get_ffmpeg_path()
    if ffmpeg and os.path.exists(ffmpeg):
        opts['ffmpeg_location'] = ffmpeg

    # Enable Node.js for solving YouTube JS challenge (EJS)
    opts['js_runtimes'] = {'node': {}}

    # Check for cookies.txt file
    cookies_file = cfg.get('cookies_file')
    if cookies_file and os.path.exists(cookies_file):
        opts['cookiefile'] = os.path.normpath(cookies_file)
    else:
        auto_cookies = os.path.join(BASE_DIR, 'cookies.txt')
        if os.path.exists(auto_cookies):
            opts['cookiefile'] = auto_cookies

    # Optional browser cookies if configured and no cookiefile
    browser = cfg.get('cookies_browser', 'none').lower()
    if browser in ['edge', 'chrome', 'firefox', 'brave'] and 'cookiefile' not in opts:
        try:
            opts['cookiesfrombrowser'] = (browser,)
        except Exception:
            pass

    if extra_extractor_args:
        opts['extractor_args'] = extra_extractor_args

    return opts

def format_duration(seconds):
    if not seconds:
        return "00:00"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"

def format_bytes(size):
    if not size:
        return "0 B"
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"

@app.before_request
def restrict_to_desktop():
    expected_token = os.environ.get('DIMPLE_DESKTOP_TOKEN')
    if not expected_token:
        return None
    # Always allow static files, thumbnails, media streams, clipboard and health check
    if (request.path.startswith('/static/') or 
        request.path.startswith('/api/thumbnail') or 
        request.path.startswith('/api/stream') or 
        request.path in ['/api/health', '/favicon.ico', '/api/clipboard']):
        return None
    token = (
        request.args.get('token') or
        request.cookies.get('dimple_desktop_token') or
        request.headers.get('X-Dimple-Token')
    )
    if token == expected_token:
        return None
    # Block external localhost browser access completely
    return (
        "<!DOCTYPE html><html><head><title>Dimple - Desktop Application</title>"
        "<style>body{background:#212529;color:#f8f9fa;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;text-align:center;padding:60px 20px;}"
        "h1{color:#dee2e6;font-size:22px;margin-bottom:10px;}p{color:#adb5bd;max-width:480px;margin:10px auto;font-size:14px;line-height:1.6;}"
        ".box{background:#343a40;border:1px solid #495057;border-radius:12px;padding:32px;display:inline-block;box-shadow:0 8px 32px rgba(0,0,0,0.4);}"
        "</style></head><body>"
        "<div class='box'>"
        "<h1>Dimple — Desktop Mode Active</h1>"
        "<p>Direct web browser access via localhost has been disabled.</p>"
        "<p style='color:#6c757d;font-size:12px;'>Please open Dimple using your desktop application shortcut.</p>"
        "</div></body></html>",
        403
    )

@app.route('/api/health')
def health_endpoint():
    return jsonify({"status": "ok", "app": "Dimple"})

@app.route('/')
def index():
    active_dir = get_default_download_dir()
    expected_token = os.environ.get('DIMPLE_DESKTOP_TOKEN', '')
    resp = make_response(render_template('index.html', download_dir=active_dir, desktop_token=expected_token))
    if expected_token:
        resp.set_cookie('dimple_desktop_token', expected_token, httponly=True, samesite='Strict')
    return resp

@app.route('/api/system-presets', methods=['GET'])
def list_presets():
    return jsonify({
        "success": True,
        "presets": get_system_presets(),
        "default_dir": get_default_download_dir()
    })

@app.after_request
def add_no_cache_headers(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    expected_token = os.environ.get('DIMPLE_DESKTOP_TOKEN')
    if expected_token and (request.args.get('token') == expected_token or request.cookies.get('dimple_desktop_token') == expected_token):
        response.set_cookie('dimple_desktop_token', expected_token, path='/', samesite='Lax')
    return response

@app.route('/api/browse-folder', methods=['POST'])
def browse_folder():
    data = request.get_json() or {}
    initial_dir = data.get('initial_dir') or get_default_download_dir()
    selected = pick_folder_dialog(initial_dir)
    if selected:
        selected = os.path.normpath(selected)
        try:
            os.makedirs(selected, exist_ok=True)
            cfg = load_config()
            cfg['download_dir'] = selected
            save_config(cfg)
        except Exception as e:
            print(f"Error saving selected folder: {e}")
        return jsonify({"success": True, "path": selected})
    return jsonify({"success": False, "cancelled": True})

@app.route('/api/info', methods=['POST'])
def fetch_info():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    if not url:
        return jsonify({"success": False, "error": "Please provide a valid video URL."}), 400

    try:
        import yt_dlp
        ydl_opts = get_ydl_base_opts()
        info = None
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as initial_err:
            err_msg = str(initial_err)
            if any(k in err_msg.lower() for k in ['bot', 'sign in', 'confirm you']):
                fallback_opts = get_ydl_base_opts(extra_extractor_args={'youtube': {'player_client': ['android', 'ios', 'web']}})
                with yt_dlp.YoutubeDL(fallback_opts) as fallback_ydl:
                    info = fallback_ydl.extract_info(url, download=False)
            else:
                raise initial_err

        if not info:
            return jsonify({"success": False, "error": "Could not extract video metadata."}), 404

        formats = info.get('formats', [])
        available_heights = set()
        for f in formats:
            h = f.get('height')
            vcodec = f.get('vcodec')
            if h and (not vcodec or vcodec != 'none'):
                available_heights.add(h)

        sorted_heights = sorted(list(available_heights), reverse=True)

        qualities = []
        qualities.append({"id": "best", "label": "Best Available", "badge": "Auto HD+"})

        for h in [2160, 1440, 1080, 720, 480, 360]:
            if any(height >= h for height in sorted_heights):
                label = f"{h}p 4K" if h == 2160 else f"{h}p QHD" if h == 1440 else f"{h}p Full HD" if h == 1080 else f"{h}p HD" if h == 720 else f"{h}p"
                qualities.append({"id": str(h), "label": label, "badge": "MP4"})

        qualities.append({"id": "audio_320", "label": "MP3 Audio (320 kbps)", "badge": "MP3"})
        qualities.append({"id": "audio_128", "label": "MP3 Audio (128 kbps)", "badge": "MP3"})

        return jsonify({
            "success": True,
            "data": {
                "id": info.get('id'),
                "title": info.get('title') or "Video",
                "channel": info.get('uploader') or info.get('channel') or info.get('creator') or info.get('extractor_key') or "Web Media",
                "site": info.get('extractor_key') or "Web",
                "thumbnail": info.get('thumbnail'),
                "duration": format_duration(info.get('duration')) if info.get('duration') else None,
                "views": f"{info.get('view_count', 0):,}" if info.get('view_count') else None,
                "qualities": qualities
            }
        })
    except Exception as e:
        err_str = str(e)
        if any(k in err_str.lower() for k in ['bot', 'sign in', 'confirm you']):
            err_str = "YouTube requested sign-in confirmation. Try enabling browser cookies in Settings, or place a cookies.txt file in the Dimple folder."
        return jsonify({"success": False, "error": err_str}), 500

def run_download_job(job_id, url, quality, target_dir):
    def progress_hook(d):
        # Check cancellation request
        if jobs.get(job_id, {}).get('cancel_requested'):
            raise Exception("Download cancelled by user.")

        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total > 0 else 0
            speed = d.get('speed', 0)
            speed_str = f"{speed / 1024 / 1024:.1f} MB/s" if speed else ""
            eta = d.get('eta')
            eta_str = f"{eta}s" if eta else ""
            
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id].update({
                        'status': 'downloading',
                        'progress': round(percent, 1),
                        'downloaded_bytes': downloaded,
                        'total_bytes': total,
                        'downloaded_str': format_bytes(downloaded),
                        'total_str': format_bytes(total),
                        'speed': speed_str,
                        'eta': eta_str
                    })
        elif d['status'] == 'finished':
            with jobs_lock:
                if job_id in jobs:
                    jobs[job_id].update({
                        'status': 'processing',
                        'progress': 99.0,
                        'speed': 'Merging streams...',
                        'eta': ''
                    })

    try:
        import yt_dlp
        os.makedirs(target_dir, exist_ok=True)
        ydl_opts = get_ydl_base_opts()
        output_template = os.path.join(target_dir, '%(title)s [%(id)s].%(ext)s')
        ydl_opts.update({
            'outtmpl': output_template,
            'progress_hooks': [progress_hook],
            'noplaylist': True,
        })

        if quality == 'audio_320':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '320',
                }]
            })
        elif quality == 'audio_128':
            ydl_opts.update({
                'format': 'bestaudio/best',
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '128',
                }]
            })
        elif quality == 'best':
            ydl_opts.update({
                'format': 'bestvideo*+bestaudio/best',
                'merge_output_format': 'mp4'
            })
        else:
            h = int(quality)
            ydl_opts.update({
                'format': f'bestvideo*[height<={h}]+bestaudio/best[height<={h}]/best',
                'merge_output_format': 'mp4'
            })

        def execute_ydl_download(options):
            with yt_dlp.YoutubeDL(options) as ydl:
                inf = ydl.extract_info(url, download=True)
                fn = ydl.prepare_filename(inf)
                if quality.startswith('audio_'):
                    fn = os.path.splitext(fn)[0] + '.mp3'
                return inf, fn

        try:
            info, filename = execute_ydl_download(ydl_opts)
        except Exception as dl_err:
            err_text = str(dl_err)
            if any(k in err_text.lower() for k in ['bot', 'sign in', 'confirm you']):
                fallback_opts = get_ydl_base_opts(extra_extractor_args={'youtube': {'player_client': ['android', 'ios', 'web']}})
                # Copy over outtmpl, progress_hooks, formats
                for k, v in ydl_opts.items():
                    if k not in ['extractor_args']:
                        fallback_opts[k] = v
                info, filename = execute_ydl_download(fallback_opts)
            else:
                raise dl_err
            
        with jobs_lock:
            if job_id in jobs:
                thumb_url = info.get('thumbnail') or (info.get('thumbnails') or [{}])[-1].get('url', '')
                dur_str = format_duration(info.get('duration')) if info.get('duration') else ''
                jobs[job_id].update({
                    'status': 'completed',
                    'progress': 100.0,
                    'filename': os.path.basename(filename),
                    'filepath': os.path.abspath(filename),
                    'target_dir': target_dir,
                    'folder_name': os.path.basename(target_dir) or target_dir,
                    'title': info.get('title') or jobs[job_id].get('title'),
                    'thumbnail': jobs[job_id].get('thumbnail') or thumb_url,
                    'duration': jobs[job_id].get('duration') or dur_str,
                    'channel': jobs[job_id].get('channel') or info.get('uploader') or info.get('channel') or '',
                    'completed_at': time.time(),
                    'speed': '',
                    'eta': ''
                })
        persist_jobs()

    except Exception as e:
        err_msg = str(e)
        if any(k in err_msg.lower() for k in ['bot', 'sign in', 'confirm you']):
            err_msg = "YouTube sign-in required. Add a cookies.txt file to the Dimple folder or configure browser cookies in Settings."

        with jobs_lock:
            if job_id in jobs:
                if jobs[job_id].get('cancel_requested'):
                    jobs[job_id].update({
                        'status': 'cancelled',
                        'progress': 0.0,
                        'speed': '',
                        'eta': '',
                        'error': 'Cancelled by user'
                    })
                else:
                    jobs[job_id].update({
                        'status': 'failed',
                        'speed': '',
                        'eta': '',
                        'error': err_msg
                    })
        persist_jobs()

@app.route('/api/download', methods=['POST'])
def start_download():
    data = request.get_json() or {}
    url = data.get('url', '').strip()
    quality = data.get('quality', 'best')
    quality_label = data.get('quality_label', quality)
    title = data.get('title') or 'Media Download'
    thumbnail = data.get('thumbnail') or ''
    duration = data.get('duration') or ''
    channel = data.get('channel') or ''
    custom_dir = (data.get('custom_dir') or data.get('dir') or '').strip()
    save_as_default = bool(data.get('save_as_default', False))

    if not url:
        return jsonify({"success": False, "error": "URL is required."}), 400

    target_dir = get_default_download_dir()
    if custom_dir:
        try:
            cand = os.path.abspath(os.path.normpath(custom_dir))
            os.makedirs(cand, exist_ok=True)
            target_dir = cand
            cfg = load_config()
            cfg["download_dir"] = target_dir
            save_config(cfg)
        except Exception as e:
            return jsonify({"success": False, "error": f"Invalid custom destination directory: {e}"}), 400

    if save_as_default:
        cfg = load_config()
        cfg["download_dir"] = target_dir
        save_config(cfg)

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            'id': job_id,
            'url': url,
            'quality': quality,
            'quality_label': quality_label,
            'title': title,
            'thumbnail': thumbnail,
            'duration': duration,
            'channel': channel,
            'status': 'queued',
            'progress': 0.0,
            'speed': '',
            'eta': '',
            'downloaded_bytes': 0,
            'total_bytes': 0,
            'downloaded_str': '0 B',
            'total_str': 'Calculating...',
            'filename': '',
            'filepath': '',
            'target_dir': target_dir,
            'folder_name': os.path.basename(target_dir) or target_dir,
            'error': '',
            'cancel_requested': False,
            'created_at': time.time(),
            'completed_at': None
        }
    persist_jobs()

    thread = threading.Thread(target=run_download_job, args=(job_id, url, quality, target_dir), daemon=True)
    thread.start()

    return jsonify({
        "success": True, 
        "job_id": job_id, 
        "target_dir": target_dir,
        "title": title
    })

@app.route('/api/jobs', methods=['GET'])
def list_jobs():
    with jobs_lock:
        job_list = sorted(jobs.values(), key=lambda j: j.get('created_at', 0), reverse=True)
        # Ensure completed jobs have thumbnail pointing to local file if available
        for j in job_list:
            fp = j.get('filepath')
            if fp and os.path.exists(fp):
                if not j.get('thumbnail') or j.get('thumbnail', '').startswith('http'):
                    encoded_fp = urllib.parse.quote(os.path.abspath(fp))
                    j['thumbnail'] = f"/api/thumbnail?path={encoded_fp}"
            elif j.get('filename') and j.get('target_dir'):
                alt_fp = os.path.join(j['target_dir'], j['filename'])
                if os.path.exists(alt_fp):
                    j['filepath'] = os.path.abspath(alt_fp)
                    if not j.get('thumbnail') or j.get('thumbnail', '').startswith('http'):
                        j['thumbnail'] = f"/api/thumbnail?path={urllib.parse.quote(j['filepath'])}"
    active_count = sum(1 for j in job_list if j.get('status') in ['queued', 'downloading', 'processing', 'cancelling'])
    return jsonify({
        "success": True,
        "jobs": job_list,
        "active_count": active_count,
        "total_count": len(job_list)
    })

@app.route('/api/status/<job_id>', methods=['GET'])
def get_job_status(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found."}), 404
    return jsonify({"success": True, "data": job})

@app.route('/api/cancel/<job_id>', methods=['POST'])
def cancel_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404
        if job.get('status') in ['downloading', 'queued', 'processing']:
            job['cancel_requested'] = True
            job['status'] = 'cancelling'
            persist_jobs()
            return jsonify({"success": True, "message": "Cancelling download..."})
        elif job.get('status') == 'interrupted':
            job['status'] = 'cancelled'
            persist_jobs()
            return jsonify({"success": True, "message": "Marked as cancelled."})
    return jsonify({"success": True, "message": "Job is not actively downloading."})

@app.route('/api/retry/<job_id>', methods=['POST'])
def retry_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404
        url = job.get('url')
        quality = job.get('quality', 'best')
        target_dir = job.get('target_dir') or get_default_download_dir()
        
        job['status'] = 'queued'
        job['progress'] = 0.0
        job['speed'] = ''
        job['eta'] = ''
        job['error'] = ''
        job['cancel_requested'] = False
        persist_jobs()

    thread = threading.Thread(target=run_download_job, args=(job_id, url, quality, target_dir), daemon=True)
    thread.start()
    return jsonify({"success": True, "job_id": job_id})

@app.route('/api/pause/<job_id>', methods=['POST'])
def pause_job(job_id):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404
        if job.get('status') in ['downloading', 'queued', 'processing']:
            job['cancel_requested'] = True
            job['status'] = 'paused'
            persist_jobs()
            return jsonify({"success": True, "message": "Download paused."})
        elif job.get('status') == 'paused':
            return retry_job(job_id)
    return jsonify({"success": True})

@app.route('/api/pause-all', methods=['POST'])
def pause_all_jobs():
    with jobs_lock:
        for job in jobs.values():
            if job.get('status') in ['downloading', 'queued', 'processing']:
                job['cancel_requested'] = True
                job['status'] = 'paused'
        persist_jobs()
    return jsonify({"success": True})

@app.route('/api/resume-all', methods=['POST'])
def resume_all_jobs():
    to_resume = []
    with jobs_lock:
        for jid, job in jobs.items():
            if job.get('status') in ['paused', 'cancelled', 'failed']:
                to_resume.append((jid, job.get('url'), job.get('quality', 'best'), job.get('target_dir') or get_default_download_dir()))
                job['status'] = 'queued'
                job['error'] = ''
                job['cancel_requested'] = False
        persist_jobs()
    for jid, url, quality, target_dir in to_resume:
        if url:
            threading.Thread(target=run_download_job, args=(jid, url, quality, target_dir), daemon=True).start()
    return jsonify({"success": True, "resumed": len(to_resume)})

@app.route('/api/clear-jobs', methods=['POST'])
@app.route('/api/clear-completed', methods=['POST'])
def clear_jobs():
    global jobs
    with jobs_lock:
        jobs = {jid: j for jid, j in jobs.items() if j.get('status') in ['downloading', 'queued', 'processing', 'cancelling']}
    persist_jobs()
    return jsonify({"success": True, "remaining": len(jobs)})

@app.route('/api/batch-delete', methods=['POST'])
def batch_delete_jobs():
    global jobs
    data = request.get_json() or {}
    job_ids = set(data.get('job_ids', []))
    with jobs_lock:
        for jid in list(jobs.keys()):
            if jid in job_ids:
                if jobs[jid].get('status') in ['downloading', 'processing']:
                    jobs[jid]['cancel_requested'] = True
                jobs.pop(jid, None)
    persist_jobs()
    return jsonify({"success": True, "remaining": len(jobs)})

@app.route('/api/remove-job/<job_id>', methods=['DELETE', 'POST'])
def remove_single_job(job_id):
    global jobs
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"success": False, "error": "Job not found"}), 404
        if job.get('status') in ['downloading', 'processing']:
            job['cancel_requested'] = True
        jobs.pop(job_id, None)
    persist_jobs()
    return jsonify({"success": True})

@app.route('/api/downloads-list', methods=['GET'])
def list_downloads():
    try:
        custom_target = request.args.get('dir')
        query = (request.args.get('q') or '').strip().lower()
        file_type = (request.args.get('type') or 'all').strip().lower()
        scan_dir = os.path.normpath(custom_target) if custom_target else get_default_download_dir()
        
        files = []
        if os.path.exists(scan_dir):
            for entry in sorted(os.scandir(scan_dir), key=lambda e: e.stat().st_mtime, reverse=True):
                if entry.is_file() and not entry.name.endswith(('.part', '.ytdl', '.temp')):
                    is_vid = entry.name.lower().endswith(('.mp4', '.mkv', '.webm', '.avi', '.mov'))
                    is_aud = entry.name.lower().endswith(('.mp3', '.m4a', '.wav', '.opus', '.flac', '.aac'))
                    
                    if file_type == 'video' and not is_vid:
                        continue
                    if file_type == 'audio' and not is_aud:
                        continue
                    if query and query not in entry.name.lower():
                        continue

                    stat = entry.stat()
                    encoded_abs_path = urllib.parse.quote(os.path.abspath(entry.path))
                    files.append({
                        "name": entry.name,
                        "size": format_bytes(stat.st_size),
                        "bytes": stat.st_size,
                        "raw_mtime": stat.st_mtime,
                        "mtime": time.strftime("%b %d, %H:%M", time.localtime(stat.st_mtime)),
                        "is_video": is_vid,
                        "is_audio": is_aud,
                        "filepath": entry.path,
                        "stream_url": f"/api/stream?path={encoded_abs_path}",
                        "thumbnail_url": f"/api/thumbnail?path={encoded_abs_path}"
                    })
        return jsonify({
            "success": True, 
            "current_dir": scan_dir, 
            "files": files, 
            "count": len(files)
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/stream', methods=['GET'])
def stream_file():
    raw_path = request.args.get('path', '').strip()
    filepath = urllib.parse.unquote(raw_path)
    if not filepath or not os.path.exists(filepath):
        name = request.args.get('name', '').strip()
        if name:
            filepath = os.path.join(get_default_download_dir(), urllib.parse.unquote(name))
    if not filepath or not os.path.exists(filepath):
        norm = os.path.normpath(filepath)
        if os.path.exists(norm):
            filepath = norm
        else:
            return "File not found", 404

    # Guess or provide explicit video/audio mimetype for reliable playback across all players
    ext = os.path.splitext(filepath)[1].lower()
    mime = None
    if ext == '.mp4':
        mime = 'video/mp4'
    elif ext == '.webm':
        mime = 'video/webm'
    elif ext == '.mkv':
        mime = 'video/x-matroska'
    elif ext == '.mp3':
        mime = 'audio/mpeg'
    elif ext == '.wav':
        mime = 'audio/wav'
    elif ext == '.m4a':
        mime = 'audio/mp4'
    elif ext == '.ogg':
        mime = 'audio/ogg'
    elif ext == '.flac':
        mime = 'audio/flac'

    if mime:
        return send_file(filepath, mimetype=mime, conditional=True)
    return send_file(filepath, conditional=True)

@app.route('/api/thumbnail', methods=['GET'])
def serve_thumbnail():
    raw_path = request.args.get('path', '').strip()
    filepath = urllib.parse.unquote(raw_path)
    if not filepath or not os.path.exists(filepath):
        name = request.args.get('name', '').strip()
        if name:
            filepath = os.path.join(get_default_download_dir(), urllib.parse.unquote(name))
    if not filepath or not os.path.exists(filepath):
        norm = os.path.normpath(filepath)
        if os.path.exists(norm):
            filepath = norm

    thumb = get_or_create_thumbnail(filepath)
    if thumb and os.path.exists(thumb):
        return send_file(thumb, mimetype='image/jpeg')

    logo_path = os.path.join(BASE_DIR, 'static', 'logo.png')
    if os.path.exists(logo_file := logo_path):
        return send_file(logo_file, mimetype='image/png')
    return "No thumbnail", 404

@app.route('/api/license', methods=['GET'])
def get_license():
    return jsonify({"success": True, "data": get_license_status()})

@app.route('/api/license/activate', methods=['POST'])
def activate_license():
    data = request.get_json() or {}
    key = (data.get('key') or '').strip().upper()
    customer = (data.get('customer') or 'Authorized User').strip()
    
    if verify_license_key(key):
        try:
            with open(LICENSE_FILE, 'w', encoding='utf-8') as f:
                json.dump({
                    "key": key,
                    "customer": customer,
                    "activated_at": time.time(),
                    "status": "active"
                }, f, indent=2)
            return jsonify({
                "success": True, 
                "message": "Dimple Studio Pro successfully activated!",
                "data": get_license_status()
            })
        except Exception as e:
            return jsonify({"success": False, "error": f"Failed to save license: {e}"}), 500
    else:
        return jsonify({
            "success": False, 
            "error": "Invalid license key format or signature. Please generate a valid key using Dimple_Keygen.exe."
        }), 400

@app.route('/api/delete', methods=['DELETE'])
def delete_file():
    try:
        data = request.get_json() or {}
        filepath = data.get('filepath') or request.args.get('filepath')
        if not filepath:
            filename = data.get('filename') or request.args.get('filename')
            if filename:
                filepath = os.path.join(get_default_download_dir(), filename)
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
            return jsonify({"success": True, "message": "File deleted."})
        return jsonify({"success": False, "error": "File not found."}), 404
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/open-downloads', methods=['POST'])
def open_downloads():
    try:
        data = request.get_json() or {}
        raw_target = data.get('path') or get_default_download_dir()
        if not raw_target or not isinstance(raw_target, str):
            raw_target = get_default_download_dir()
        target = os.path.normpath(raw_target.strip())
        if not os.path.exists(target):
            try:
                os.makedirs(target, exist_ok=True)
            except Exception:
                target = DOWNLOAD_DIR

        if sys.platform == 'win32':
            try:
                os.startfile(target)
            except Exception:
                subprocess.Popen(f'explorer.exe "{target}"', shell=True)
        else:
            subprocess.run(['xdg-open', target])
        return jsonify({"success": True, "path": target})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/reveal-file', methods=['POST'])
def reveal_file():
    data = request.get_json() or {}
    filepath = data.get('filepath')
    if not filepath:
        filename = data.get('filename')
        if filename:
            filepath = os.path.join(get_default_download_dir(), filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"success": False, "error": "File does not exist"}), 404
    try:
        if sys.platform == 'win32':
            subprocess.run(['explorer', f'/select,{os.path.normpath(filepath)}'])
        else:
            subprocess.run(['xdg-open', os.path.dirname(filepath)])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

def get_native_clipboard_text():
    """Reads text directly from the OS clipboard with 0 external dependencies."""
    if sys.platform == 'win32':
        try:
            import ctypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            
            user32.OpenClipboard.argtypes = [ctypes.c_void_p]
            user32.OpenClipboard.restype = ctypes.c_bool
            user32.CloseClipboard.restype = ctypes.c_bool
            user32.GetClipboardData.argtypes = [ctypes.c_uint]
            user32.GetClipboardData.restype = ctypes.c_void_p
            kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalLock.restype = ctypes.c_void_p
            kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
            kernel32.GlobalUnlock.restype = ctypes.c_bool

            CF_UNICODETEXT = 13
            CF_TEXT = 1
            if user32.OpenClipboard(None):
                try:
                    # 1. Try Unicode text (standard)
                    h_glb = user32.GetClipboardData(CF_UNICODETEXT)
                    if h_glb:
                        p_text = kernel32.GlobalLock(h_glb)
                        if p_text:
                            try:
                                val = ctypes.c_wchar_p(p_text).value
                                if val:
                                    return val
                            finally:
                                kernel32.GlobalUnlock(h_glb)
                    # 2. Try ANSI text fallback
                    h_glb = user32.GetClipboardData(CF_TEXT)
                    if h_glb:
                        p_text = kernel32.GlobalLock(h_glb)
                        if p_text:
                            try:
                                b_val = ctypes.c_char_p(p_text).value
                                if b_val:
                                    return b_val.decode('utf-8', errors='replace')
                            finally:
                                kernel32.GlobalUnlock(h_glb)
                finally:
                    user32.CloseClipboard()
        except Exception:
            pass
    try:
        import tkinter as tk
        r = tk.Tk()
        r.withdraw()
        text = r.clipboard_get()
        r.destroy()
        return text or ""
    except Exception:
        pass
    return ""

@app.route('/api/clipboard', methods=['GET'])
def get_clipboard_endpoint():
    text = get_native_clipboard_text()
    return jsonify({"success": True, "text": text})

@app.route('/api/open-file', methods=['POST'])
def open_file_externally():
    data = request.get_json() or {}
    filepath = data.get('filepath')
    if not filepath:
        filename = data.get('filename')
        if filename:
            filepath = os.path.join(get_default_download_dir(), filename)
    if not filepath or not os.path.exists(filepath):
        return jsonify({"success": False, "error": "File does not exist"}), 404
    try:
        if sys.platform == 'win32':
            os.startfile(os.path.normpath(filepath))
        else:
            subprocess.run(['xdg-open', filepath])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/settings', methods=['GET', 'POST'])
def manage_settings():
    config = load_config()
    if request.method == 'POST':
        data = request.get_json() or {}
        if 'token' in data:
            config['token'] = data['token'].strip()
        if 'download_dir' in data:
            new_dir = os.path.normpath(data['download_dir'].strip())
            if new_dir:
                try:
                    os.makedirs(new_dir, exist_ok=True)
                    config['download_dir'] = new_dir
                except Exception as e:
                    return jsonify({"success": False, "error": f"Cannot use directory: {e}"}), 400
        if 'cookies_browser' in data:
            config['cookies_browser'] = data['cookies_browser'].strip().lower()
        if 'cookies_file' in data:
            config['cookies_file'] = data['cookies_file'].strip()
        save_config(config)
        return jsonify({"success": True, "data": config})
    
    ytdlp_ver = "Installed"
    try:
        import yt_dlp.version
        ytdlp_ver = yt_dlp.version.__version__
    except Exception:
        pass

    auto_cookies_txt = os.path.join(BASE_DIR, 'cookies.txt')
    cookies_detected = os.path.exists(auto_cookies_txt) or (bool(config.get('cookies_file')) and os.path.exists(config.get('cookies_file', '')))

    return jsonify({
        "success": True,
        "data": {
            "token": config.get("token", DEFAULT_API_TOKEN),
            "download_dir": get_default_download_dir(),
            "presets": get_system_presets(),
            "ffmpeg": get_ffmpeg_path() or "Bundled imageio-ffmpeg",
            "ytdlp_version": ytdlp_ver,
            "cookies_browser": config.get("cookies_browser", "none"),
            "cookies_file": config.get("cookies_file", ""),
            "cookies_detected": cookies_detected,
            "js_runtime": "Node.js (Active)",
            "license": get_license_status()
        }
    })

def run_cli():
    import argparse
    parser = argparse.ArgumentParser(description="Dimple - Simple, Fast, Free YouTube Downloader")
    parser.add_argument("url", nargs="?", help="Video URL to download")
    parser.add_argument("--quality", "-q", default="best", help="Quality: best, 1080, 720, 480, 360, audio_320, audio_128")
    parser.add_argument("--dir", "-d", default=None, help="Custom destination folder path")
    parser.add_argument("--port", "-p", default=5000, type=int, help="Web UI port (default 5000)")
    parser.add_argument("--no-browser", action="store_true", help="Do not automatically open the browser")

    args = parser.parse_args()

    target_dir = os.path.normpath(args.dir) if args.dir else get_default_download_dir()

    if args.url:
        print(f"[*] Dimple Downloader: {args.url}")
        print(f"[*] Quality: {args.quality}")
        print(f"[*] Destination: {target_dir}")
        
        job_id = "cli_job"
        jobs[job_id] = {'status': 'queued'}
        run_download_job(job_id, args.url, args.quality, target_dir)
        job = jobs[job_id]
        if job['status'] == 'completed':
            print(f"\n[+] Download finished successfully!")
            print(f"[+] Saved to: {job['filepath']}")
        else:
            print(f"\n[-] Download failed: {job.get('error')}")
    else:
        # Launch native standalone desktop application directly
        try:
            from desktop_app import main as launch_desktop
            launch_desktop()
            return
        except Exception as e:
            # Fallback if desktop GUI cannot open
            app.run(host="127.0.0.1", port=args.port, debug=False)

if __name__ == '__main__':
    run_cli()
