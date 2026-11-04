import collections, hashlib, json, os, re, shutil, subprocess, sys, threading, webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import unquote, urlparse, quote, parse_qs

for _st in (sys.stdout, sys.stderr):
    try: _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

ROOT = os.path.dirname(os.path.abspath(__file__))
VIDEOS = os.path.join(ROOT, "videos")      # сюда кладём папки-плейлисты
SHORTS = os.path.join(ROOT, "shorts")      # сюда скачиваются Shorts
SHORTS_ARCHIVE = os.path.join(ROOT, "data", "shorts_archive.txt")   # что уже скачано, чтобы не качать повторно
SHORTS_LIMIT = 2
LIKES = os.path.join(ROOT, "data", "likes.json")
COLLECTIONS = os.path.join(ROOT, "data", "collections.json")        # «мои плейлисты» (Смотреть позже и т.д.)
EXT = {".mp4", ".mkv", ".webm", ".mov"}
PORT = 8765
THUMBS = os.path.join(ROOT, "data", "thumbs")
FFMPEG = shutil.which("ffmpeg") or next((p for p in (
    os.path.join(ROOT, "ffmpeg", "ffmpeg.exe"), os.path.join(ROOT, "ffmpeg.exe"))
    if os.path.isfile(p)), None)
SIDE = (".jpg", ".jpeg", ".png", ".webp")   # обложки, скачанные yt-dlp рядом с видео
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
COOKIES_BROWSER = "firefox"   # браузер, из которого yt-dlp берёт cookies; None - не использовать
FIREFOX_CACHE = None          # путь к cache2 (или cache2\entries) вручную, если авто-поиск не нашёл кэш
YT_HOSTS = ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be")

def safe_parts(parts):
    return bool(parts) and all(p and p != ".." and ":" not in p and "\\" not in p for p in parts)

def get_chapters(rel):
    """Эпизоды из <видео>.info.json, который сохраняет yt-dlp"""
    parts = rel.split("/")
    if not safe_parts(parts): return []
    base = os.path.splitext(os.path.join(VIDEOS, *parts))[0]
    try:
        with open(base + ".info.json", encoding="utf-8") as f: info = json.load(f)
    except Exception: return []
    out = []
    for c in info.get("chapters") or []:
        try: st, en = float(c["start_time"]), float(c["end_time"])
        except Exception: continue
        if en > st: out.append({"start": st, "end": en, "title": str(c.get("title") or "")})
    return out

def sidecar(video_path):
    base = os.path.splitext(video_path)[0]
    for e in SIDE:
        if os.path.isfile(base + e) and os.path.getsize(base + e) > 0: return base + e
    return None

SEM = threading.Semaphore(2)  # не больше 2 ffmpeg одновременно

THUMB_ERR = {}  # rel -> текст последней ошибки

def _run_ffmpeg(args, tries):
    """Пробуем несколько способов запуска (обёртка Chocolatey бывает капризной)"""
    last = ""
    for kw in tries:
        try:
            r = subprocess.run([FFMPEG] + args, stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, timeout=90, **kw)
            last = "код %s: %s" % (r.returncode, r.stderr.decode("utf-8", "replace").strip()[-400:])
        except Exception as e:
            last = repr(e)
    return last

