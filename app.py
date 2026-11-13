"""Lorota Converter — extrator de MP3/WAV pessoal, roda em http://127.0.0.1:5000"""
import html
import json
import re
import shutil
import socket
import time
import urllib.request
import subprocess
import sys
import threading
import uuid
import webbrowser
import zipfile
from pathlib import Path

import logging

from flask import Flask, jsonify, request, send_file, send_from_directory

# o yt-dlp é pesado de carregar (~1s); só é importado na primeira conversão
yt_dlp = None


def get_ytdlp():
    global yt_dlp
    if yt_dlp is None:
        import yt_dlp as _y
        yt_dlp = _y
    return yt_dlp


# Sem janela de console (.exe ou pythonw) não existe stdout/stderr — evita erro nos prints do Flask
import os
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

FROZEN = getattr(sys, "frozen", False)  # rodando como .exe (PyInstaller)
APP_DIR = Path(sys.executable).parent if FROZEN else Path(__file__).parent
RES_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))  # onde ficam os arquivos embutidos (static/)
DOWNLOADS = APP_DIR / "downloads"
DOWNLOADS.mkdir(exist_ok=True)

BROWSER_MODE = "--browser" in sys.argv  # modo antigo: abre no navegador, porta fixa 5000

# ffmpeg ao lado do .exe tem prioridade; senão, o do PATH
FFMPEG = str(APP_DIR / "ffmpeg.exe") if (APP_DIR / "ffmpeg.exe").exists() else shutil.which("ffmpeg")

VIDEO_FORMATS = {"mp4", "mov", "mkv", "webm", "avi"}

app = Flask(__name__, static_folder=str(RES_DIR / "static"))
logging.getLogger("werkzeug").setLevel(logging.ERROR)  # não registra cada pedido da página (menos trabalho à toa)
jobs: dict[str, dict] = {}
lock = threading.RLock()

# Quantas conversões rodam ao mesmo tempo. Os outros links esperam "na fila" e
# entram assim que abre vaga — dá pra colar quantos links quiser.
MAX_PARALLEL = 3
slots = threading.Semaphore(MAX_PARALLEL)
START_GAP = 1.5  # segundos entre o início de cada download (muitos pedidos juntos o YouTube barra)
_start_lock = threading.Lock()
_last_start = [0.0]

JOBS_FILE = DOWNLOADS / "lista.json"       # a lista sobrevive quando o app é fechado
SETTINGS_FILE = DOWNLOADS / "config.json"  # última pasta escolhida em "Baixar"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


# Itens cujo arquivo não foi achado ao abrir (ex.: HD externo/pendrive desconectado, pasta
# movida). Não aparecem na lista, mas continuam guardados no lista.json e voltam
# sozinhos quando o arquivo estiver no lugar de novo.
missing_jobs: list[dict] = []


def save_jobs():
    """Grava os itens prontos para a lista voltar na próxima vez que abrir o app."""
    with lock:
        done = [j for j in jobs.values() if j.get("status") == "pronto"]
        keep = done + [j for j in missing_jobs if j["id"] not in jobs]
    tmp = JOBS_FILE.with_suffix(".tmp")
    try:
        # grava num arquivo temporário e troca: se o PC desligar no meio, a lista não corrompe
        tmp.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(JOBS_FILE)
    except OSError:
        pass


def load_jobs():
    for j in read_json(JOBS_FILE, []):
        if not j.get("file") or not j.get("id"):
            continue
        if job_path(j).exists():
            jobs[j["id"]] = j
        else:
            missing_jobs.append(j)


def job_path(job: dict) -> Path:
    # arquivos novos guardam o caminho completo (ficam na pasta escolhida pelo usuário);
    # os antigos ficavam em downloads/<id>/
    return Path(job["path"]) if job.get("path") else DOWNLOADS / job["id"] / job["file"]


# ---------- pasta onde os arquivos prontos são salvos ----------
def load_settings() -> dict:
    return read_json(SETTINGS_FILE, {})


def save_settings(settings: dict):
    try:
        SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def save_dir() -> Path:
    """Pasta escolhida pelo usuário; se não existir mais (ex.: pendrive tirado), volta pra downloads."""
    folder = load_settings().get("save_folder")
    if folder and Path(folder).is_dir():
        return Path(folder)
    return DOWNLOADS


def update(job_id, **kw):
    with lock:
        if job_id not in jobs:  # removido da lista enquanto convertia
            return
        jobs[job_id].update(kw)
    if kw.get("status") == "pronto":
        save_jobs()


