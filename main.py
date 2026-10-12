import json
import os
import re
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yt_dlp
from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QFont, QIcon, QPainter, QPainterPath, QPixmap
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QSlider,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

SEARCH_CACHE: dict[str, list] = {}
COVER_CACHE: dict[str, bytes] = {}

AUDIUS_APP_NAME = "Aether"
AUDIUS_HOSTS = ("https://api.audius.co", "https://discoveryprovider.audius.co")


def audius_get(path: str, params: dict | None = None, timeout: int = 6):
    query = urllib.parse.urlencode({**(params or {}), "app_name": AUDIUS_APP_NAME})
    headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
    last = None
    for host in AUDIUS_HOSTS:
        try:
            req = urllib.request.Request(f"{host}{path}?{query}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")).get("data")
        except Exception as ex:
            last = ex
    raise OSError(f"Audius API недоступен: {last}")


def get_app_dir() -> Path:
    if os.name == "nt":
        base_dir = Path(os.environ.get("APPDATA", Path.home()))
    else:
        base_dir = Path.home() / ".config"
    app_dir = base_dir / "Aether"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


LIBRARY_FILE = get_app_dir() / "library.json"

QSS = """
* { font-family: 'Segoe UI', 'Inter', 'Helvetica Neue', sans-serif; color: #F3E8FF; }
QMainWindow, QWidget#root { background: #0D0B18; }
QWidget { background: transparent; }
QFrame#sidebar { background: #18132B; border-right: 1px solid #221B3D; }
QFrame#playerbar { background: #18132B; border-top: 1px solid #221B3D; }
QLabel#logo { font-size: 24px; font-weight: 800; color: #C084FC; letter-spacing: 3px; }
QLabel#h1 { font-size: 28px; font-weight: 700; color: #F3E8FF; }
QLabel#muted { color: #9CA3AF; }
QLabel#artist { color: #A78BFA; font-size: 12px; }
QLabel#title { font-size: 14px; font-weight: 600; }
QLabel#cover { background: #221B3D; border-radius: 10px; }
QLabel#time { color: #A78BFA; font-size: 11px; }
QPushButton#nav { text-align: left; padding: 11px 16px; border: none; border-radius: 10px;
                  color: #D8B4FE; font-size: 14px; background: transparent; }
QPushButton#nav:hover { background: #221B3D; color: #F3E8FF; }
QPushButton#nav:checked { background: #2E2352; color: #E9D5FF; font-weight: 600; }
QPushButton#chip { background: #221B3D; border: 1px solid #2E2352; border-radius: 16px;
                   padding: 8px 18px; color: #E9D5FF; }
QPushButton#chip:hover { background: #A855F7; color: white; }
QPushButton#ctl { background: transparent; border: none; border-radius: 18px;
                  font-size: 18px; min-width: 36px; min-height: 36px; color: #E9D5FF; }
QPushButton#ctl:hover { background: #2E2352; }
QPushButton#play { background: #A855F7; border: none; border-radius: 22px;
                   font-size: 18px; min-width: 44px; min-height: 44px; color: white; }
QPushButton#play:hover { background: #C084FC; }
QPushButton#go { background: #A855F7; border: none; border-radius: 12px;
                 padding: 10px 22px; font-weight: 600; color: white; }
QPushButton#go:hover { background: #C084FC; }
QLineEdit { background: #221B3D; border: 1px solid #2E2352; border-radius: 12px;
            padding: 10px 16px; font-size: 14px; selection-background-color: #A855F7; }
QLineEdit:focus { border: 1px solid #A855F7; }
QTableWidget { background: #18132B; border: none; border-radius: 12px; outline: none;
               gridline-color: transparent; alternate-background-color: #1B1531; }
QTableWidget::item { padding: 6px 8px; border: none; }
QTableWidget::item:hover { background: #221B3D; }
QTableWidget::item:selected { background: #2E2352; color: #E9D5FF; }
QHeaderView { background: #18132B; border: none; }
QHeaderView::section { background: #18132B; color: #A78BFA; border: none;
                       border-bottom: 1px solid #221B3D; padding: 10px 8px; font-weight: 600; }
QTableCornerButton::section { background: #18132B; border: none; }
QSlider::groove:horizontal { height: 4px; background: #2E2352; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #A855F7; border-radius: 2px; }
QSlider::handle:horizontal { background: #E9D5FF; width: 12px; height: 12px;
                             margin: -4px 0; border-radius: 6px; }
QSlider::handle:horizontal:hover { background: #C084FC; }
QScrollBar:vertical { background: #18132B; width: 12px; margin: 0; border: none; }
QScrollBar::handle:vertical { background: #2E2352; border-radius: 4px; min-height: 30px;
                              margin: 2px 2px; }
QScrollBar::handle:vertical:hover { background: #A855F7; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; background: none; border: none; }
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: #18132B; }
"""