def make_thumb(rel):
    """Кадр из видео -> data/thumbs/<hash>.jpg (кэшируется)"""
    parts = rel.split("/")
    if not safe_parts(parts):
        THUMB_ERR[rel] = "плохой путь"; return None
    src = os.path.join(VIDEOS, *parts)
    if not os.path.isfile(src):
        THUMB_ERR[rel] = "файл не найден: " + src; print("[превью]", THUMB_ERR[rel]); return None
    side = sidecar(src)
    if side: return side                      # оригинальная обложка с YouTube
    if not FFMPEG:
        THUMB_ERR[rel] = "ffmpeg не найден"; return None
    out = os.path.join(THUMBS, hashlib.md5(rel.encode("utf-8")).hexdigest() + ".jpg")
    if os.path.isfile(out) and os.path.getsize(out) > 0: return out
    os.makedirs(THUMBS, exist_ok=True)
    hide = {"creationflags": 0x08000000} if os.name == "nt" else {}
    tries = [dict(stdin=subprocess.DEVNULL, **hide), dict(stdin=subprocess.DEVNULL), {}]
    err = ""
    with SEM:
        for ss in ("5", "0"):
            args = ["-v", "error", "-ss", ss, "-i", src, "-frames:v", "1",
                    "-vf", "scale=480:-2,format=yuvj420p", "-q:v", "4", "-update", "1", "-y", out]
            for kw in tries:
                err = _run_ffmpeg(args, [kw])
                if os.path.isfile(out) and os.path.getsize(out) > 0:
                    THUMB_ERR.pop(rel, None); return out
    THUMB_ERR[rel] = err or "ffmpeg ничего не создал"
    print("[превью] не удалось:", os.path.basename(src), "|", THUMB_ERR[rel])
    return None

# index.html ищем в app/, а если папки нет - рядом со server.py
APP = os.path.join(ROOT, "app")
if not os.path.isfile(os.path.join(APP, "index.html")):
    APP = ROOT

def scan():
    """videos/<Плейлист>/001 - Название.mp4 -> JSON-библиотека"""
    playlists = []
    if os.path.isdir(VIDEOS):
        for pl in sorted(os.listdir(VIDEOS)):
            d = os.path.join(VIDEOS, pl)
            if not os.path.isdir(d): continue
            items = []
            for f in sorted(os.listdir(d)):
                name, ext = os.path.splitext(f)
                if ext.lower() not in EXT: continue
                if re.search(r"\.f\d+(-\w+)?$", name): continue   # недокачанный поток yt-dlp
                title = re.sub(r"^\d+\s*-\s*", "", name)

                # Читаем информацию, сохранённую yt-dlp
                info_path = os.path.join(d, name + ".info.json")
                try:
                    with open(info_path, encoding="utf-8") as file:
                        info = json.load(file)
                except Exception:
                    info = {}

                video = {
                    "id": f"{pl}/{f}",
                    "title": title,
                    "playlist": pl,
                    "url": "/videos/" + quote(pl) + "/" + quote(f),
                    "thumb": "/thumb?id=" + quote(pl + "/" + f)
                             + ("&s=1" if sidecar(os.path.join(d, f)) else ""),

                    # Информация с YouTube
                    "description": info.get("description") or "",
                    "views": info.get("view_count"),
                    "youtube_likes": info.get("like_count"),
                    "uploader": info.get("uploader") or "",
                    "upload_date": info.get("upload_date") or "",
                    "duration": info.get("duration")
                }
                items.append(video)
            playlists.append({"name": pl, "videos": items})
    return {"playlists": playlists}

def scan_shorts():
    """shorts/<id>.mp4 (+ .jpg, .info.json) -> список для раздела Shorts, новые сверху"""
    if not os.path.isdir(SHORTS): return []
    files = []
    for f in os.listdir(SHORTS):
        name, ext = os.path.splitext(f)
        if ext.lower() in EXT and not re.search(r"\.f\d+(-\w+)?$", name):
            files.append(f)
    files.sort(key=lambda f: os.path.getmtime(os.path.join(SHORTS, f)), reverse=True)
    out = []
    for f in files:
        name = os.path.splitext(f)[0]
        try:
            with open(os.path.join(SHORTS, name + ".info.json"), encoding="utf-8") as fh:
                info = json.load(fh)
        except Exception:
            info = {}
        poster = ""
        for e in SIDE:
            if os.path.isfile(os.path.join(SHORTS, name + e)):
                poster = "/shortsfiles/" + quote(name + e); break
        out.append({
            "id": "shorts/" + f,
            "title": info.get("title") or name,
            "uploader": info.get("uploader") or "",
            "views": info.get("view_count"),
            "youtube_likes": info.get("like_count"),
            "description": info.get("description") or "",
            "url": "/shortsfiles/" + quote(f),
            "poster": poster,
        })
    return out


