import json
import os
import re
import sys
import threading
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import zip_longest
from pathlib import Path

import yt_dlp
from PyQt6.QtCore import QByteArray, QRectF, QSize, Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPixmap
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
from zvuk_music import Client as _ZvukClient
from zvuk_music import Quality as _ZvukQuality

# ───────────────────────────── Общие константы и хелперы ─────────────────────────────

SEARCH_CACHE: dict[tuple, list] = {}
COVER_CACHE: dict[str, bytes] = {}
UA = {"User-Agent": "Mozilla/5.0"}


def clean_error(msg) -> str:
    msg = re.sub(r"\x1b?\[[0-9;]*m", "", str(msg))
    msg = re.sub(r"^\s*ERROR:\s*", "", msg.strip())
    if "Unsupported URL" in msg:
        return "ссылка не поддерживается"
    return msg.strip()


def fmt_time(ms: int) -> str:
    s = max(0, int(ms // 1000))
    return f"{s // 60:02d}:{s % 60:02d}"


def make_track(id_, title, artist, duration, url, thumb, source) -> dict:
    return {
        "id": id_,
        "title": title or "Без названия",
        "artist": artist or "Неизвестно",
        "duration": int(duration or 0),
        "url": url,
        "thumbnail": thumb,
        "source": source,
    }


def pick_thumb(e: dict) -> str | None:
    thumbs = e.get("thumbnails")
    return e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)


# ───────────────────────────── Настройки и хранилище ─────────────────────────────

def get_app_dir() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home())) if os.name == "nt" else Path.home() / ".config"
    app_dir = base / "Aether"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


LIBRARY_FILE = get_app_dir() / "library.json"
SETTINGS_FILE = get_app_dir() / "settings.json"


def _load_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


SETTINGS: dict = _load_settings()


def save_settings():
    try:
        SETTINGS_FILE.write_text(json.dumps(SETTINGS, ensure_ascii=False, indent=2), encoding="utf-8")
        if os.name != "nt":
            os.chmod(SETTINGS_FILE, 0o600)
    except OSError:
        pass


# ───────────────────────────── Источники треков ─────────────────────────────

# ── Audius ──

AUDIUS_APP_NAME = "Aether"
AUDIUS_HOSTS = ("https://api.audius.co", "https://discoveryprovider.audius.co")


def audius_get(path: str, params: dict | None = None, timeout: int = 6):
    query = urllib.parse.urlencode({**(params or {}), "app_name": AUDIUS_APP_NAME})
    headers = {**UA, "Accept": "application/json"}
    last = None
    for host in AUDIUS_HOSTS:
        try:
            req = urllib.request.Request(f"{host}{path}?{query}", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8")).get("data")
        except Exception as ex:
            last = ex
    raise OSError(f"Audius API недоступен: {last}")


def audius_search(query: str, limit: int) -> list:
    tracks = []
    try:
        for e in audius_get("/v1/tracks/search", {"query": query, "limit": limit}) or []:
            if not e or not e.get("id"):
                continue
            if e.get("is_delete") or e.get("is_unlisted") or e.get("is_streamable") is False:
                continue
            if (e.get("access") or {}).get("stream") is False:
                continue
            art = e.get("artwork") or {}
            user = e.get("user") or {}
            track_id = f"audius:{e['id']}"
            tracks.append(make_track(
                track_id, e.get("title"), user.get("name") or user.get("handle"), e.get("duration"), track_id,
                art.get("480x480") or art.get("1000x1000") or art.get("150x150"), "Audius"))
    except Exception as ex:
        print(f"[search:Audius] {ex}", file=sys.stderr)
    return tracks


# ── YouTube Music / SoundCloud (yt-dlp) ──

def ytdlp_search(query: str, limit: int, prefix: str, source_name: str = "Ссылка") -> list:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True,
            "socket_timeout": 5, "playlistend": limit}
    tracks = []
    try:
        if prefix == "ytmusic":
            target = "https://music.youtube.com/search?q=" + urllib.parse.quote(query) + "#songs"
        elif prefix:
            target = f"{prefix}{limit}:{query}"
        else:
            target = query
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(target, download=False)
        entries = info.get("entries", []) if "entries" in (info or {}) else [info]
        for e in entries or []:
            if not e or not e.get("id"):
                continue
            if prefix == "ytmusic":
                e_url = e.get("webpage_url") or e.get("url") or ""
                if ("watch?v=" not in e_url and len(str(e["id"])) != 11) or not e.get("title"):
                    continue
            tracks.append(make_track(
                str(e["id"]),
                e.get("title") or e.get("track"),
                e.get("uploader") or e.get("artist") or e.get("channel"),
                e.get("duration"),
                e.get("webpage_url") or e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
                pick_thumb(e),
                source_name,
            ))
    except Exception as ex:
        print(f"[search:{source_name}] {ex}", file=sys.stderr)
    return tracks


