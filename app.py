"""Lorota Converter — extrator de MP3/WAV pessoal, roda em http://127.0.0.1:5000"""
import shutil
import socket
import subprocess
import sys
import threading
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, jsonify, request, send_file, send_from_directory
import yt_dlp

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
jobs: dict[str, dict] = {}
lock = threading.Lock()


def update(job_id, **kw):
    with lock:
        jobs[job_id].update(kw)


def run_job(job_id: str, url: str, quality: str, fmt: str, res: str = "1080"):
    job_dir = DOWNLOADS / job_id
    job_dir.mkdir(exist_ok=True)

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
        "outtmpl": str(job_dir / "%(title)s.%(ext)s"),
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
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        mp3s = list(job_dir.glob(f"*.{fmt}"))
        if not mp3s:
            raise RuntimeError(f"Nenhum {fmt.upper()} foi gerado.")
        update(
            job_id,
            status="pronto",
            progress=100,
            file=mp3s[0].name,
            title=info.get("title") or mp3s[0].stem,
            uploader=info.get("uploader") or info.get("extractor_key"),
            thumbnail=info.get("thumbnail"),
            size=mp3s[0].stat().st_size,
            format=fmt,
        )
    except Exception as e:  # noqa: BLE001
        msg = str(e).replace("\x1b[0;31m", "").replace("\x1b[0m", "")
        update(job_id, status="erro", error=msg)


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
        jobs[job_id] = {"id": job_id, "url": url, "status": "na fila", "progress": 0, "title": url}
    threading.Thread(target=run_job, args=(job_id, url, quality, fmt, res), daemon=True).start()
    return jsonify(jobs[job_id])


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    with lock:
        job = jobs.get(job_id)
    return (jsonify(job), 200) if job else (jsonify(error="não encontrado"), 404)


@app.get("/api/file/<job_id>")
def get_file(job_id):
    job = jobs.get(job_id)
    if not job or job.get("status") != "pronto":
        return jsonify(error="Arquivo não disponível"), 404
    path = DOWNLOADS / job_id / job["file"]
    return send_file(path, as_attachment=True, download_name=job["file"])


@app.post("/api/reveal/<job_id>")
def reveal(job_id):
    """Abre o Explorer com o arquivo selecionado (ou a pasta downloads)."""
    job = jobs.get(job_id)
    if job and job.get("status") == "pronto":
        subprocess.Popen(["explorer", "/select,", str(DOWNLOADS / job_id / job["file"])])
    else:
        subprocess.Popen(["explorer", str(DOWNLOADS)])
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


def run_window():
    import webview  # pywebview: janela nativa usando o Edge WebView2 do Windows

    port = free_port()
    threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    ).start()
    webview.create_window(
        "Lorota Converter",
        f"http://127.0.0.1:{port}/?app=1",
        width=920,
        height=820,
        min_size=(420, 600),
        background_color="#0d0716",
    )
    webview.start()  # bloqueia até fechar a janela; aí o programa encerra


if __name__ == "__main__":
    run_browser() if BROWSER_MODE else run_window()