def fmt_time(ms: int) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 60:02d}:{s % 60:02d}"


def make_rounded_pixmap(data: bytes, size: int = 66, radius: int = 10) -> QPixmap | None:
    pm = QPixmap()
    if not pm.loadFromData(data):
        return None
    scaled = pm.scaled(size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(size, size)
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, size, size, radius, radius)
    painter.setClipPath(path)
    x = (scaled.width() - size) // 2
    y = (scaled.height() - size) // 2
    painter.drawPixmap(0, 0, scaled, x, y, size, size)
    painter.end()
    return out


ICONS = {
    "home": '<path d="M3 10.5 12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
    "search": '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    "library": '<path d="M9 18V5l11-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="17" cy="16" r="3"/>',
    "prev": '<polygon points="19,20 9,12 19,4"/><line x1="5" y1="4" x2="5" y2="20"/>',
    "next": '<polygon points="5,4 15,12 5,20"/><line x1="19" y1="4" x2="19" y2="20"/>',
    "play": '<polygon points="7,4 20,12 7,20"/>',
    "pause": '<rect x="6" y="4" width="4" height="16" rx="1"/><rect x="14" y="4" width="4" height="16" rx="1"/>',
    "heart": '<path d="M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"/>',
    "volume": '<polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
    "spark": '<path d="M12 2l2.5 7.5L22 12l-7.5 2.5L12 22l-2.5-7.5L2 12l7.5-2.5z"/>',
}


def icon_pixmap(name: str, color: str, size: int = 20, fill: bool = False) -> QPixmap:
    ratio = 3
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
           f'fill="{color if fill else "none"}" stroke="{color}" stroke-width="2" '
           f'stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>')
    pm = QPixmap(size * ratio, size * ratio)
    pm.fill(Qt.GlobalColor.transparent)
    pm.setDevicePixelRatio(ratio)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(QByteArray(svg.encode("utf-8"))).render(p, QRectF(0, 0, size, size))
    p.end()
    return pm


def make_icon(name: str, color: str, size: int = 20, fill: bool = False,
              on: tuple | None = None) -> QIcon:
    ic = QIcon()
    ic.addPixmap(icon_pixmap(name, color, size, fill), QIcon.Mode.Normal, QIcon.State.Off)
    if on:
        ic.addPixmap(icon_pixmap(name, on[0], size, on[1]), QIcon.Mode.Normal, QIcon.State.On)
    return ic


class ElidedLabel(QLabel):
    def __init__(self, text: str = ""):
        super().__init__()
        self._full = text
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setText(text)

    def setText(self, text: str):
        self._full = text
        self.setToolTip(text)
        self._refresh()

    def _refresh(self):
        w = max(self.width(), 10)
        super().setText(self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, w))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._refresh()