# ---------- Spotify ----------
# O áudio do Spotify é protegido, então não dá pra baixar de lá. O Lorota lê o nome,
# artista, álbum e capa da página pública da música, acha a mesma faixa no YouTube,
# baixa de lá e grava as informações do Spotify no arquivo.
SPOTIFY_TRACK = re.compile(r"https?://open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]+)")
SPOTIFY_ANY = re.compile(r"https?://(?:open\.)?spotify\.com/|https?://spotify\.link/")
# User-Agent simples: com um de navegador completo o Spotify devolve a página sem as tags com os dados
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read()


def spotify_meta(url: str) -> dict:
    m = SPOTIFY_TRACK.search(url)
    if not m:
        raise RuntimeError("Do Spotify, por enquanto só funcionam links de música (open.spotify.com/track/...).")
    page = _fetch(f"https://open.spotify.com/track/{m.group(1)}").decode("utf-8", "replace")

    def meta(prop):
        mm = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(prop), page)
        return html.unescape(mm.group(1)).strip() if mm else ""

    title = meta("og:title")
    parts = [p.strip() for p in meta("og:description").split("·")]  # "Artista · Álbum · Song · Ano"
    artist = meta("music:musician_description") or (parts[0] if parts else "")
    album = parts[1] if len(parts) > 1 else ""
    if not title or not artist:
        raise RuntimeError("Não consegui ler os dados dessa música no Spotify.")
    return {"title": title, "artist": artist, "album": album, "cover": meta("og:image")}


def safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "", s).strip()[:150] or "musica"


def retag_mp3(path: Path, sp: dict):
    """Troca capa e informações do MP3 pelas do Spotify."""
    cover = path.with_name("spotify-cover.jpg")
    tmp = path.with_name("retag.mp3")
    try:
        cover.write_bytes(_fetch(sp["cover"])) if sp.get("cover") else None
    except Exception:  # noqa: BLE001
        pass
    cmd = [FFMPEG or "ffmpeg", "-y", "-v", "error", "-i", str(path)]
    if cover.exists():
        cmd += ["-i", str(cover), "-map", "0:a", "-map", "1:v", "-c", "copy", "-disposition:v", "attached_pic",
                "-metadata:s:v", "title=Album cover", "-metadata:s:v", "comment=Cover (front)"]
    else:
        cmd += ["-map", "0", "-c", "copy"]
    cmd += ["-map_metadata", "-1", "-id3v2_version", "3",
            "-metadata", f"title={sp['title']}", "-metadata", f"artist={sp['artist']}",
            "-metadata", f"album={sp['album']}", str(tmp)]
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    if subprocess.run(cmd, creationflags=flags).returncode == 0:
        tmp.replace(path)
    cover.unlink(missing_ok=True)


def friendly_error(msg: str) -> str:
    low = msg.lower()
    if "not a bot" in low or "429" in low or "too many requests" in low:
        return "O YouTube pediu uma pausa (muitos pedidos seguidos). Espere um pouco e clique em Tentar de novo."
    if "private video" in low:
        return "Esse vídeo é privado."
    if "video unavailable" in low or "not available" in low:
        return "Esse conteúdo não está disponível (removido ou bloqueado na sua região)."
    if "unsupported url" in low:
        return "Esse site ou link não é suportado."
    return msg


def run_job(*args):
    job_id = args[0]
    with slots:  # espera vaga (no máximo MAX_PARALLEL ao mesmo tempo)
        for attempt in range(3):
            with _start_lock:  # espaça o início dos downloads
                wait = _last_start[0] + START_GAP - time.time()
                if wait > 0:
                    time.sleep(wait)
                _last_start[0] = time.time()
            if job_id not in jobs:
                return
            _run_job(*args)
            err = (jobs.get(job_id) or {}).get("error", "")
            if jobs.get(job_id, {}).get("status") != "erro" or "pediu uma pausa" not in err or attempt == 2:
                return
            # YouTube barrou por excesso de pedidos: espera e tenta de novo sozinho
            update(job_id, status=f"YouTube pediu pausa, tentando de novo em {10 * (attempt + 1)}s",
                   error=None, progress=0)
            time.sleep(10 * (attempt + 1))