# ── Сберзвук ──

ZVUK_SOURCE = "Сберзвук"
ZVUK_TOKEN_URL = "https://zvuk.com/api/v2/tiny/profile"
ZVUK_LINK_RE = re.compile(r"https?://(?:www\.)?zvuk\.com/(track|release|playlist|artist|abook|podcast)/(\d+)", re.I)


class ZvukError(Exception):
    pass


def zvuk_token() -> str:
    return str(SETTINGS.get("zvuk_token") or "").strip()


def zvuk_enabled() -> bool:
    return bool(zvuk_token())


def extract_zvuk_token(text: str) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    m = re.search(r'"token"\s*:\s*"([^"]{16,})"', text)
    if m:
        return m.group(1).strip()
    bare = text.strip("\"'`<> \n\t")
    return bare if re.fullmatch(r"[A-Za-z0-9._\-]{16,}", bare) else ""


def zvuk_friendly_error(ex: Exception) -> str:
    if isinstance(ex, ZvukError):
        return str(ex)
    name = type(ex).__name__
    return {
        "UnauthorizedError": "Токен Сберзвука не принят или устарел — обновите его в «Настройках»",
        "SubscriptionRequiredError": "Для этого трека нужна подписка Сберзвука",
        "BotDetectedError": "Сберзвук временно блокирует запросы (антибот) — попробуйте позже",
    }.get(name) or f"Сберзвук: {str(ex)[:80] or name}"


_ZVUK_LOCK = threading.Lock()
_ZVUK_STATE: dict = {"token": None, "client": None}


def zvuk_client():
    token = zvuk_token()
    if not token:
        raise ZvukError("Сберзвук не подключён — добавьте токен в «Настройках»")
    with _ZVUK_LOCK:
        if _ZVUK_STATE["token"] != token:
            _ZVUK_STATE.update(token=token, client=_ZvukClient(token=token))
        return _ZVUK_STATE["client"]


def _dig(obj, *names):
    for n in names:
        if obj is None:
            return None
        obj = obj.get(n) if isinstance(obj, dict) else getattr(obj, n, None)
    return obj


def _zvuk_cover(t) -> str | None:
    for path in (("release", "image", "src"), ("image", "src"), ("release", "image"), ("image",)):
        v = _dig(t, *path)
        if isinstance(v, str) and v.startswith("http"):
            return v.replace("{size}", "300")
    return None


def _zvuk_artists(t) -> str:
    fn = getattr(t, "get_artists_str", None)
    if callable(fn):
        try:
            s = fn()
            if s:
                return str(s)
        except Exception:
            pass
    names = getattr(t, "artist_names", None)
    return ", ".join(str(n) for n in names) if names else "Неизвестно"