class ImageLoaderWorker(QThread):
    loaded = pyqtSignal(bytes)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        if not self.url:
            return
        if self.url in COVER_CACHE:
            self.loaded.emit(COVER_CACHE[self.url])
            return
        try:
            req = urllib.request.Request(self.url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
                if data:
                    COVER_CACHE[self.url] = data
                    self.loaded.emit(data)
        except Exception:
            pass


class SearchWorker(QThread):
    results_ready = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, query: str, limit: int = 15):
        super().__init__()
        self.query = query.strip()
        self.limit = limit

    def _search_ytdlp(self, prefix: str, source_name: str = "Ссылка") -> list:
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "socket_timeout": 5,
            "playlistend": self.limit,
        }
        tracks = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                if prefix == "ytmusic":
                    target_query = "https://music.youtube.com/search?q=" + urllib.parse.quote(self.query) + "#songs"
                elif prefix:
                    target_query = f"{prefix}{self.limit}:{self.query}"
                else:
                    target_query = self.query
                info = ydl.extract_info(target_query, download=False)
                entries = info.get("entries", []) if "entries" in (info or {}) else [info]
                for e in entries or []:
                    if not e or not e.get("id"):
                        continue
                    if prefix == "ytmusic":
                        e_url = e.get("webpage_url") or e.get("url") or ""
                        if "watch?v=" not in e_url and len(str(e.get("id"))) != 11:
                            continue
                        if not e.get("title"):
                            continue
                    thumb = e.get("thumbnail")
                    if not thumb and e.get("thumbnails"):
                        thumb = e.get("thumbnails")[-1].get("url")

                    tracks.append({
                        "id": str(e.get("id")),
                        "title": e.get("title") or e.get("track") or "Без названия",
                        "artist": e.get("uploader") or e.get("artist") or e.get("channel") or "Неизвестно",
                        "duration": int(e.get("duration") or 0),
                        "url": e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                        "thumbnail": thumb,
                        "source": source_name,
                    })
        except Exception as ex:
            print(f"[search:{source_name}] {ex}", file=sys.stderr)
        return tracks

    def _search_audius(self) -> list:
        tracks = []
        try:
            data = audius_get("/v1/tracks/search", {"query": self.query, "limit": self.limit}) or []
            for e in data:
                if not e or not e.get("id"):
                    continue
                if e.get("is_delete") or e.get("is_unlisted") or e.get("is_streamable") is False:
                    continue
                if (e.get("access") or {}).get("stream") is False:
                    continue
                art = e.get("artwork") or {}
                thumb = art.get("480x480") or art.get("1000x1000") or art.get("150x150")
                user = e.get("user") or {}
                track_id = f"audius:{e['id']}"
                tracks.append({
                    "id": track_id,
                    "title": e.get("title") or "Без названия",
                    "artist": user.get("name") or user.get("handle") or "Неизвестно",
                    "duration": int(e.get("duration") or 0),
                    "url": track_id,
                    "thumbnail": thumb,
                    "source": "Audius",
                })
        except Exception as ex:
            print(f"[search:Audius] {ex}", file=sys.stderr)
        return tracks

    def run(self):
        if not self.query:
            self.results_ready.emit([])
            return

        if self.query in SEARCH_CACHE:
            self.results_ready.emit(SEARCH_CACHE[self.query])
            return

        try:
            if self.query.startswith("http://") or self.query.startswith("https://"):
                tracks = self._search_ytdlp("", source_name="Прямая ссылка")
                self.results_ready.emit(tracks)
                return

            results = {}
            with ThreadPoolExecutor(max_workers=3) as executor:
                futures = {
                    executor.submit(self._search_ytdlp, "ytmusic", "YouTube Music"): "ytmusic",
                    executor.submit(self._search_ytdlp, "scsearch", "SoundCloud"): "sc",
                    executor.submit(self._search_audius): "audius",
                }
                for future in as_completed(futures):
                    service = futures[future]
                    try:
                        results[service] = future.result() or []
                    except Exception:
                        results[service] = []

            yt_tracks = results.get("ytmusic", [])
            if not yt_tracks:
                yt_tracks = self._search_ytdlp("ytsearch", "YouTube")
            sc_tracks = results.get("sc", [])
            audius_tracks = results.get("audius", [])

            final_tracks = []
            provider_lists = (yt_tracks, sc_tracks, audius_tracks)
            for i in range(max(len(lst) for lst in provider_lists)):
                for lst in provider_lists:
                    if i < len(lst):
                        final_tracks.append(lst[i])

            if final_tracks:
                SEARCH_CACHE[self.query] = final_tracks

            self.results_ready.emit(final_tracks)
        except Exception as ex:
            self.failed.emit(str(ex))


