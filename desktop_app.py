import os
import sys
import time
import socket
import secrets
import threading
import urllib.request
import logging

# Ensure sys.stdout and sys.stderr are safe when running under pythonw (windowless)
class NullWriter:
    def write(self, s): pass
    def flush(self): pass
    def isatty(self): return False

if sys.stdout is None:
    sys.stdout = NullWriter()
if sys.stderr is None:
    sys.stderr = NullWriter()

# Ensure Chromium uses DirectX 11 via ANGLE on Windows - prevents GLES3 context failure and blank screen
os.environ['QTWEBENGINE_CHROMIUM_FLAGS'] = (
    '--use-gl=angle '
    '--use-angle=d3d11 '
    '--no-sandbox '
    '--disable-features=TouchTextSelectionStrategy '
    '--enable-features=NetworkServiceInProcess '
    '--ignore-gpu-blocklist '
    '--disable-logging'
)

# Silence werkzeug logging
logging.getLogger('werkzeug').setLevel(logging.CRITICAL)

# Generate unique desktop session security token
SESSION_TOKEN = secrets.token_hex(24)
os.environ['DIMPLE_DESKTOP_TOKEN'] = SESSION_TOKEN

from PySide6.QtCore import QUrl, Qt, QCoreApplication, QTimer
from PySide6.QtGui import QIcon, QColor, QPalette, QAction, QKeySequence
from PySide6.QtWidgets import QApplication, QMainWindow, QMenu, QMenuBar
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import QWebEngineProfile, QWebEnginePage, QWebEngineSettings

# Path setup - supports both standard execution and PyInstaller frozen bundle
if getattr(sys, 'frozen', False):
    BASE_DIR = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ICON_ICO = os.path.join(BASE_DIR, "static", "app_icon.ico")
ICON_PNG = os.path.join(BASE_DIR, "static", "logo.png")

def find_free_port():
    """Finds an ephemeral free local port directly from the OS kernel."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]

def wait_for_server(port, timeout=5.0):
    """Polls internal health endpoint until it responds with HTTP 200."""
    start = time.time()
    url = f"http://127.0.0.1:{port}/api/health"
    while time.time() - start < timeout:
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'DimpleDesktop'})
            with urllib.request.urlopen(req, timeout=0.3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(0.04)
    return False

def start_backend(port):
    """Runs the Flask backend engine in an in-process background thread."""
    from app import app
    def run():
        import logging
        log = logging.getLogger('werkzeug')
        log.setLevel(logging.CRITICAL)
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return wait_for_server(port)

class DimpleYouTubeBrowserPage(QWebEnginePage):
    """Restricted web page enforcing YouTube-only browsing and catching video link triggers."""
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window

    def javaScriptConsoleMessage(self, level, msg, line, src):
        if msg.startswith('DIMPLE_SEND_CURRENT_VIDEO:'):
            url = msg.split('DIMPLE_SEND_CURRENT_VIDEO:')[1].strip()
            self.main_window.send_video_to_downloader(url)
            return

    def acceptNavigationRequest(self, url, nav_type, is_main_frame):
        if url.scheme() in ('about', 'data', 'blob', 'javascript'):
            return True
        host = url.host().lower()
        allowed = (
            'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
            'consent.youtube.com', 'youtu.be', 'accounts.google.com', 'myaccount.google.com'
        )
        if host in allowed or host.endswith('.youtube.com') or host.endswith('.googlevideo.com') or host.endswith('.ytimg.com') or host.endswith('.google.com') or host.endswith('.gstatic.com'):
            return True
        if is_main_frame:
            self.main_window.notify_restriction(host)
            return False
        return False

class DimpleAppWebEnginePage(QWebEnginePage):
    """Main application WebEnginePage that intercepts browser UI commands from index.html."""
    def __init__(self, main_window, parent=None):
        super().__init__(parent)
        self.main_window = main_window

    def javaScriptConsoleMessage(self, level, msg, line, src):
        if self.main_window and self.main_window.handle_js_command(msg):
            return

class DimpleWebEngineView(QWebEngineView):
    """Custom WebEngineView with clean, native dark context menu replacing generic browser menus."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.main_window = parent

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        menu.setStyleSheet("""
            QMenu {
                background-color: #14171b;
                color: #ffffff;
                font-family: 'Arial Black', 'Arial Bold', Gadget, sans-serif;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid #2b3036;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 28px 6px 14px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #ff6400;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #272a33;
                margin: 4px 6px;
            }
        """)

        act_reload = menu.addAction("Reload Page")
        act_reload.setShortcut(QKeySequence("Ctrl+R"))
        act_reload.triggered.connect(self.reload)

        act_back = menu.addAction("Back")
        act_back.setShortcut(QKeySequence("Alt+Left"))
        act_back.triggered.connect(self.back)
        act_back.setEnabled(self.history().canGoBack())

        act_forward = menu.addAction("Forward")
        act_forward.setShortcut(QKeySequence("Alt+Right"))
        act_forward.triggered.connect(self.forward)
        act_forward.setEnabled(self.history().canGoForward())

        menu.addSeparator()

        act_copy = menu.addAction("Copy")
        act_copy.setShortcut(QKeySequence.Copy)
        act_copy.triggered.connect(lambda: self.triggerPageAction(QWebEnginePage.WebAction.Copy))

        act_paste = menu.addAction("Paste")
        act_paste.setShortcut(QKeySequence.Paste)
        act_paste.triggered.connect(lambda: self.triggerPageAction(QWebEnginePage.WebAction.Paste))

        act_select_all = menu.addAction("Select All")
        act_select_all.setShortcut(QKeySequence.SelectAll)
        act_select_all.triggered.connect(lambda: self.triggerPageAction(QWebEnginePage.WebAction.SelectAll))

        menu.addSeparator()

        if self.main_window:
            act_browser = menu.addAction("Go to In-App Browser")
            act_browser.triggered.connect(lambda: self.main_window.switch_view_tab('browser'))

            act_open_dir = menu.addAction("Open Downloads Folder")
            act_open_dir.triggered.connect(self.main_window.action_open_downloads_folder)

            act_media = menu.addAction("Go to Media Library")
            act_media.triggered.connect(lambda: self.main_window.switch_view_tab('downloads'))

        menu.exec(event.globalPos())

