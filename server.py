import collections, hashlib, json, os, re, shutil, subprocess, sys, threading, webbrowser
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import unquote, urlparse, quote, parse_qs

for _st in (sys.stdout, sys.stderr):
    try: _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

ROOT = os.path.dirname(os.path.abspath(__file__))
VIDEOS = os.path.join(ROOT, "videos")      # сюда кладём папки-плейлисты
LIKES = os.path.join(ROOT, "data", "likes.json")
EXT = {".mp4", ".mkv", ".webm", ".mov"}
PORT = 8765
THUMBS = os.path.join(ROOT, "data", "thumbs")
FFMPEG = shutil.which("ffmpeg") or next((p for p in (
    os.path.join(ROOT, "ffmpeg", "ffmpeg.exe"), os.path.join(ROOT, "ffmpeg.exe"))
    if os.path.isfile(p)), None)
SIDE = (".jpg", ".jpeg", ".png", ".webp")   # обложки, скачанные yt-dlp рядом с видео
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
COOKIES_BROWSER = "firefox"   # браузер, из которого yt-dlp берёт cookies; None - не использовать
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
                items.append({"id": f"{pl}/{f}", "title": title,
                              "playlist": pl, "url": "/videos/" + quote(pl) + "/" + quote(f),
                              "thumb": "/thumb?id=" + quote(pl + "/" + f)
                              + ("&s=1" if sidecar(os.path.join(d, f)) else "")})
            playlists.append({"name": pl, "videos": items})
    return {"playlists": playlists}


JOB = {"status": "idle", "url": "", "thumbs_only": False, "percent": 0.0, "item": "",
       "lines": collections.deque(maxlen=60), "seq": 0, "code": None}
JOB_LOCK = threading.Lock()
PROC = None

def valid_url(u):
    p = urlparse(u.strip())
    return p.scheme in ("http", "https") and (p.hostname or "") in YT_HOSTS and "list=" in (p.query or "")

def ytdlp_cmd(url, thumbs_only):
    cmd = [sys.executable, "-m", "yt_dlp", "--remote-components", "ejs:github"]
    if COOKIES_BROWSER: cmd += ["--cookies-from-browser", COOKIES_BROWSER]
    if FFMPEG and not shutil.which("ffmpeg"): cmd += ["--ffmpeg-location", FFMPEG]
    cmd += ["--yes-playlist", "-c", "--retries", "20", "--fragment-retries", "20",
            "--concurrent-fragments", "1", "--newline",
            "-P", VIDEOS, "-o", "%(playlist_title)s/%(playlist_index)03d - %(title)s.%(ext)s",
            "-f", "bv*+ba/b", "--merge-output-format", "mp4",
            "--write-thumbnail", "--convert-thumbnails", "jpg",
            "--write-info-json", "--embed-chapters"]
    if thumbs_only: cmd.append("--skip-download")
    return cmd + [url.strip()]

def job_snapshot():
    with JOB_LOCK:
        d = dict(JOB); d["lines"] = list(JOB["lines"]); return d

def _run_job(url, thumbs_only):
    global PROC
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    flags = 0x08000000 if os.name == "nt" else 0
    code = None
    try:
        PROC = subprocess.Popen(ytdlp_cmd(url, thumbs_only), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=env, creationflags=flags)
        for raw in PROC.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            if not line: continue
            m = re.search(r"Downloading item (\d+) of (\d+)", line)
            pm = re.search(r"\[download\]\s+([\d.]+)%", line)
            with JOB_LOCK:
                if m: JOB["item"] = "Видео %s из %s" % m.groups(); JOB["percent"] = 0.0
                if pm: JOB["percent"] = float(pm.group(1))
                else: JOB["lines"].append(line[-300:])
        code = PROC.wait()
    except Exception as e:
        with JOB_LOCK: JOB["lines"].append("Не удалось запустить yt-dlp: %r" % e)
    with JOB_LOCK:
        if JOB["status"] != "cancelled":
            JOB["status"] = "done" if code == 0 else "error"
        JOB["code"] = code; JOB["seq"] += 1
        if JOB["status"] == "done": JOB["percent"] = 100.0
    PROC = None

def start_job(url, thumbs_only):
    if not valid_url(url):
        return {"ok": False, "error": "Нужна ссылка на плейлист YouTube, в ней должно быть list=…"}
    with JOB_LOCK:
        if JOB["status"] == "running":
            return {"ok": False, "error": "Скачивание уже идёт."}
        JOB.update(status="running", url=url.strip(), thumbs_only=thumbs_only, percent=0.0,
                   item="", code=None); JOB["lines"].clear()
    threading.Thread(target=_run_job, args=(url, thumbs_only), daemon=True).start()
    return {"ok": True}

def stop_job():
    p = PROC
    with JOB_LOCK:
        if JOB["status"] != "running" or p is None: return {"ok": False}
        JOB["status"] = "cancelled"
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else: p.terminate()
    except Exception: pass
    return {"ok": True}

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
        if p == "/api/likes":
            try: return self.json(json.load(open(LIKES, encoding="utf-8")))
            except Exception: return self.json([])
        if p.startswith("/videos/") and self.headers.get("Range"):
            return self.ranged(self.translate_path(self.path))
        return super().do_GET()

    def do_POST(self):
        p = urlparse(self.path).path
        if p == "/api/likes":
            data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with open(LIKES, "wb") as f: f.write(data)
            return self.json({"ok": True})
        if p in ("/api/download", "/api/download/cancel"):
            if self.headers.get("X-OfflineTube") != "1":   # защита от запросов с чужих сайтов
                return self.send_error(403)
            if p.endswith("cancel"): return self.json(stop_job())
            try: d = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            except Exception: d = {}
            return self.json(start_job(str(d.get("url", "")), bool(d.get("thumbs_only"))))
        self.send_error(404)

    def ranged(self, path):  # нужен для перемотки видео
        if not os.path.isfile(path): return self.send_error(404)
        size = os.path.getsize(path)
        m = re.match(r"bytes=(\d*)-(\d*)", self.headers["Range"])
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