class StreamFetchWorker(QThread):
    stream_ready = pyqtSignal(int, dict)
    failed = pyqtSignal(int, str)

    def __init__(self, token: int, page_url: str):
        super().__init__()
        self.token, self.page_url = token, page_url

    def _fetch_audius(self):
        track_id = urllib.parse.quote(self.page_url.split(":", 1)[1], safe="")
        url = audius_get(f"/v1/tracks/{track_id}/stream", {"no_redirect": "true"})
        if not isinstance(url, str) or not url.startswith("http"):
            url = (f"{AUDIUS_HOSTS[0]}/v1/tracks/{track_id}/stream?"
                   + urllib.parse.urlencode({"app_name": AUDIUS_APP_NAME}))
        headers = {"User-Agent": "Mozilla/5.0"}

        size = 0
        try:
            req = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                m = re.match(r"bytes \d+-\d+/(\d+)", r.headers.get("Content-Range", ""))
                if m:
                    size = int(m.group(1))
                elif r.status == 200:
                    size = int(r.headers.get("Content-Length") or 0)
        except Exception:
            pass

        self.stream_ready.emit(self.token, {
            "url": url, "headers": headers, "size": size,
            "mime": "audio/mpeg", "ext": "mp3", "is_hls": False,
            "direct": size == 0,
            "thumbnail": None,
        })

    def run(self):
        if self.page_url.startswith("audius:"):
            try:
                self._fetch_audius()
            except Exception as ex:
                self.failed.emit(self.token, str(ex))
            return

        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "socket_timeout": 5,
            "format": "bestaudio[protocol^=http][protocol!=m3u8_native][protocol!=m3u8]/bestaudio/best"
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.page_url, download=False)
            if "url" not in info:
                if info.get("entries"):
                    info = next(iter(info["entries"]))
                elif info.get("requested_formats"):
                    info = {**info, **info["requested_formats"][0]}
                elif info.get("formats"):
                    info = {**info, **info["formats"][-1]}
            url = info["url"]
            headers = dict(info.get("http_headers") or {})
            
            protocol = info.get("protocol", "")
            is_hls = "m3u8" in url or "m3u8" in protocol
            
            ext = info.get("ext") or "m4a"
            mime = {"m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm"}.get(ext, "audio/mpeg")
            if is_hls:
                mime = "application/x-mpegURL"

            size = int(info.get("filesize") or info.get("filesize_approx") or 0)
            if not is_hls and not size:
                size = 50 * 1024 * 1024

            thumb = info.get("thumbnail")
            if not thumb and info.get("thumbnails"):
                thumb = info.get("thumbnails")[-1].get("url")

            self.stream_ready.emit(self.token, {
                "url": url, "headers": headers, "size": size,
                "mime": mime, "ext": ext, "is_hls": is_hls, "thumbnail": thumb
            })
        except Exception as ex:
            self.failed.emit(self.token, str(ex))


CHUNK = 512 * 1024


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    @staticmethod
    def _fetch(src: dict, a: int, b: int) -> bytes:
        last = None
        for attempt in range(4):
            try:
                req = urllib.request.Request(
                    src["url"], headers={**src["headers"], "Range": f"bytes={a}-{b}"})
                with urllib.request.urlopen(req, timeout=8) as r:
                    data = r.read()
                if data:
                    return data
            except Exception as ex:
                last = ex
            time.sleep(0.1 * (attempt + 1))
        raise OSError(f"fetch failed: {last}")

    def _serve(self, head: bool):
        src = self.server.source
        if not src:
            self.send_error(404)
            return
        total = src["size"]
        start, end, status = 0, total - 1, 200
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            status = 206
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), total - 1)
            else:
                start = max(0, total - int(m.group(2)))
        if start > end or start >= total:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{total}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(status)
        self.send_header("Content-Type", src["mime"])
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
        self.end_headers()
        if head:
            return
        pos = start
        try:
            while pos <= end:
                data = self._fetch(src, pos, min(pos + CHUNK - 1, end))
                self.wfile.write(data)
                pos += len(data)
        except OSError:
            pass

    def do_GET(self):
        self._serve(False)

    def do_HEAD(self):
        self._serve(True)


class StreamProxy:
    def __init__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self.httpd.daemon_threads = True
        self.httpd.source = None
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def open(self, token: int, src: dict) -> str:
        self.httpd.source = src
        return f"http://127.0.0.1:{self.port}/{token}.{src['ext']}"