class DimpleMainWindow(QMainWindow):
    def __init__(self, port, token):
        super().__init__()
        self.port = port
        self.token = token
        self.setWindowTitle("Dimple")
        self.resize(1360, 880)
        self.setMinimumSize(1020, 680)

        # Set Obsidian Charcoal background (#0e1013) to match palette immediately
        self.setStyleSheet("background-color: #0e1013;")
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Window, QColor(14, 16, 19))
        self.setPalette(palette)

        # Set Official 3D App Icon
        if os.path.exists(ICON_ICO):
            self.setWindowIcon(QIcon(ICON_ICO))
        elif os.path.exists(ICON_PNG):
            self.setWindowIcon(QIcon(ICON_PNG))

        # Central Web Engine View
        self.browser = DimpleWebEngineView(self)
        self.browser.setStyleSheet("background-color: #0e1013;")
        
        self.app_page = DimpleAppWebEnginePage(self)
        self.browser.setPage(self.app_page)

        profile = self.browser.page().profile()
        profile.setHttpCacheType(QWebEngineProfile.HttpCacheType.NoCache)
        self.browser.page().setBackgroundColor(QColor(14, 16, 19))

        # Enable seamless clipboard reading/writing in embedded Chromium
        settings = self.browser.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanPaste, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.browser.page().featurePermissionRequested.connect(self.on_feature_permission_requested)

        # Connect load state handlers
        self.browser.loadFinished.connect(self.on_load_finished)
        self.browser.renderProcessTerminated.connect(self.on_render_terminated)

        self.setCentralWidget(self.browser)
        self.app_url = f"http://127.0.0.1:{self.port}/?token={self.token}"

        # In-App YouTube Browser Widget
        self.browser_tab_active = False
        self.current_browser_video_url = ""
        self.init_inapp_browser()

        # Build native Windows Menu Bar (File and Options)
        self.setup_menus()

    def on_feature_permission_requested(self, securityOrigin, feature):
        self.browser.page().setFeaturePermission(securityOrigin, feature, QWebEnginePage.PermissionPolicy.PermissionGrantedByUser)

    def setup_menus(self):
        menubar = self.menuBar()
        menubar.setStyleSheet("""
            QMenuBar {
                background-color: #14171b;
                color: #ffffff;
                font-family: 'Arial Black', 'Arial Bold', Gadget, sans-serif;
                font-size: 11px;
                font-weight: bold;
                border-bottom: 1px solid #22262c;
                padding: 3px 6px;
            }
            QMenuBar::item {
                background: transparent;
                padding: 5px 12px;
                border-radius: 4px;
            }
            QMenuBar::item:selected {
                background-color: #ff6400;
                color: #ffffff;
            }
            QMenu {
                background-color: #14171b;
                color: #ffffff;
                font-family: 'Arial Black', 'Arial Bold', Gadget, sans-serif;
                font-size: 11px;
                font-weight: bold;
                border: 1px solid #2b3036;
                padding: 4px;
            }
            QMenu::item {
                padding: 6px 28px 6px 14px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #ff6400;
                color: #ffffff;
            }
            QMenu::separator {
                height: 1px;
                background-color: #272a33;
                margin: 4px 6px;
            }
        """)

        # ----------------------------------------------------
        # 1. FILE MENU (Includes all Right-Click operations)
        # ----------------------------------------------------
        file_menu = menubar.addMenu("&File")

        act_open_folder = QAction("Open Downloads Folder", self)
        act_open_folder.setShortcut(QKeySequence("Ctrl+O"))
        act_open_folder.triggered.connect(self.action_open_downloads_folder)
        file_menu.addAction(act_open_folder)

        act_change_dir = QAction("Change Destination Folder...", self)
        act_change_dir.setShortcut(QKeySequence("Ctrl+Shift+O"))
        act_change_dir.triggered.connect(self.action_select_destination_folder)
        file_menu.addAction(act_change_dir)

        file_menu.addSeparator()

        # Right-Click Operations integrated into File Menu
        act_reload = QAction("Reload Page", self)
        act_reload.setShortcut(QKeySequence("Ctrl+R"))
        act_reload.triggered.connect(self.browser.reload)
        file_menu.addAction(act_reload)

        act_back = QAction("Back", self)
        act_back.setShortcut(QKeySequence("Alt+Left"))
        act_back.triggered.connect(self.browser.back)
        file_menu.addAction(act_back)

        act_forward = QAction("Forward", self)
        act_forward.setShortcut(QKeySequence("Alt+Right"))
        act_forward.triggered.connect(self.browser.forward)
        file_menu.addAction(act_forward)

        file_menu.addSeparator()

        act_copy = QAction("Copy", self)
        act_copy.setShortcut(QKeySequence.Copy)
        act_copy.triggered.connect(lambda: self.browser.triggerPageAction(QWebEnginePage.WebAction.Copy))
        file_menu.addAction(act_copy)

        act_paste = QAction("Paste", self)
        act_paste.setShortcut(QKeySequence.Paste)
        act_paste.triggered.connect(lambda: self.browser.triggerPageAction(QWebEnginePage.WebAction.Paste))
        file_menu.addAction(act_paste)

        act_select_all = QAction("Select All", self)
        act_select_all.setShortcut(QKeySequence.SelectAll)
        act_select_all.triggered.connect(lambda: self.browser.triggerPageAction(QWebEnginePage.WebAction.SelectAll))
        file_menu.addAction(act_select_all)

        file_menu.addSeparator()

        act_exit = QAction("Exit", self)
        act_exit.setShortcut(QKeySequence("Alt+F4"))
        act_exit.triggered.connect(self.close)
        file_menu.addAction(act_exit)

        # ----------------------------------------------------
        # 2. OPTIONS MENU
        # ----------------------------------------------------
        opt_menu = menubar.addMenu("&Options")

        act_tab_downloader = QAction("Downloader Studio", self)
        act_tab_downloader.setShortcut(QKeySequence("Ctrl+1"))
        act_tab_downloader.triggered.connect(lambda: self.switch_view_tab('downloader'))
        opt_menu.addAction(act_tab_downloader)

        act_tab_browser = QAction("YouTube Browser", self)
        act_tab_browser.setShortcut(QKeySequence("Ctrl+2"))
        act_tab_browser.triggered.connect(lambda: self.switch_view_tab('browser'))
        opt_menu.addAction(act_tab_browser)

        act_tab_transfers = QAction("Active Transfers", self)
        act_tab_transfers.setShortcut(QKeySequence("Ctrl+3"))
        act_tab_transfers.triggered.connect(lambda: self.switch_view_tab('transfers'))
        opt_menu.addAction(act_tab_transfers)

        act_tab_media = QAction("Media Library", self)
        act_tab_media.setShortcut(QKeySequence("Ctrl+4"))
        act_tab_media.triggered.connect(lambda: self.switch_view_tab('downloads'))
        opt_menu.addAction(act_tab_media)

        act_tab_settings = QAction("Settings", self)
        act_tab_settings.setShortcut(QKeySequence("Ctrl+,"))
        act_tab_settings.triggered.connect(lambda: self.switch_view_tab('settings'))
        opt_menu.addAction(act_tab_settings)

        opt_menu.addSeparator()

        act_zoom_in = QAction("Zoom In", self)
        act_zoom_in.setShortcut(QKeySequence("Ctrl+="))
        act_zoom_in.triggered.connect(lambda: self.browser.setZoomFactor(min(2.0, self.browser.zoomFactor() + 0.1)))
        opt_menu.addAction(act_zoom_in)

        act_zoom_out = QAction("Zoom Out", self)
        act_zoom_out.setShortcut(QKeySequence("Ctrl+-"))
        act_zoom_out.triggered.connect(lambda: self.browser.setZoomFactor(max(0.6, self.browser.zoomFactor() - 0.1)))
        opt_menu.addAction(act_zoom_out)

        act_zoom_reset = QAction("Reset Zoom (100%)", self)
        act_zoom_reset.setShortcut(QKeySequence("Ctrl+0"))
        act_zoom_reset.triggered.connect(lambda: self.browser.setZoomFactor(1.0))
        opt_menu.addAction(act_zoom_reset)

        opt_menu.addSeparator()

        act_fullscreen = QAction("Toggle Fullscreen", self)
        act_fullscreen.setShortcut(QKeySequence("F11"))
        act_fullscreen.triggered.connect(self.toggle_fullscreen)
        opt_menu.addAction(act_fullscreen)

    def action_open_downloads_folder(self):
        try:
            from app import get_default_download_dir
            folder = get_default_download_dir()
            if os.path.exists(folder):
                os.startfile(folder)
        except Exception as e:
            print("Error opening folder:", e)

    def action_select_destination_folder(self):
        try:
            import json
            from app import pick_folder_dialog, load_config, save_config, get_default_download_dir
            folder = pick_folder_dialog(get_default_download_dir())
            if folder:
                cfg = load_config()
                cfg['download_dir'] = folder
                save_config(cfg)
                self.browser.page().runJavaScript(f"saveDownloaderPath({json.dumps(folder)})")
        except Exception as e:
            print("Error selecting folder:", e)

    def switch_view_tab(self, tab_id):
        self.browser.page().runJavaScript(f"switchTab('{tab_id}')")

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def init_inapp_browser(self):
        self.inapp_page = DimpleYouTubeBrowserPage(self)
        self.inapp_browser = DimpleWebEngineView(self.browser)
        self.inapp_browser.setPage(self.inapp_page)
        self.inapp_browser.setStyleSheet("background-color: #0e1013; border: none; border-radius: 0px;")

        settings = self.inapp_browser.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.FullScreenSupportEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ScrollAnimatorEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.Accelerated2dCanvasEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.AutoLoadImages, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)

        self.inapp_browser.page().fullScreenRequested.connect(lambda req: req.accept())
        self.inapp_browser.urlChanged.connect(self.on_inapp_browser_url_changed)
        self.inapp_browser.loadFinished.connect(self.on_inapp_browser_load_finished)

        self.inapp_browser.setUrl(QUrl("https://www.youtube.com"))
        self.inapp_browser.hide()

    def inject_youtube_downloader_button(self):
        script = """
        (function() {
            if (window._dimpleInjected) return;
            window._dimpleInjected = true;
            function checkAndInject() {
                var isWatch = location.pathname.includes('/watch') || location.pathname.includes('/shorts/');
                var existing = document.getElementById('dimple-floating-sender');
                if (!isWatch) {
                    if (existing) existing.remove();
                    return;
                }
                if (existing) return;
                var btn = document.createElement('div');
                btn.id = 'dimple-floating-sender';
                btn.setAttribute('title', 'Send to Dimple Downloader Studio');
                btn.style.cssText = 'position:fixed; bottom:28px; right:28px; z-index:2147483647; display:flex; align-items:center; gap:8px; background:linear-gradient(135deg, #ff7a00, #ff4500); color:#ffffff; font-family:Arial Black, Gadget, sans-serif; font-size:12px; font-weight:bold; padding:10px 18px; border-radius:9999px; box-shadow:0 8px 24px rgba(255,100,0,0.65), 0 0 0 1px rgba(255,255,255,0.25); cursor:pointer; transition:all 0.2s ease; user-select:none;';
                btn.innerHTML = '<span style="font-size:16px;">⚡</span><span>Send to Downloader</span>';
                btn.onmouseenter = function() { btn.style.transform = 'scale(1.06)'; btn.style.boxShadow = '0 10px 28px rgba(255,100,0,0.85), 0 0 0 2px #ffffff'; };
                btn.onmouseleave = function() { btn.style.transform = 'scale(1)'; btn.style.boxShadow = '0 8px 24px rgba(255,100,0,0.65), 0 0 0 1px rgba(255,255,255,0.25)'; };
                btn.onclick = function(e) {
                    e.stopPropagation();
                    e.preventDefault();
                    console.log('DIMPLE_SEND_CURRENT_VIDEO:' + window.location.href);
                };
                document.body.appendChild(btn);
            }
            setInterval(checkAndInject, 1000);
            checkAndInject();
        })();
        """
        if hasattr(self, 'inapp_browser') and self.inapp_browser:
            self.inapp_browser.page().runJavaScript(script)

    def on_inapp_browser_url_changed(self, qurl):
        import json, urllib.parse
        url_str = qurl.toString()
        self.current_browser_video_url = url_str
        is_video = False
        video_id = ""

        if "youtube.com/watch" in url_str:
            is_video = True
            try:
                parsed = urllib.parse.urlparse(url_str)
                qs = urllib.parse.parse_qs(parsed.query)
                video_id = qs.get("v", [""])[0]
            except Exception:
                pass
        elif "youtube.com/shorts/" in url_str:
            is_video = True
            parts = url_str.split("/shorts/")
            if len(parts) > 1:
                video_id = parts[1].split("?")[0].split("/")[0]
        elif "youtu.be/" in url_str:
            is_video = True
            parts = url_str.split("youtu.be/")
            if len(parts) > 1:
                video_id = parts[1].split("?")[0].split("/")[0]

        can_back = self.inapp_browser.history().canGoBack()
        can_forward = self.inapp_browser.history().canGoForward()

        js = f"if (typeof onBrowserUrlChanged === 'function') onBrowserUrlChanged({json.dumps(url_str)}, {json.dumps(is_video)}, {json.dumps(video_id)}, {json.dumps(can_back)}, {json.dumps(can_forward)});"
        self.browser.page().runJavaScript(js)

        if is_video:
            self.inject_youtube_downloader_button()

    def on_inapp_browser_load_finished(self, ok):
        if ok:
            self.inject_youtube_downloader_button()

    def handle_js_command(self, msg):
        import json
        if not isinstance(msg, str):
            return False
        if msg.startswith('DIMPLE_BROWSER_GEO:'):
            try:
                geo = json.loads(msg[len('DIMPLE_BROWSER_GEO:'):])
                self.update_browser_geometry(geo.get('x', 0), geo.get('y', 0), geo.get('width', 0), geo.get('height', 0))
            except Exception as e:
                pass
            return True
        elif msg == 'DIMPLE_BROWSER_SHOW':
            self.browser_tab_active = True
            if hasattr(self, 'inapp_browser') and self.inapp_browser:
                self.inapp_browser.show()
                self.inapp_browser.raise_()
            return True
        elif msg == 'DIMPLE_BROWSER_HIDE':
            self.browser_tab_active = False
            if hasattr(self, 'inapp_browser') and self.inapp_browser:
                self.inapp_browser.hide()
            return True
        elif msg.startswith('DIMPLE_BROWSER_NAV:'):
            nav_url = msg[len('DIMPLE_BROWSER_NAV:'):].strip()
            self.inapp_browser_navigate(nav_url)
            return True
        elif msg == 'DIMPLE_BROWSER_BACK':
            if hasattr(self, 'inapp_browser') and self.inapp_browser:
                self.inapp_browser.back()
            return True
        elif msg == 'DIMPLE_BROWSER_FORWARD':
            if hasattr(self, 'inapp_browser') and self.inapp_browser:
                self.inapp_browser.forward()
            return True
        elif msg == 'DIMPLE_BROWSER_RELOAD':
            if hasattr(self, 'inapp_browser') and self.inapp_browser:
                self.inapp_browser.reload()
            return True
        elif msg == 'DIMPLE_BROWSER_HOME':
            if hasattr(self, 'inapp_browser') and self.inapp_browser:
                self.inapp_browser.setUrl(QUrl("https://www.youtube.com"))
            return True
        elif msg.startswith('DIMPLE_SEND_TO_DOWNLOADER:'):
            url = msg[len('DIMPLE_SEND_TO_DOWNLOADER:'):].strip()
            self.send_video_to_downloader(url)
            return True
        return False

    def update_browser_geometry(self, x, y, w, h):
        if hasattr(self, 'inapp_browser') and self.inapp_browser:
            self.inapp_browser.setGeometry(int(x), int(y), int(w), int(h))
            if self.browser_tab_active:
                self.inapp_browser.show()
                self.inapp_browser.raise_()

    def inapp_browser_navigate(self, url):
        import urllib.parse
        if not hasattr(self, 'inapp_browser') or not self.inapp_browser or not url:
            return
        if not url.startswith('http://') and not url.startswith('https://'):
            if '.' in url and ' ' not in url:
                url = 'https://' + url
            else:
                url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(url)}"

        parsed = urllib.parse.urlparse(url)
        host = parsed.netloc.lower()
        allowed = (
            'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com',
            'consent.youtube.com', 'youtu.be', 'accounts.google.com', 'myaccount.google.com'
        )
        if not (host in allowed or host.endswith('.youtube.com') or host.endswith('.googlevideo.com') or host.endswith('.ytimg.com')):
            self.notify_restriction(host)
            search_query = parsed.netloc + parsed.path
            url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(search_query)}"

        self.inapp_browser.setUrl(QUrl(url))

    def send_video_to_downloader(self, url):
        import json
        self.browser.page().runJavaScript(f"sendCurrentBrowserVideoToDownloader({json.dumps(url)});")

    def notify_restriction(self, host):
        import json
        self.browser.page().runJavaScript(f"showToast('Only YouTube is accessible in Dimple Browser for now (blocked {json.dumps(host)}).');")

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'browser') and self.browser:
            self.browser.page().runJavaScript("if (typeof syncBrowserGeometry === 'function') syncBrowserGeometry();")

    def start_navigation(self):
        self.browser.load(QUrl(self.app_url))

    def on_load_finished(self, ok):
        print(f"DEBUG: on_load_finished ok={ok}, url={self.browser.url().toString()}", flush=True)
        if ok:
            self.browser.page().runJavaScript("window.isDimpleDesktop = true;")
            self.browser.update()
            self.browser.repaint()
        else:
            # If load failed on first attempt, retry after brief delay
            QTimer.singleShot(250, self.start_navigation)

    def on_render_terminated(self, termination_type, exit_code):
        # Automatically recover if Chromium process restarts
        QTimer.singleShot(400, self.start_navigation)

def main():
    # Require OpenGL context sharing before QApplication is instantiated
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Dimple")
    app.setApplicationDisplayName("Dimple")

    if os.path.exists(ICON_ICO):
        app.setWindowIcon(QIcon(ICON_ICO))
    elif os.path.exists(ICON_PNG):
        app.setWindowIcon(QIcon(ICON_PNG))

    port = find_free_port()
    start_backend(port)

    window = DimpleMainWindow(port, SESSION_TOKEN)
    window.show()
    QTimer.singleShot(50, window.start_navigation)

    sys.exit(app.exec())

if __name__ == '__main__':
    main()