# ───────────── поиск Shorts в кэше Firefox ─────────────
# Firefox хранит кэш в cache2/entries: один файл на ответ сервера, в конце файла - метаданные
# с адресом (ключом). Страницы с рекомендациями грузят превью роликов, поэтому по адресам
# превью и ссылкам /shorts/<id> можно понять, какие Shorts вам показывали.
KEY_RE = re.compile(rb":(https?://[^\x00]{8,2000})\x00")
SHORT_KEY = re.compile(
    rb"youtube\.com/shorts/([\w-]{11})(?![\w-])"
    rb"|ytimg\.com/vi(?:_webp)?/([\w-]{11})/(?:oar\w*|frame0)\.(?:jpg|webp)")   # вертикальные превью Shorts

def firefox_cache_dirs():
    if FIREFOX_CACHE:
        p = FIREFOX_CACHE
        for cand in (p, os.path.join(p, "entries"), os.path.join(p, "cache2", "entries")):
            if os.path.isdir(cand) and os.path.basename(cand) == "entries": return [cand]
        return []
    home = os.path.expanduser("~")
    bases = []
    if os.name == "nt":
        for env in ("LOCALAPPDATA", "APPDATA"):
            b = os.environ.get(env)
            if b: bases.append(os.path.join(b, "Mozilla", "Firefox", "Profiles"))
    elif sys.platform == "darwin":
        bases.append(os.path.join(home, "Library", "Caches", "Firefox", "Profiles"))
    else:
        bases += [os.path.join(home, ".cache", "mozilla", "firefox"),
                  os.path.join(home, "snap", "firefox", "common", ".cache", "mozilla", "firefox")]
    dirs = []
    for b in bases:
        if not os.path.isdir(b): continue
        for prof in os.listdir(b):
            d = os.path.join(b, prof, "cache2", "entries")
            if os.path.isdir(d): dirs.append(d)
    return dirs