def _zvuk_track_dict(t, fallback_id=None):
    tid = getattr(t, "id", None) or fallback_id
    title = getattr(t, "title", None)
    if not tid or not title:
        return None
    try:
        duration = int(getattr(t, "duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0
    track_id = f"zvuk:{tid}"
    return make_track(track_id, str(title), _zvuk_artists(t), duration, track_id, _zvuk_cover(t), ZVUK_SOURCE)


def zvuk_search(query: str, limit: int) -> tuple[list, str]:
    """Возвращает (треки, текст_ошибки). Ошибка Сберзвука не должна ломать остальной поиск."""
    try:
        res = zvuk_client().quick_search(query, limit=limit)
        tracks = [d for d in map(_zvuk_track_dict, getattr(res, "tracks", None) or []) if d]
        return tracks, ""
    except Exception as ex:
        print(f"[search:{ZVUK_SOURCE}] {ex!r}", file=sys.stderr)
        return [], zvuk_friendly_error(ex)


def zvuk_track_from_id(tid: str) -> dict:
    t = None
    try:
        t = zvuk_client().get_track(int(tid))
    except Exception as ex:
        if type(ex).__name__ == "UnauthorizedError":
            raise
    d = _zvuk_track_dict(t, tid) if t is not None else None
    if d:
        return d
    track_id = f"zvuk:{tid}"
    return make_track(track_id, f"Трек Сберзвука #{tid}", ZVUK_SOURCE, 0, track_id, None, ZVUK_SOURCE)


def _pick_stream_url(obj, depth: int = 0) -> str | None:
    if obj is None or depth > 4:
        return None
    if isinstance(obj, str):
        return obj if obj.startswith("http") else None
    if isinstance(obj, (list, tuple)):
        return next(filter(None, (_pick_stream_url(x, depth + 1) for x in obj)), None)
    getter = (lambda k: obj.get(k)) if isinstance(obj, dict) else (lambda k: getattr(obj, k, None))
    candidates = [getter(k) for k in ("high", "mid", "url", "stream")]
    if isinstance(obj, dict):
        candidates += list(obj.values())
    return next(filter(None, (_pick_stream_url(v, depth + 1) for v in candidates)), None)


def zvuk_stream_url(track_id: str) -> str:
    client = zvuk_client()
    tid = int(track_id) if str(track_id).isdigit() else track_id
    errors: list = []

    getter = getattr(client, "get_stream_urls", None)
    if callable(getter):
        try:
            url = _pick_stream_url(getter([tid]))
            if url:
                return url
        except Exception as ex:
            errors.append(ex)

    for quality in ("HIGH", "MID"):
        try:
            url = client.get_stream_url(tid, quality=getattr(_ZvukQuality, quality))
            if url:
                return str(url)
        except Exception as ex:
            errors.append(ex)

    for ex in errors:
        if type(ex).__name__ in ("UnauthorizedError", "SubscriptionRequiredError"):
            raise ex
    raise (errors[0] if errors else ZvukError("Сберзвук не вернул ссылку на поток"))


def verify_zvuk(token: str) -> None:
    client = _ZvukClient(token=token)
    client.init()
    check = getattr(client, "is_authorized", None)
    if callable(check) and not check():
        raise ZvukError("Токен Сберзвука не принят — скопируйте его заново")


# ───────────────────────────── Поиск ─────────────────────────────

def search_tracks(query: str, use_zvuk: bool, limit: int = 15) -> tuple[list, str]:
    """Возвращает (треки, уведомление). Выполняется в фоновом потоке."""
    query = query.strip()
    if not query:
        return [], ""
    key = (query, use_zvuk)
    if key in SEARCH_CACHE:
        return SEARCH_CACHE[key], ""

    zm = ZVUK_LINK_RE.match(query)
    if zm:
        if not zvuk_enabled():
            return [], "Чтобы открывать ссылки Сберзвука, подключите его в «Настройках»"
        if zm.group(1).lower() != "track":
            return [], "Из ссылок Сберзвука пока поддерживаются только треки"
        try:
            return [zvuk_track_from_id(zm.group(2))], ""
        except Exception as ex:
            raise ZvukError(zvuk_friendly_error(ex)) from ex

    if query.startswith(("http://", "https://")):
        return ytdlp_search(query, limit, "", "Прямая ссылка"), ""

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = [pool.submit(ytdlp_search, query, limit, "ytmusic", "YouTube Music"),
                pool.submit(ytdlp_search, query, limit, "scsearch", "SoundCloud"),
                pool.submit(audius_search, query, limit)]
        zjob = pool.submit(zvuk_search, query, limit) if use_zvuk else None
        lists = [j.result() for j in jobs]
        zerr = ""
        if zjob:
            ztracks, zerr = zjob.result()
            lists.append(ztracks)

    lists[0] = lists[0] or ytdlp_search(query, limit, "ytsearch", "YouTube")
    final = [t for row in zip_longest(*lists) for t in row if t]
    if final and not zerr:
        SEARCH_CACHE[key] = final
    return final, zerr


# ───────────────────────────── Получение потока и обложек ─────────────────────────────

def _http_stream(url: str, headers: dict) -> dict:
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
    return {"url": url, "headers": headers, "size": size, "mime": "audio/mpeg", "ext": "mp3",
            "is_hls": False, "direct": size == 0, "thumbnail": None}


def fetch_stream(page_url: str) -> dict:
    """Получает прямую ссылку на аудиопоток. Выполняется в фоновом потоке."""
    if page_url.startswith("audius:"):
        track_id = urllib.parse.quote(page_url.split(":", 1)[1], safe="")
        url = audius_get(f"/v1/tracks/{track_id}/stream", {"no_redirect": "true"})
        if not isinstance(url, str) or not url.startswith("http"):
            url = (f"{AUDIUS_HOSTS[0]}/v1/tracks/{track_id}/stream?"
                   + urllib.parse.urlencode({"app_name": AUDIUS_APP_NAME}))
        return _http_stream(url, UA)

    if page_url.startswith("zvuk:"):
        return _http_stream(zvuk_stream_url(page_url.split(":", 1)[1]), {**UA, "Referer": "https://zvuk.com/"})

    opts = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 5,
        "format": "bestaudio[protocol^=http][protocol!=m3u8_native][protocol!=m3u8]/bestaudio/best",
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(page_url, download=False)
    if "url" not in info:
        if info.get("entries"):
            info = next(iter(info["entries"]))
        elif info.get("requested_formats"):
            info = {**info, **info["requested_formats"][0]}
        elif info.get("formats"):
            info = {**info, **info["formats"][-1]}
    url = info["url"]
    is_hls = "m3u8" in url or "m3u8" in info.get("protocol", "")
    ext = info.get("ext") or "m4a"
    mime = "application/x-mpegURL" if is_hls else {
        "m4a": "audio/mp4", "mp4": "audio/mp4", "webm": "audio/webm"}.get(ext, "audio/mpeg")
    size = int(info.get("filesize") or info.get("filesize_approx") or 0)
    if not is_hls and not size:
        size = 50 * 1024 * 1024
    return {"url": url, "headers": dict(info.get("http_headers") or {}), "size": size,
            "mime": mime, "ext": ext, "is_hls": is_hls, "thumbnail": pick_thumb(info)}


def fetch_cover(url: str) -> bytes:
    if url not in COVER_CACHE:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=5) as resp:
            COVER_CACHE[url] = resp.read()
    return COVER_CACHE[url]


# ───────────────────────────── Фоновые задачи ─────────────────────────────

class Worker(QThread):
    done = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, fn, *args, err=clean_error):
        super().__init__()
        self.fn, self.args, self.err = fn, args, err

    def run(self):
        try:
            result = self.fn(*self.args)
        except Exception as ex:
            self.failed.emit(self.err(ex))
        else:
            self.done.emit(result)