def _run_job(job_id: str, url: str, quality: str, fmt: str, res: str = "1080"):
    job_dir = DOWNLOADS / job_id
    job_dir.mkdir(exist_ok=True)

    sp = None
    if SPOTIFY_ANY.match(url):
        try:
            update(job_id, status="lendo o Spotify")
            sp = spotify_meta(url)
            update(job_id, title=f"{sp['artist']} - {sp['title']}", thumbnail=sp["cover"],
                   status="procurando no YouTube")
            kind = "official video" if fmt in VIDEO_FORMATS else "audio"
            url = f"ytsearch1:{sp['artist']} - {sp['title']} {kind}"
        except Exception as e:  # noqa: BLE001
            update(job_id, status="erro", error=str(e))
            return

    def hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            pct = (d.get("downloaded_bytes", 0) / total * 100) if total else 0
            update(job_id, status="baixando", progress=round(pct, 1))
        elif d["status"] == "finished":
            update(job_id, status="convertendo", progress=100)

    opts = {
        "format": "bestaudio/best",
        "format_sort": ["abr", "asr"],  # prioriza a faixa de áudio de maior bitrate
        "outtmpl": str(job_dir / (safe_name(f"{sp['artist']} - {sp['title']}") + ".%(ext)s" if sp else "%(title)s.%(ext)s")),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
    }
    if fmt in VIDEO_FORMATS:
        # Vídeo + áudio juntos. Prefere H.264 + AAC (abre em qualquer player);
        # acima de 1080p o YouTube só tem VP9/AV1. WEBM prefere VP9 + Opus.
        res_sort = [f"res:{res}"] if res != "max" else ["res"]
        codecs = ["vcodec:vp9", "acodec:opus"] if fmt == "webm" else ["vcodec:h264", "acodec:aac"]
        opts["format"] = "bv*+ba/b"
        opts["format_sort"] = res_sort + codecs
        pps = [{"key": "FFmpegMetadata"}]
        if fmt == "avi":
            # AVI é um formato antigo: precisa recodificar (mais lento)
            opts["merge_output_format"] = "mkv"
            pps.append({"key": "FFmpegVideoConvertor", "preferedformat": "avi"})
        elif fmt == "webm":
            opts["merge_output_format"] = "webm"
            pps.append({"key": "FFmpegVideoConvertor", "preferedformat": "webm"})  # só recodifica se não vier webm
        else:
            # MP4 / MOV / MKV: só troca o "envelope", sem perder qualidade
            opts["merge_output_format"] = fmt
            pps.append({"key": "FFmpegVideoRemuxer", "preferedformat": fmt})
        if fmt in {"mp4", "mov", "mkv"}:
            opts["writethumbnail"] = True
            pps.append({"key": "EmbedThumbnail"})
        opts["postprocessors"] = pps
    elif fmt == "wav":
        # WAV 24-bit, mantendo a taxa de amostragem original (sem reamostrar).
        # O áudio decodificado da fonte vai direto pro WAV, sem arredondar pra 16-bit.
        # Não suporta capa embutida.
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "wav"},
        ]
        opts["postprocessor_args"] = {"extractaudio": ["-c:a", "pcm_s24le"]}
    else:
        opts["writethumbnail"] = True
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ]
    if FFMPEG:
        opts["ffmpeg_location"] = str(Path(FFMPEG).parent)

    try:
        with get_ytdlp().YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info.get("entries"):  # resultado de busca (Spotify)
            info = info["entries"][0] or {}
        mp3s = list(job_dir.glob(f"*.{fmt}"))
        if not mp3s:
            raise RuntimeError(f"Nenhum {fmt.upper()} foi gerado.")
        if sp and fmt == "mp3":
            update(job_id, status="colocando capa do Spotify")
            retag_mp3(mp3s[0], sp)
        # move o arquivo pronto pra pasta escolhida (sem sobrescrever nada que já esteja lá)
        dest_dir = save_dir()
        dest = dest_dir / unique_name(mp3s[0].name, set(), dest_dir)
        shutil.move(str(mp3s[0]), dest)
        shutil.rmtree(job_dir, ignore_errors=True)
        update(
            job_id,
            status="pronto",
            progress=100,
            file=dest.name,
            path=str(dest),
            title=sp["title"] if sp else (info.get("title") or mp3s[0].stem),
            uploader=f"{sp['artist']} · via Spotify" if sp else (info.get("uploader") or info.get("extractor_key")),
            thumbnail=sp["cover"] if sp else info.get("thumbnail"),
            size=dest.stat().st_size,
            format=fmt,
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("\x1b[0;31m", "").replace("\x1b[0m", "").replace("ERROR: ", "")
        update(job_id, status="erro", error=friendly_error(msg))


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.post("/api/download")
def start_download():
    data = request.get_json(force=True)
    url = (data.get("url") or "").strip()
    quality = str(data.get("quality") or "320")
    if quality not in {"128", "192", "256", "320"}:
        quality = "320"
    fmt = data.get("format") if data.get("format") in {"mp3", "wav", *VIDEO_FORMATS} else "mp3"
    res = str(data.get("res") or "1080")
    if res not in {"max", "1080", "720", "480"}:
        res = "1080"
    if not url.startswith(("http://", "https://")):
        return jsonify(error="Cole um link válido (http/https)."), 400
    job_id = uuid.uuid4().hex[:10]
    with lock:
        jobs[job_id] = {"id": job_id, "url": url, "status": "na fila", "progress": 0, "title": url,
                        "format": fmt, "quality": quality, "res": res, "created": time.time(), "downloaded": False}
    threading.Thread(target=run_job, args=(job_id, url, quality, fmt, res), daemon=True).start()
    return jsonify(jobs[job_id])


@app.post("/api/retry/<job_id>")
def retry_job(job_id):
    with lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "erro":
            return jsonify(error="Nada pra tentar de novo."), 400
        job.update(status="na fila", error=None, progress=0)
    shutil.rmtree(DOWNLOADS / job_id, ignore_errors=True)
    args = (job_id, job["url"], job.get("quality", "320"), job.get("format", "mp3"), job.get("res", "1080"))
    threading.Thread(target=run_job, args=args, daemon=True).start()
    return jsonify(job)


@app.get("/api/jobs")
def list_jobs():
    with lock:
        return jsonify(sorted(jobs.values(), key=lambda j: j.get("created", 0)))


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with lock:
        job = jobs.get(job_id)
    return (jsonify(job), 200) if job else (jsonify(error="não encontrado"), 404)


@app.delete("/api/jobs/<job_id>")
def delete_job(job_id):
    """Tira o item da lista. O arquivo pronto continua na pasta (só some o temporário)."""
    with lock:
        job = jobs.pop(job_id, None)
        missing_jobs[:] = [j for j in missing_jobs if j["id"] != job_id]
    if job:
        shutil.rmtree(DOWNLOADS / job_id, ignore_errors=True)
        save_jobs()
    return jsonify(ok=True)


@app.get("/api/settings")
def get_settings():
    folder = save_dir()
    return jsonify(save_folder=str(folder), is_default=(folder == DOWNLOADS))


@app.post("/api/settings")
def set_settings():
    data = request.get_json(force=True)
    settings = load_settings()
    if data.get("reset"):
        settings.pop("save_folder", None)
    else:
        folder = Path(data.get("save_folder") or "")
        if not folder.is_dir():
            return jsonify(error="Pasta não encontrada."), 400
        settings["save_folder"] = str(folder)
    save_settings(settings)
    return get_settings()


def ready_jobs(ids) -> list[dict]:
    with lock:
        return [jobs[i] for i in ids if i in jobs and jobs[i].get("status") == "pronto" and job_path(jobs[i]).exists()]


def unique_name(name: str, used: set, folder: Path | None = None) -> str:
    """Evita sobrescrever: "Música.mp3" -> "Música (2).mp3"."""
    stem, ext = os.path.splitext(name)
    n, cand = 1, name
    while cand.lower() in used or (folder and (folder / cand).exists()):
        n += 1
        cand = f"{stem} ({n}){ext}"
    used.add(cand.lower())
    return cand


def mark_downloaded(items):
    with lock:
        for j in items:
            j["downloaded"] = True
    save_jobs()


@app.post("/api/export")
def export_files():
    """Janela do app: copia os arquivos escolhidos para uma pasta de uma vez só."""
    data = request.get_json(force=True)
    dest = Path(data.get("dest") or "")
    items = ready_jobs(data.get("ids") or [])
    if not items:
        return jsonify(error="Nenhum arquivo pronto selecionado."), 400
    if not dest.is_dir():
        return jsonify(error="Pasta de destino não encontrada."), 400
    used: set = set()
    for j in items:
        shutil.copy2(job_path(j), dest / unique_name(j["file"], used, dest))
    mark_downloaded(items)
    subprocess.Popen(["explorer", str(dest)])
    return jsonify(ok=True, count=len(items), dest=str(dest))


@app.get("/api/zip")
def zip_files():
    """Modo navegador: entrega os arquivos escolhidos num .zip só."""
    items = ready_jobs((request.args.get("ids") or "").split(","))
    if not items:
        return jsonify(error="Nenhum arquivo pronto selecionado."), 400
    zdir = DOWNLOADS / "_zips"
    shutil.rmtree(zdir, ignore_errors=True)  # limpa zips de vezes anteriores
    zdir.mkdir()
    zpath = zdir / f"Lorota-{time.strftime('%Y-%m-%d_%H-%M')}.zip"
    used: set = set()
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:  # áudio/vídeo já é comprimido
        for j in items:
            z.write(job_path(j), unique_name(j["file"], used))
    mark_downloaded(items)
    return send_file(zpath, as_attachment=True, download_name=zpath.name)


@app.get("/api/file/<job_id>")
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job.get("status") != "pronto":
        return jsonify(error="Arquivo não disponível"), 404
    mark_downloaded([job])
    return send_file(job_path(job), as_attachment=True, download_name=job["file"])


@app.post("/api/reveal/<job_id>")
def reveal(job_id):
    """Abre o Explorer com o arquivo selecionado (ou a pasta onde os arquivos são salvos)."""
    job = jobs.get(job_id)
    if job and job.get("status") == "pronto" and job_path(job).exists():
        subprocess.Popen(["explorer", "/select,", str(job_path(job))])
    else:
        subprocess.Popen(["explorer", str(save_dir())])
    return jsonify(ok=True)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_browser():
    port = 5000
    url = f"http://127.0.0.1:{port}"
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:  # já está rodando
            webbrowser.open(url)
            return
    print(f"Lorota Converter rodando em {url} — feche esta janela para desligar.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)


class WindowApi:
    """Funções que a página chama dentro da janela do app (window.pywebview.api).

    IMPORTANTE: tudo que for público aqui o pywebview varre ao abrir a janela. A janela
    fica em `_window` (com _) — se ficasse pública, ele entraria no objeto nativo do
    Windows e ficaria varrendo sem fim, travando o app na abertura.
    """

    def __init__(self):
        self._window = None

    def choose_folder(self, start=None):
        """Abre a janela do Windows pra escolher uma pasta. Devolve o caminho ou None."""
        import webview

        settings = load_settings()
        start = start or settings.get("last_folder")
        if not start or not Path(start).is_dir():
            start = str(Path.home() / "Downloads")
        folder_dialog = getattr(getattr(webview, "FileDialog", None), "FOLDER", None) or webview.FOLDER_DIALOG
        result = self._window.create_file_dialog(folder_dialog, directory=start)
        if not result:
            return None
        folder = result[0] if isinstance(result, (list, tuple)) else result
        settings = load_settings()
        settings["last_folder"] = folder
        save_settings(settings)
        return folder


APP_ID = "LorotaCorp.LorotaConverter"
ICON_FILE = RES_DIR / "docs" / "icon.ico"


def set_taskbar_identity():
    """Faz o Windows tratar o app como "Lorota Converter", e não como Python, na barra de tarefas."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:  # noqa: BLE001
        pass


def set_window_icon(title: str):
    """Troca o ícone da janela (barra de tarefas e Alt+Tab) pelo do Lorota."""
    if sys.platform != "win32" or not ICON_FILE.exists():
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.SendMessageW.restype = wintypes.LPARAM
    IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON, ICON_SMALL, ICON_BIG = 1, 0x10, 0x80, 0, 1

    # acha a janela deste processo pelo título
    pid, found = os.getpid(), []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def check(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value == title:
                found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(check), 0)
    for hwnd in found:
        for size, kind in ((16, ICON_SMALL), (32, ICON_BIG)):
            hicon = user32.LoadImageW(None, str(ICON_FILE), IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if hicon:
                user32.SendMessageW(hwnd, WM_SETICON, kind, hicon)
    return found


def run_window():
    import webview  # pywebview: janela nativa usando o Edge WebView2 do Windows

    set_taskbar_identity()
    port = free_port()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    api = WindowApi()
    window = api._window = webview.create_window(
        "Lorota Converter",
        f"http://127.0.0.1:{port}/?app=1",
        js_api=api,
        width=920,
        height=860,
        min_size=(420, 600),
        background_color="#0d0716",
    )
    def on_shown():
        set_window_icon("Lorota Converter")  # sem retornar nada: o pywebview não aceita lista aqui

    window.events.shown += on_shown
    webview.start()  # bloqueia até fechar a janela; aí o programa encerra


load_jobs()

if __name__ == "__main__":
    run_browser() if BROWSER_MODE else run_window()