def cache_keys(path):
    """Адреса (ключи) из метаданных одного файла кэша"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > 8:
                f.seek(size - 4)
                off = int.from_bytes(f.read(4), "big")      # последние 4 байта - смещение метаданных
            else:
                off = size
            if not (0 < off < size): off = max(0, size - 16384)
            f.seek(off); meta = f.read()
    except OSError:
        return []
    return [m.group(1) for m in KEY_RE.finditer(meta)]

def archived_ids():
    try:
        with open(SHORTS_ARCHIVE, encoding="utf-8") as f:
            return {ln.split()[-1] for ln in f if ln.strip()}
    except OSError:
        return set()

def firefox_short_ids(limit, log):
    dirs = firefox_cache_dirs()
    if not dirs:
        log("Кэш Firefox не найден. Укажите путь в переменной FIREFOX_CACHE в server.py.")
        return []
    entries = []
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_file(): entries.append((e.stat().st_mtime, e.path))
                    except OSError: pass
        except OSError: pass
    entries.sort(reverse=True)                     # сначала самое свежее
    log("Профилей с кэшем: %d, файлов в кэше: %d" % (len(dirs), len(entries)))
    seen = archived_ids(); skipped = len(seen & set())
    found = []
    for _, path in entries:
        for key in cache_keys(path):
            for m in SHORT_KEY.finditer(key):
                vid = (m.group(1) or m.group(2)).decode("ascii", "ignore")
                if vid in seen: continue
                seen.add(vid); found.append(vid)
        if len(found) >= limit: break
    return found[:limit]


JOB = {"status": "idle", "kind": "playlist", "url": "", "thumbs_only": False, "percent": 0.0, "item": "",
       "lines": collections.deque(maxlen=60), "seq": 0, "code": None,
       "queue": [], "queue_index": 0, "queue_total": 0, "failed": []}
JOB_LOCK = threading.Lock()
PROC = None

def valid_url(u):
    p = urlparse(u.strip())
    return p.scheme in ("http", "https") and (p.hostname or "") in YT_HOSTS and "list=" in (p.query or "")

def ytdlp_base():
    cmd = [sys.executable, "-m", "yt_dlp", "--remote-components", "ejs:github"]
    if COOKIES_BROWSER: cmd += ["--cookies-from-browser", COOKIES_BROWSER]
    if FFMPEG and not shutil.which("ffmpeg"): cmd += ["--ffmpeg-location", FFMPEG]
    return cmd

def ytdlp_cmd(url, thumbs_only):
    cmd = ytdlp_base()
    cmd += ["--yes-playlist", "-c", "--retries", "20", "--fragment-retries", "20",
            "--concurrent-fragments", "1", "--newline",
            "-P", VIDEOS, "-o", "%(playlist_title)s/%(playlist_index)03d - %(title)s.%(ext)s",
            "-f", "bv*+ba/b", "--merge-output-format", "mp4",
            "--write-thumbnail", "--convert-thumbnails", "jpg",
            "--write-info-json", "--embed-chapters"]
    if thumbs_only: cmd.append("--skip-download")
    return cmd + [url.strip()]

def shorts_cmd(ids):
    cmd = ytdlp_base()
    cmd += ["--no-playlist", "-c", "--retries", "20", "--fragment-retries", "20",
            "--concurrent-fragments", "1", "--newline",
            "-P", SHORTS, "-o", "%(id)s.%(ext)s",
            "-f", "bv*[vcodec^=avc1]+ba[acodec^=mp4a]/b[ext=mp4]/bv*+ba/b", "--merge-output-format", "mp4",
            "--match-filters", "duration<=190",      # отсеивает обычные длинные видео, попавшие в список
            "--write-thumbnail", "--convert-thumbnails", "jpg", "--write-info-json",
            "--download-archive", SHORTS_ARCHIVE]
    return cmd + ["https://www.youtube.com/shorts/" + i for i in ids]

def job_snapshot():
    with JOB_LOCK:
        d = dict(JOB); d["lines"] = list(JOB["lines"]); return d

def job_log(text):
    with JOB_LOCK: JOB["lines"].append(text)

def _execute(cmd, total=0, finalize=True):
    """Запуск yt-dlp и разбор его вывода в JOB (общий для плейлистов и Shorts)"""
    global PROC
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    flags = 0x08000000 if os.name == "nt" else 0
    code = None; seen = 0
    try:
        PROC = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, creationflags=flags)
        for raw in PROC.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line: continue
            m = re.search(r"Downloading item (\d+) of (\d+)", line)
            pm = re.search(r"\[download\]\s+([\d.]+)%", line)
            ex = total and re.search(r"\[youtube\] Extracting URL", line)
            with JOB_LOCK:
                if m: JOB["item"] = "Видео %s из %s" % m.groups(); JOB["percent"] = 0.0
                if ex:
                    seen += 1; JOB["item"] = "Ролик %d из %d" % (min(seen, total), total); JOB["percent"] = 0.0
                if pm: JOB["percent"] = float(pm.group(1))
                else: JOB["lines"].append(line[-300:])
        code = PROC.wait()
    except Exception as e:
        job_log("Не удалось запустить yt-dlp: %r" % e)
    with JOB_LOCK:
        if finalize:
            if JOB["status"] != "cancelled":
                JOB["status"] = "done" if code == 0 else "error"
            JOB["code"] = code; JOB["seq"] += 1
            if JOB["status"] == "done": JOB["percent"] = 100.0
    PROC = None
    return code

def _run_job(urls, thumbs_only):
    failed = []
    for index, url in enumerate(urls, 1):
        with JOB_LOCK:
            if JOB["status"] == "cancelled": break
            JOB["queue_index"] = index
            JOB["url"] = url
            JOB["item"] = "Плейлист %d из %d" % (index, len(urls))
            JOB["percent"] = 0.0
        job_log("\n[%d/%d] Начинаю: %s" % (index, len(urls), url))
        code = _execute(ytdlp_cmd(url, thumbs_only), finalize=False)
        with JOB_LOCK:
            cancelled = JOB["status"] == "cancelled"
        if cancelled: break
        if code != 0:
            failed.append({"url": url, "code": code})
            job_log("Ошибка этого плейлиста; перехожу к следующему.")
        else:
            job_log("Плейлист завершён успешно.")
    with JOB_LOCK:
        if JOB["status"] != "cancelled":
            JOB["status"] = "done_with_errors" if failed else "done"
            JOB["percent"] = 100.0
        JOB["failed"] = failed
        JOB["code"] = 1 if failed else 0
        JOB["seq"] += 1

def _run_shorts_job():
    try:
        ids = firefox_short_ids(SHORTS_LIMIT, job_log)
    except Exception as e:
        ids = []; job_log("Ошибка при чтении кэша: %r" % e)
    if not ids:
        job_log("Новых Shorts в кэше не найдено. Пролистайте Shorts/рекомендации в Firefox и повторите.")
        with JOB_LOCK:
            JOB["status"] = "error"; JOB["seq"] += 1
        return
    job_log("Найдено новых Shorts: %d" % len(ids))
    os.makedirs(SHORTS, exist_ok=True)
    os.makedirs(os.path.dirname(SHORTS_ARCHIVE), exist_ok=True)
    with JOB_LOCK: JOB["item"] = "Ролик 0 из %d" % len(ids)
    _execute(shorts_cmd(ids), total=len(ids))

def start_job(raw_urls, thumbs_only):
    urls = [u.strip() for u in str(raw_urls or "").splitlines() if u.strip()]
    if not urls:
        return {"ok": False, "error": "Вставь хотя бы одну ссылку — по одной на строку."}
    bad = [u for u in urls if not valid_url(u)]
    if bad:
        return {"ok": False, "error": "Некорректная ссылка (нужна ссылка YouTube с list=):\n" + bad[0]}
    with JOB_LOCK:
        if JOB["status"] == "running":
            return {"ok": False, "error": "Скачивание уже идёт."}
        JOB.update(status="running", kind="playlist", url="\n".join(urls), thumbs_only=thumbs_only, percent=0.0,
                   item="Подготовка очереди…", code=None, queue=urls, queue_index=0, queue_total=len(urls), failed=[]); JOB["lines"].clear()
    threading.Thread(target=_run_job, args=(urls, thumbs_only), daemon=True).start()
    return {"ok": True, "count": len(urls)}

def start_shorts_job():
    with JOB_LOCK:
        if JOB["status"] == "running":
            return {"ok": False, "error": "Скачивание уже идёт."}
        JOB.update(status="running", kind="shorts", url="Shorts из кэша Firefox", thumbs_only=False, percent=0.0,
                   item="Поиск в кэше Firefox…", code=None); JOB["lines"].clear()
    threading.Thread(target=_run_shorts_job, daemon=True).start()
    return {"ok": True}

def stop_job():
    global PROC
    with JOB_LOCK:
        if JOB["status"] != "running": return {"ok": False}
        JOB["status"] = "cancelled"
        p = PROC
    if p is not None:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
            else: p.terminate()
        except Exception: pass
    return {"ok": True}

def read_json_file(path, default):
    try:
        with open(path, encoding="utf-8") as f: return json.load(f)
    except Exception:
        return default

def write_bytes(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f: f.write(data)

class Srv(ThreadingHTTPServer):
    # На Windows SO_REUSEADDR позволяет двум серверам занять один порт - запрещаем
    allow_reuse_address = (os.name != "nt")

class H(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

    def handle(self):  # обрыв соединения браузером - это нормально
        try: super().handle()
        except OSError: pass

    def translate_path(self, path):
        p = unquote(urlparse(path).path)
        if p.startswith("/videos/"):
            return os.path.join(VIDEOS, *p[8:].split("/"))
        if p.startswith("/shortsfiles/"):
            parts = p[13:].split("/")
            if not safe_parts(parts): return os.path.join(SHORTS, "__нет__")
            return os.path.join(SHORTS, *parts)
        return os.path.join(APP, *p.lstrip("/").split("/"))

    def json(self, obj):
        b = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        p = urlparse(self.path).path
        if p == "/api/library": return self.json(scan())
        if p == "/api/shorts": return self.json(scan_shorts())
        if p == "/api/download/status": return self.json(job_snapshot())
        if p == "/api/chapters":
            return self.json({"chapters": get_chapters(parse_qs(urlparse(self.path).query).get("id", [""])[0])})
        if p == "/thumb":
            rel = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            out = make_thumb(rel)
            if not out:
                msg = ("Не удалось сделать превью: " + THUMB_ERR.get(rel, "?")).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers(); self.wfile.write(msg); return
            b = open(out, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", MIME.get(os.path.splitext(out)[1].lower(), "image/jpeg"))
            self.send_header("Cache-Control", "max-age=86400")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b); return
        if p == "/api/likes": return self.json(read_json_file(LIKES, []))
        if p == "/api/collections": return self.json(read_json_file(COLLECTIONS, []))
        if (p.startswith("/videos/") or p.startswith("/shortsfiles/")) and self.headers.get("Range"):
            return self.ranged(self.translate_path(self.path))
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/likes":
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            write_bytes(LIKES, data)
            return self.json({"ok": True})
        if p in ("/api/download", "/api/download/cancel", "/api/shorts/download", "/api/collections"):
            if self.headers.get("X-OfflineTube") != "1":   # защита от запросов с чужих сайтов
                return self.send_error(403)
            if p == "/api/collections":
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                try: json.loads(data.decode("utf-8"))
                except Exception: return self.send_error(400)
                write_bytes(COLLECTIONS, data)
                return self.json({"ok": True})
            if p.endswith("cancel"): return self.json(stop_job())
            if p == "/api/shorts/download": return self.json(start_shorts_job())
            try: d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception: d = {}
            return self.json(start_job(str(d.get("url", "")), bool(d.get("thumbs_only"))))
        self.send_error(404)

    def ranged(self, path):  # нужен для перемотки видео
        if not os.path.isfile(path): return self.send_error(404)
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers["Range"])
        if not m: return super().do_GET()
        size = os.path.getsize(path)
        start = int(m.group(1) or 0)
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start); left = end - start + 1
            while left > 0:
                chunk = f.read(min(1 << 20, left))
                if not chunk: break
                try: self.wfile.write(chunk)
                except OSError: break
                left -= len(chunk)

if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "data"), exist_ok=True)
    os.makedirs(VIDEOS, exist_ok=True)
    os.makedirs(SHORTS, exist_ok=True)
    if not os.path.isfile(os.path.join(APP, "index.html")):
        print("ОШИБКА: не найден index.html (ожидается в папке app рядом с server.py)")
        input("Нажмите Enter..."); sys.exit(1)
    if FFMPEG:
        try:
            r = subprocess.run([FFMPEG, "-version"], capture_output=True, timeout=20, stdin=subprocess.DEVNULL)
            print("Проверка ffmpeg:", (r.stdout.decode("utf-8", "replace").splitlines() or ["пустой ответ"])[0])
        except Exception as e:
            print("Проверка ffmpeg: ОШИБКА", repr(e))
    try:
        srv = Srv(("127.0.0.1", PORT), H)
    except OSError:
        print("\nОШИБКА: порт %d уже занят. OfflineTube уже запущен в другом окне." % PORT)
        print("Закройте то окно (или в PyCharm нажмите Stop) и запустите снова.")
        input("Нажмите Enter..."); sys.exit(1)
    url = f"http://127.0.0.1:{PORT}/"
    print("Видео:", VIDEOS, "| плейлистов:", len(scan()["playlists"]))
    print("OfflineTube запущен:", url, "| ffmpeg:", FFMPEG or "не найден (превью не будут создаваться)", "\nЗакройте это окно, чтобы выключить.")
    threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try: srv.serve_forever()
    except KeyboardInterrupt: sys.exit(0)