_WORKERS: set = set()


def run_worker(fn, *args, on_done=None, on_fail=None, err=clean_error) -> Worker:
    w = Worker(fn, *args, err=err)
    if on_done:
        w.done.connect(on_done)
    if on_fail:
        w.failed.connect(on_fail)
    _WORKERS.add(w)
    w.finished.connect(lambda: _WORKERS.discard(w))
    w.start()
    return w


# ───────────────────────────── Прокси потока ─────────────────────────────

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


# ───────────────────────────── Стили и иконки ─────────────────────────────

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
QFrame#card { background: #18132B; border: 1px solid #221B3D; border-radius: 14px; }
QLabel#h2 { font-size: 18px; font-weight: 700; }
QLabel#badge { border-radius: 9px; padding: 3px 10px; font-size: 11px; font-weight: 600;
               background: #2E2352; color: #D8B4FE; }
QLabel#badge[state="ok"] { background: #14532D; color: #86EFAC; }
QLabel#badge[state="warn"] { background: #78350F; color: #FCD34D; }
QPushButton#linkbtn { background: transparent; border: none; color: #C084FC;
                      text-align: left; padding: 2px 0; }
QPushButton#linkbtn:hover { color: #E9D5FF; text-decoration: underline; }
QPushButton#go:disabled, QPushButton#chip:disabled { background: #2E2352; color: #6B5B95; }
"""


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
    painter.drawPixmap(0, 0, scaled, (scaled.width() - size) // 2, (scaled.height() - size) // 2, size, size)
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
    "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
}


@lru_cache(maxsize=None)
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


# ───────────────────────────── Виджеты ─────────────────────────────

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
        nav_items = [("home", "Главная", False), ("search", "Поиск", False),
                     ("heart", "Избранное", True), ("settings", "Настройки", False)]
        for i, (icon, text, fill) in enumerate(nav_items):
            b = QPushButton("  " + text)
            b.setObjectName("nav")
            b.setIcon(make_icon(icon, "#D8B4FE", 18, fill=fill, on=("#E9D5FF", fill)))
            b.setIconSize(QSize(18, 18))
            b.setCheckable(True)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, idx=i: self.select(idx))
            if i == len(nav_items) - 1:
                lay.addStretch()
            lay.addWidget(b)
            self.buttons.append(b)
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
        Mode = QHeaderView.ResizeMode
        for col, mode, width in ((0, Mode.Fixed, 44), (1, Mode.Stretch, None), (2, Mode.Interactive, 180),
                                 (3, Mode.Interactive, 130), (4, Mode.Fixed, 70)):
            h.setSectionResizeMode(col, mode)
            if width:
                self.setColumnWidth(col, width)
        self.verticalHeader().setDefaultSectionSize(44)
        self.cellClicked.connect(lambda r, _c: self.track_chosen.emit(r))

    def set_tracks(self, tracks: list[dict]):
        self.tracks = list(tracks)
        self.setRowCount(len(tracks))
        for r, t in enumerate(tracks):
            dur = fmt_time(t["duration"] * 1000) if t.get("duration") else "--:--"
            for c, text in enumerate([str(r + 1), t["title"], t["artist"], t.get("source", "—"), dur]):
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
        self._cover_url: str | None = None

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
        btns.setSpacing(10)
        btns.addStretch()
        self.btn_prev, self.btn_play, self.btn_next = QPushButton(), QPushButton(), QPushButton()
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
        for b in (self.btn_prev, self.btn_play, self.btn_next):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            btns.addWidget(b)
        btns.addStretch()
        self.btn_prev.clicked.connect(self.prev_clicked)
        self.btn_next.clicked.connect(self.next_clicked)
        self.btn_play.clicked.connect(self.toggle_clicked)

        prog = QHBoxLayout()
        self.lbl_pos, self.lbl_dur = QLabel("00:00"), QLabel("00:00")
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
        self._cover_url = url
        if url:
            run_worker(fetch_cover, url, on_done=lambda data, u=url: self._on_cover_loaded(u, data))

    def _on_cover_loaded(self, url: str, data: bytes):
        if url != self._cover_url:
            return
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


class SettingsPage(QWidget):
    zvuk_changed = pyqtSignal()

    def __init__(self):
        super().__init__()
        self._pending_token = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(32, 28, 32, 16)
        lay.setSpacing(14)

        h = QLabel("Настройки")
        h.setObjectName("h1")
        lay.addWidget(h)

        card = QFrame()
        card.setObjectName("card")
        cl = QVBoxLayout(card)
        cl.setContentsMargins(22, 20, 22, 20)
        cl.setSpacing(12)

        head = QHBoxLayout()
        title = QLabel("Сберзвук")
        title.setObjectName("h2")
        self.badge = QLabel()
        self.badge.setObjectName("badge")
        head.addWidget(title)
        head.addWidget(self.badge)
        head.addStretch()
        cl.addLayout(head)

        intro = QLabel("Подключите аккаунт, чтобы находить и слушать треки со Сберзвука")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        cl.addWidget(intro)

        steps = QLabel(
            "<b>Как подключить</b><br>"
            "1. Войдите в свой аккаунт на <b>zvuk.com</b> в браузере.<br>"
            "2. Нажмите «Открыть страницу с токеном» — откроется страница с текстом. "
            "Нажмите кнопку «Копировать» вверху страницы.<br>"
            "3. Нажмите «Вставить и подключить». Копировать можно всё целиком — "
            "токен приложение найдёт само.")
        steps.setTextFormat(Qt.TextFormat.RichText)
        steps.setWordWrap(True)
        cl.addWidget(steps)

        row1 = QHBoxLayout()
        self.btn_open = QPushButton("Открыть страницу с токеном")
        self.btn_open.setObjectName("chip")
        self.btn_paste = QPushButton("Вставить и подключить")
        self.btn_paste.setObjectName("go")
        for b in (self.btn_open, self.btn_paste):
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            row1.addWidget(b)
        row1.addStretch()
        cl.addLayout(row1)

        row2 = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.edit.setPlaceholderText("Или вставьте токен сюда вручную")
        self.btn_connect = QPushButton("Подключить")
        self.btn_connect.setObjectName("chip")
        self.btn_connect.setCursor(Qt.CursorShape.PointingHandCursor)
        row2.addWidget(self.edit, 1)
        row2.addWidget(self.btn_connect)
        cl.addLayout(row2)
        self._inputs = (self.btn_paste, self.btn_connect, self.edit)

        self.status = QLabel("")
        self.status.setWordWrap(True)
        cl.addWidget(self.status)

        self.btn_off = QPushButton("Отключить Сберзвук")
        self.btn_off.setObjectName("linkbtn")
        self.btn_off.setCursor(Qt.CursorShape.PointingHandCursor)
        cl.addWidget(self.btn_off, 0, Qt.AlignmentFlag.AlignLeft)

        note = QLabel(f"Токен хранится только на вашем компьютере ({SETTINGS_FILE}) и отправляется "
                      "только Сберзвуку — это ваш пропуск в аккаунт, никому его не передавайте. "
                      "Если страница с токеном показывает ошибку — проверьте, что вы вошли на zvuk.com. "
                      "Если треки Сберзвука перестали находиться, токен мог устареть: повторите шаги выше.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        cl.addWidget(note)

        lay.addWidget(card)
        lay.addStretch()

        self.btn_open.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(ZVUK_TOKEN_URL)))
        self.btn_paste.clicked.connect(self._paste_and_connect)
        self.btn_connect.clicked.connect(self._connect)
        self.edit.returnPressed.connect(self._connect)
        self.btn_off.clicked.connect(self._disconnect)
        self._refresh_state()

    def _set_badge(self, text: str, state: str):
        self.badge.setText(text)
        self.badge.setProperty("state", state)
        self.badge.style().unpolish(self.badge)
        self.badge.style().polish(self.badge)

    def _set_status(self, text: str, kind: str = "muted"):
        color = {"ok": "#86EFAC", "err": "#FCA5A5", "muted": "#9CA3AF"}[kind]
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)

    def _busy(self, busy: bool):
        for w in self._inputs:
            w.setEnabled(not busy)

    def _refresh_state(self):
        token = zvuk_token()
        if token:
            self._set_badge("Подключено", "ok")
            self.edit.setPlaceholderText(f"Токен сохранён (…{token[-4:]}). Чтобы заменить — вставьте новый")
            self.btn_off.setVisible(True)
        else:
            self._set_badge("Не подключено", "off")
            self.edit.setPlaceholderText("Или вставьте токен сюда вручную")
            self.btn_off.setVisible(False)

    def _paste_and_connect(self):
        self.edit.setText(QApplication.clipboard().text())
        self._connect()

    def _connect(self):
        token = extract_zvuk_token(self.edit.text())
        if not token:
            self._set_status("Не нашли токен в скопированном тексте. Откройте страницу кнопкой выше, "
                             "убедитесь, что вы вошли в аккаунт, и скопируйте её содержимое целиком.", "err")
            return
        self._pending_token = token
        self._busy(True)
        self._set_status("Проверяем токен…")
        run_worker(verify_zvuk, token,
                   on_done=lambda _: self._on_verified(True, ""),
                   on_fail=lambda m: self._on_verified(False, m),
                   err=zvuk_friendly_error)

    def _on_verified(self, ok: bool, message: str):
        self._busy(False)
        if not ok:
            self._set_status(message or "Токен не подошёл", "err")
            return
        SETTINGS["zvuk_token"] = self._pending_token
        save_settings()
        self.edit.clear()
        self._set_status("Готово! Сберзвук подключён — его треки появятся в поиске.", "ok")
        self._refresh_state()
        self.zvuk_changed.emit()

    def _disconnect(self):
        SETTINGS.pop("zvuk_token", None)
        save_settings()
        self.edit.clear()
        self._set_status("Сберзвук отключён, токен удалён с этого компьютера.")
        self._refresh_state()
        self.zvuk_changed.emit()


# ───────────────────────────── Главное окно ─────────────────────────────

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Aether")
        self.resize(1180, 740)
        self.setMinimumSize(900, 560)

        self.queue: list[dict] = []
        self.index = -1
        self.token = 0
        self.search_id = 0
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

        self.settings_page = SettingsPage()
        for page in (self._build_home(), self._build_search(), self._build_library(), self.settings_page):
            self.stack.addWidget(page)

        self.settings_page.zvuk_changed.connect(SEARCH_CACHE.clear)
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
        sub = QLabel("Слушайте музыку без границ. Поддерживаются YouTube Music, SoundCloud, Audius и Сберзвук (подключается в «Настройках»).")
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
        self.search_id += 1
        sid = self.search_id
        run_worker(search_tracks, query, zvuk_enabled(),
                   on_done=lambda res: self._on_results(sid, *res),
                   on_fail=lambda m: self._on_search_failed(sid, m))

    def _on_results(self, sid: int, tracks: list, notice: str):
        if sid != self.search_id:
            return
        self.search_table.set_tracks(tracks)
        base = f"Найдено треков: {len(tracks)}" if tracks else "Ничего не найдено"
        self.search_status.setText(f"{base} · {notice}" if notice else base)

    def _on_search_failed(self, sid: int, msg: str):
        if sid == self.search_id:
            self.search_status.setText(f"Ошибка поиска: {msg}")

    def play_from(self, tracks: list, idx: int):
        self.queue = list(tracks)
        self.play_index(idx)

    def play_index(self, idx: int):
        if not (0 <= idx < len(self.queue)):
            return
        self.index = idx
        t = self.queue[idx]
        self.token += 1
        tok = self.token
        self.player.stop()

        self.bar.set_track(t["title"], t["artist"], self._is_liked(t), t.get("thumbnail"))
        self.bar.set_duration(0)

        err = zvuk_friendly_error if t["url"].startswith("zvuk:") else clean_error
        run_worker(fetch_stream, t["url"],
                   on_done=lambda src: self._on_stream(tok, src),
                   on_fail=lambda m: self._on_stream_failed(tok, m),
                   err=err)

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
    for stream in ("stdout", "stderr"):
        if getattr(sys, stream) is None:
            setattr(sys, stream, open(os.devnull, "w", encoding="utf-8", errors="replace"))
    sys.excepthook = lambda *exc: traceback.print_exception(*exc)
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    app.setStyleSheet(QSS)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
