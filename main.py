"""
main.py — Deezer Flask App
━━━━━━━━━━━━━━━━━━━━━━━━━━
Servidor Flask que serve a UI e expõe rotas para:
  • GET  /                      → player HTML
  • GET  /search?q=...          → busca JSON
  • GET  /album/<id>            → faixas do álbum JSON
  • GET  /stream?url=...        → stream de áudio + limpeza automática
  • GET  /health                → status da sessão

Uso:
  pip install -r requirements.txt
  python main.py
"""

import json
import logging
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv
from deezer import Deezer
from deemix import generateDownloadObject
from deemix.downloader import Downloader
from deemix.settings import load as load_settings
from flask import Flask, Response, jsonify, render_template_string, request, send_file, stream_with_context

# ── Config ─────────────────────────────────────────────────────────────────────
load_dotenv()

ARL          = os.getenv("DEEZER_ARL", "").strip()
DOWNLOAD_DIR = Path(tempfile.gettempdir()) / "deezer_stream_tmp"
DOWNLOAD_DIR.mkdir(exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("deezer_flask")

app = Flask(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# SESSÃO DEEZER
# ══════════════════════════════════════════════════════════════════════════════

class Session:
    def __init__(self):
        self.dz       = None
        self.settings = None
        self.ok       = False
        self.user     = {}

    def init(self, arl: str):
        log.info("🔐 Autenticando ARL...")
        self.dz = Deezer()
        if not self.dz.login_via_arl(arl):
            raise RuntimeError("ARL inválido ou expirado.")
        self.user = self.dz.current_user or {}
        self.settings = load_settings(None)
        self.settings["downloadLocation"] = str(DOWNLOAD_DIR)
        self.settings["maxBitrate"]       = "3"   # MP3 320
        self.settings["overwriteFile"]    = "y"
        self.ok = True
        log.info(f"✅ Logado como: {self.user.get('name','?')}")

session = Session()

if ARL:
    try:
        session.init(ARL)
    except Exception as e:
        log.error(f"❌ Falha na autenticação: {e}")
else:
    log.error("❌ ARL não encontrado no .env!")

# ══════════════════════════════════════════════════════════════════════════════
# UTILITÁRIOS
# ══════════════════════════════════════════════════════════════════════════════

DEEZER_API = "https://api.deezer.com"
_http = requests.Session()
_http.headers["User-Agent"] = "Mozilla/5.0"


def find_audio(directory: Path):
    for ext in ("*.flac", "*.mp3"):
        files = list(directory.rglob(ext))
        if files:
            return files[0]
    return None


def is_valid_url(url: str) -> bool:
    return bool(re.match(r"https?://(www\.)?deezer\.com/.+/track/\d+", url.strip()))


def mime(path: Path) -> str:
    return "audio/flac" if path.suffix == ".flac" else "audio/mpeg"


# ══════════════════════════════════════════════════════════════════════════════
# ROTAS
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/health")
def health():
    return jsonify({"ok": session.ok, "user": session.user.get("name", "—")})


@app.route("/search")
def search():
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify({"error": "Parâmetro q obrigatório"}), 400
    limit = min(int(request.args.get("limit", 24)), 50)
    try:
        r = _http.get(f"{DEEZER_API}/search", params={"q": q, "limit": limit}, timeout=8)
        r.raise_for_status()
        data = r.json().get("data", [])
        tracks = [{
            "id":       t["id"],
            "title":    t.get("title", ""),
            "artist":   t.get("artist", {}).get("name", ""),
            "album":    t.get("album", {}).get("title", ""),
            "cover":    t.get("album", {}).get("cover_medium", ""),
            "cover_xl": t.get("album", {}).get("cover_xl", ""),
            "duration": t.get("duration", 0),
            "preview":  t.get("preview", ""),
            "link":     t.get("link", ""),
            "album_id": t.get("album", {}).get("id", ""),
        } for t in data]
        return jsonify({"results": tracks})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/album/<int:album_id>")
def album(album_id):
    try:
        r  = _http.get(f"{DEEZER_API}/album/{album_id}", timeout=8)
        tr = _http.get(f"{DEEZER_API}/album/{album_id}/tracks?limit=50", timeout=8)
        r.raise_for_status(); tr.raise_for_status()
        meta   = r.json()
        tracks = tr.json().get("data", [])
        return jsonify({
            "id":     meta.get("id"),
            "title":  meta.get("title",""),
            "artist": meta.get("artist",{}).get("name",""),
            "cover":  meta.get("cover_xl") or meta.get("cover_medium",""),
            "year":   (meta.get("release_date","") or "")[:4],
            "tracks": [{
                "id":       t["id"],
                "title":    t.get("title",""),
                "duration": t.get("duration",0),
                "link":     t.get("link",""),
            } for t in tracks]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/stream")
def stream():
    url = request.args.get("url", "").strip()

    if not session.ok:
        return jsonify({"error": "Sessão não autenticada"}), 401

    if not is_valid_url(url):
        return jsonify({"error": "URL inválida"}), 400

    tmp_dir = Path(tempfile.mkdtemp(dir=DOWNLOAD_DIR, prefix="s_"))
    try:
        local = dict(session.settings)
        local["downloadLocation"] = str(tmp_dir)

        dl_obj = generateDownloadObject(session.dz, url, local["maxBitrate"])
        if dl_obj is None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return jsonify({"error": "Faixa não encontrada"}), 404

        Downloader(session.dz, dl_obj, local).start()
        audio = find_audio(tmp_dir)

        if audio is None:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return jsonify({"error": "Nenhum arquivo gerado"}), 500

    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        log.exception(e)
        return jsonify({"error": str(e)}), 500

    def generate():
        try:
            with open(audio, "rb") as f:
                while chunk := f.read(65536):
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            log.info(f"🗑️  Limpou: {tmp_dir.name}")

    return Response(
        stream_with_context(generate()),
        mimetype=mime(audio),
        headers={
            "Content-Disposition": f'inline; filename="{audio.name}"',
            "Accept-Ranges": "bytes",
            "Content-Length": str(audio.stat().st_size),
        }
    )


# ══════════════════════════════════════════════════════════════════════════════
# UI — lida pelo Flask (template inline)
# ══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    with open(Path(__file__).parent / "templates" / "index.html", encoding="utf-8") as f:
        return f.read()


def find_free_port(start=5000, end=5100):
    import socket
    for port in range(start, end):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Nenhuma porta livre encontrada entre 5000-5100.")

if __name__ == "__main__":
    port = find_free_port()
    log.info(f"🌐 Acesse: http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True)