class SidebarWidget(QFrame):
    page_requested = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(220)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 22, 14, 14)
        lay.setSpacing(6)

        logo_row = QHBoxLayout()
        logo_row.setContentsMargins(6, 0, 0, 0)
        logo_row.setSpacing(10)
        logo_icon = QLabel()
        logo_icon.setPixmap(icon_pixmap("spark", "#C084FC", 26, fill=True))
        logo = QLabel("AETHER")
        logo.setObjectName("logo")
        logo_row.addWidget(logo_icon)
        logo_row.addWidget(logo)
        logo_row.addStretch()

        lay.addLayout(logo_row)
        lay.addSpacing(18)

        self.buttons = []
        nav_items = [
            ("home", "Главная", False),
            ("search", "Поиск", False),
            ("heart", "Избранное", True),
        ]

        for i, (icon, text, fill) in enumerate(nav_items):
            b = QPushButton("  " + text)
            b.setObjectName("nav")
            b.setIcon(make_icon(icon, "#D8B4FE", 18, fill=fill, on=("#E9D5FF", fill)))
            b.setIconSize(QSize(18, 18))
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self.select(idx))
            lay.addWidget(b)
            self.buttons.append(b)

        lay.addStretch()
        self.select(0, emit=False)

    def select(self, idx: int, emit: bool = True):
        for i, b in enumerate(self.buttons):
            b.setChecked(i == idx)
        if emit:
            self.page_requested.emit(idx)


class TrackTableWidget(QTableWidget):
    track_chosen = pyqtSignal(int)
    COL_ALIGN = {
        0: Qt.AlignmentFlag.AlignCenter,
        3: Qt.AlignmentFlag.AlignCenter,
        4: Qt.AlignmentFlag.AlignRight,
    }

    def __init__(self):
        super().__init__(0, 5)
        self.tracks: list[dict] = []
        self.setHorizontalHeaderLabels(["№", "Название", "Исполнитель", "Источник", "Время"])
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setCornerButtonEnabled(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setAlternatingRowColors(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        h = self.horizontalHeader()
        h.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        h.setHighlightSections(False)
        h.setStretchLastSection(False)
        for c, al in self.COL_ALIGN.items():
            header_item = self.horizontalHeaderItem(c)
            if header_item:
                header_item.setTextAlignment(al | Qt.AlignmentFlag.AlignVCenter)
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(0, 44)
        self.setColumnWidth(2, 180)
        self.setColumnWidth(3, 130)
        self.setColumnWidth(4, 70)
        self.verticalHeader().setDefaultSectionSize(44)
        self.cellClicked.connect(lambda r, _c: self.track_chosen.emit(r))

    def set_tracks(self, tracks: list[dict]):
        self.tracks = list(tracks)
        self.setRowCount(len(tracks))
        for r, t in enumerate(tracks):
            dur = fmt_time(t["duration"] * 1000) if t.get("duration") else "--:--"
            source = t.get("source", "—")
            for c, text in enumerate([str(r + 1), t["title"], t["artist"], source, dur]):
                it = QTableWidgetItem(text)
                it.setTextAlignment(self.COL_ALIGN.get(c, Qt.AlignmentFlag.AlignLeft)
                                    | Qt.AlignmentFlag.AlignVCenter)
                if c in (0, 2, 3, 4):
                    it.setForeground(Qt.GlobalColor.lightGray if c != 2 else Qt.GlobalColor.white)
                self.setItem(r, c, it)

    def highlight(self, row: int):
        if 0 <= row < self.rowCount():
            self.selectRow(row)


class PlayerBarWidget(QFrame):
    prev_clicked = pyqtSignal()
    next_clicked = pyqtSignal()
    toggle_clicked = pyqtSignal()
    like_clicked = pyqtSignal()
    seek_requested = pyqtSignal(int)
    volume_changed = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self.setObjectName("playerbar")
        self.setFixedHeight(96)
        self._seeking = False
        self.img_worker = None

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 10, 18, 10)

        left = QHBoxLayout()
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(12)
        self.cover = QLabel()
        self.cover.setObjectName("cover")
        self.reset_cover()
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover.setFixedSize(66, 66)

        info = QVBoxLayout()
        info.setSpacing(3)
        info.addStretch()
        self.title = ElidedLabel("Ничего не играет")
        self.title.setObjectName("title")
        self.artist = ElidedLabel("Найдите трек в поиске или вставьте ссылку")
        self.artist.setObjectName("artist")
        info.addWidget(self.title)
        info.addWidget(self.artist)
        info.addStretch()

        self.like = QPushButton()
        self.like.setObjectName("ctl")
        self.like.setCheckable(True)
        self.like.setCursor(Qt.CursorShape.PointingHandCursor)
        self.like.setFixedSize(36, 36)
        self.like.setIconSize(QSize(20, 20))
        self.like.setIcon(make_icon("heart", "#E9D5FF", 20, on=("#C084FC", True)))
        self.like.clicked.connect(self.like_clicked)

        left.addWidget(self.cover)
        left.addLayout(info, 1)
        left.addWidget(self.like, 0, Qt.AlignmentFlag.AlignVCenter)
        lw = QWidget()
        lw.setLayout(left)
        lw.setFixedWidth(380)

        center = QVBoxLayout()
        btns = QHBoxLayout()
        btns.addStretch()
        self.btn_prev = QPushButton()
        self.btn_play = QPushButton()
        self.btn_next = QPushButton()
        self.btn_prev.setObjectName("ctl")
        self.btn_next.setObjectName("ctl")
        self.btn_play.setObjectName("play")
        self.icon_play = make_icon("play", "#FFFFFF", 20, fill=True)
        self.icon_pause = make_icon("pause", "#FFFFFF", 20, fill=True)
        self.btn_prev.setIcon(make_icon("prev", "#E9D5FF", 18, fill=True))
        self.btn_next.setIcon(make_icon("next", "#E9D5FF", 18, fill=True))
        self.btn_play.setIcon(self.icon_play)
        for b in (self.btn_prev, self.btn_next):
            b.setIconSize(QSize(18, 18))
        self.btn_play.setIconSize(QSize(20, 20))
        btns.setSpacing(10)
        for b in (self.btn_prev, self.btn_play, self.btn_next):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            btns.addWidget(b)
        btns.addStretch()
        self.btn_prev.clicked.connect(self.prev_clicked)
        self.btn_next.clicked.connect(self.next_clicked)
        self.btn_play.clicked.connect(self.toggle_clicked)

        prog = QHBoxLayout()
        self.lbl_pos = QLabel("00:00")
        self.lbl_dur = QLabel("00:00")
        self.lbl_pos.setObjectName("time")
        self.lbl_dur.setObjectName("time")
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 0)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.slider.sliderReleased.connect(self._released)
        self.slider.sliderMoved.connect(lambda v: self.lbl_pos.setText(fmt_time(v)))
        prog.addWidget(self.lbl_pos)
        prog.addWidget(self.slider, 1)
        prog.addWidget(self.lbl_dur)
        center.addLayout(btns)
        center.addLayout(prog)

        right = QHBoxLayout()
        right.addStretch()
        vol_icon = QLabel()
        vol_icon.setPixmap(icon_pixmap("volume", "#D8B4FE", 20))
        right.addWidget(vol_icon)
        self.vol = QSlider(Qt.Orientation.Horizontal)
        self.vol.setRange(0, 100)
        self.vol.setValue(70)
        self.vol.setFixedWidth(110)
        self.vol.valueChanged.connect(self.volume_changed)
        right.addWidget(self.vol)
        rw = QWidget()
        rw.setLayout(right)
        rw.setFixedWidth(200)

        root.addWidget(lw)
        root.addLayout(center, 1)
        root.addWidget(rw)

    def reset_cover(self):
        self.cover.setPixmap(icon_pixmap("library", "#E9D5FF", 28))

    def load_cover_url(self, url: str | None):
        self.reset_cover()
        if not url:
            return
        self.img_worker = ImageLoaderWorker(url)
        self.img_worker.loaded.connect(self._on_cover_loaded)
        self.img_worker.start()

    def _on_cover_loaded(self, data: bytes):
        pm = make_rounded_pixmap(data, 66, 10)
        if pm:
            self.cover.setPixmap(pm)

    def _released(self):
        self._seeking = False
        self.seek_requested.emit(self.slider.value())

    def set_track(self, title: str, artist: str, liked: bool, thumb_url: str | None = None):
        self.title.setText(title)
        self.artist.setText(artist)
        self.like.setChecked(liked)
        self.load_cover_url(thumb_url)

    def set_message(self, text: str):
        self.artist.setText(text)

    def set_playing(self, playing: bool):
        self.btn_play.setIcon(self.icon_pause if playing else self.icon_play)

    def set_duration(self, ms: int):
        self.slider.setRange(0, ms)
        self.lbl_dur.setText(fmt_time(ms))

    def set_position(self, ms: int):
        if not self._seeking:
            self.slider.setValue(ms)
            self.lbl_pos.setText(fmt_time(ms))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Aether")
        self.resize(1180, 740)
        self.setMinimumSize(900, 560)

        self.queue: list[dict] = []
        self.index = -1
        self.token = 0
        self.search_worker = None
        self.stream_worker = None
        self.library: list[dict] = self._load_library()

        self.player = QMediaPlayer()
        self.audio = QAudioOutput()
        self.player.setAudioOutput(self.audio)
        self.audio.setVolume(0.7)
        self.proxy = StreamProxy()

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        body = QHBoxLayout()
        body.setSpacing(0)
        self.sidebar = SidebarWidget()
        self.stack = QStackedWidget()
        body.addWidget(self.sidebar)
        body.addWidget(self.stack, 1)
        self.bar = PlayerBarWidget()
        outer.addLayout(body, 1)
        outer.addWidget(self.bar)

        self.stack.addWidget(self._build_home())
        self.stack.addWidget(self._build_search())
        self.stack.addWidget(self._build_library())

        self.sidebar.page_requested.connect(self.stack.setCurrentIndex)
        self.search_table.track_chosen.connect(lambda i: self.play_from(self.search_table.tracks, i))
        self.lib_table.track_chosen.connect(lambda i: self.play_from(self.lib_table.tracks, i))
        self.bar.toggle_clicked.connect(self.toggle_play)
        self.bar.next_clicked.connect(self.play_next)
        self.bar.prev_clicked.connect(self.play_prev)
        self.bar.like_clicked.connect(self.toggle_like)
        self.bar.seek_requested.connect(self.player.setPosition)
        self.bar.volume_changed.connect(lambda v: self.audio.setVolume(v / 100))
        self.player.positionChanged.connect(self.bar.set_position)
        self.player.durationChanged.connect(self.bar.set_duration)
        self.player.playbackStateChanged.connect(
            lambda s: self.bar.set_playing(s == QMediaPlayer.PlaybackState.PlayingState))
        self.player.mediaStatusChanged.connect(self._media_status)
        self.player.errorOccurred.connect(lambda _e, msg: self.bar.set_message(f"Ошибка плеера: {msg[:70]}"))

        self.lib_table.set_tracks(self.library)

    def _page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(32, 28, 32, 16)
        lay.setSpacing(14)
        return w, lay

    def _build_home(self):
        w, lay = self._page()
        h = QLabel("Добро пожаловать в Aether")
        h.setObjectName("h1")
        sub = QLabel("Слушайте музыку без границ. Поддерживаются YouTube Music, SoundCloud, Audius и другие платформы.")
        sub.setObjectName("muted")
        lay.addWidget(h)
        lay.addWidget(sub)
        row = QHBoxLayout()
        for q in ["Lo-fi", "Synthwave", "Classical", "Electronic", "Chill", "Rock"]:
            b = QPushButton(q)
            b.setObjectName("chip")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, text=q: self.run_search(text, goto=True))
            row.addWidget(b)
        row.addStretch()
        lay.addLayout(row)
        lay.addStretch()
        return w

    def _build_search(self):
        w, lay = self._page()
        row = QHBoxLayout()

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Название трека, исполнитель или ссылка")
        self.search_edit.returnPressed.connect(lambda: self.run_search(self.search_edit.text()))
        
        go = QPushButton("Искать")
        go.setObjectName("go")
        go.setCursor(Qt.CursorShape.PointingHandCursor)
        go.clicked.connect(lambda: self.run_search(self.search_edit.text()))
        
        row.addWidget(self.search_edit, 1)
        row.addWidget(go)
        
        self.search_status = QLabel("")
        self.search_status.setObjectName("muted")
        self.search_table = TrackTableWidget()
        
        lay.addLayout(row)
        lay.addWidget(self.search_status)
        lay.addWidget(self.search_table, 1)
        return w

    def _build_library(self):
        w, lay = self._page()
        h = QLabel("Избранное")
        h.setObjectName("h1")
        hint = QLabel("Нажмите на сердечко в плеере, чтобы добавить трек сюда.")
        hint.setObjectName("muted")
        self.lib_table = TrackTableWidget()
        lay.addWidget(h)
        lay.addWidget(hint)
        lay.addWidget(self.lib_table, 1)
        return w

    def run_search(self, query: str, goto: bool = False):
        query = query.strip()
        if not query:
            return
        if goto:
            self.sidebar.select(1)
            self.search_edit.setText(query)
            
        self.search_status.setText("Ищем в эфире…")
        self.search_worker = SearchWorker(query)
        self.search_worker.results_ready.connect(self._on_results)
        self.search_worker.failed.connect(lambda m: self.search_status.setText(f"Ошибка поиска: {m}"))
        self.search_worker.start()

    def _on_results(self, tracks: list):
        self.search_table.set_tracks(tracks)
        self.search_status.setText(f"Найдено треков: {len(tracks)}" if tracks else "Ничего не найдено")

    def play_from(self, tracks: list, idx: int):
        self.queue = list(tracks)
        self.play_index(idx)

    def play_index(self, idx: int):
        if not (0 <= idx < len(self.queue)):
            return
        self.index = idx
        t = self.queue[idx]
        self.token += 1
        self.player.stop()
        
        self.bar.set_track(t["title"], t["artist"], self._is_liked(t), t.get("thumbnail"))
        self.bar.set_duration(0)
        
        self.stream_worker = StreamFetchWorker(self.token, t["url"])
        self.stream_worker.stream_ready.connect(self._on_stream)
        self.stream_worker.failed.connect(self._on_stream_failed)
        self.stream_worker.start()
        
        for tbl in (self.search_table, self.lib_table):
            if tbl.tracks == self.queue:
                tbl.highlight(idx)

    def _on_stream(self, token: int, src: dict):
        if token != self.token:
            return
        
        if src.get("is_hls") or src.get("direct"):
            self.player.setSource(QUrl(src["url"]))
        else:
            self.player.setSource(QUrl(self.proxy.open(token, src)))
            
        self.player.play()
        if src.get("thumbnail") and not self.queue[self.index].get("thumbnail"):
            self.queue[self.index]["thumbnail"] = src["thumbnail"]
            self.bar.load_cover_url(src["thumbnail"])

    def _on_stream_failed(self, token: int, msg: str):
        if token == self.token:
            self.bar.set_message(f"Не удалось получить поток: {msg[:70]}")

    def toggle_play(self):
        if self.player.source().isEmpty():
            return
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def play_next(self):
        if self.queue:
            self.play_index((self.index + 1) % len(self.queue))

    def play_prev(self):
        if not self.queue:
            return
        if self.player.position() > 3000:
            self.player.setPosition(0)
        else:
            self.play_index((self.index - 1) % len(self.queue))

    def _media_status(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.play_next()

    def _load_library(self) -> list:
        try:
            return json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save_library(self):
        try:
            LIBRARY_FILE.write_text(json.dumps(self.library, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _is_liked(self, t: dict) -> bool:
        return any(x["id"] == t["id"] for x in self.library)

    def toggle_like(self):
        if not (0 <= self.index < len(self.queue)):
            self.bar.like.setChecked(False)
            return
        t = self.queue[self.index]
        if self._is_liked(t):
            self.library = [x for x in self.library if x["id"] != t["id"]]
        else:
            self.library.append(t)
        self._save_library()
        self.lib_table.set_tracks(self.library)
        self.bar.like.setChecked(self._is_liked(t))


def main():
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
