"""Karaoke Maker - local web app. Run `python app.py`, then open http://127.0.0.1:5123"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file

from karaoke_core import make_karaoke

HERE = Path(__file__).resolve().parent
VERSION = "1.5 (better non-English sync)"
PORT = int(os.environ.get("KARAOKE_PORT", "5123"))


def default_out_dir() -> Path:
    movies = Path.home() / "Movies"
    return (movies / "Karaoke") if movies.is_dir() else (HERE / "output")


OUT_DIR = Path(os.environ.get("KARAOKE_OUT", default_out_dir()))
WORK_ROOT = Path(tempfile.gettempdir()) / "karaoke-maker"

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 300 * 1024 * 1024
jobs: dict[str, dict] = {}


def run_job(job_id: str, mp3: Path, opts: dict) -> None:
    job = jobs[job_id]

    def log(msg: str) -> None:
        print(msg, flush=True)
        job["log"].append(msg)

    try:
        out = make_karaoke(mp3, OUT_DIR, WORK_ROOT / job_id, log=log, **opts)
        job["output"] = str(out)
        job["status"] = "done"
    except Exception as e:  # report anything to the UI
        log(f"ERROR: {e}")
        job["status"] = "error"
    finally:
        shutil.rmtree(WORK_ROOT / job_id, ignore_errors=True)


@app.get("/")
def index():
    return send_file(HERE / "index.html")


@app.post("/api/jobs")
def create_job():
    f = request.files.get("audio")
    if not f or not f.filename:
        return jsonify(error="Please choose an audio file."), 400
    job_id = uuid.uuid4().hex[:12]
    work = WORK_ROOT / job_id
    work.mkdir(parents=True, exist_ok=True)
    name = Path(f.filename).name or "song.mp3"
    mp3 = work / name
    f.save(mp3)

    lrc = request.files.get("lrc")
    lrc_text = lrc.read().decode("utf-8", "replace") if lrc and lrc.filename else None
    try:
        strength = float(request.form.get("strength", 85)) / 100
    except ValueError:
        strength = 0.85
    opts = dict(
        artist=request.form.get("artist", "").strip(),
        title=request.form.get("title", "").strip(),
        strength=strength,
        lyrics_source=request.form.get("source", "auto"),
        lrc_text=lrc_text,
        whisper_model=request.form.get("model", "small"),
        language=request.form.get("language", "").strip() or None,
        precise=request.form.get("precise") == "1",
        offset=_float(request.form.get("offset"), 0.0),
    )
    jobs[job_id] = {"status": "running", "log": [], "output": None}
    threading.Thread(target=run_job, args=(job_id, mp3, opts), daemon=True).start()
    return jsonify(id=job_id)


def _float(v, default):
    try:
        return max(-5.0, min(5.0, float(v)))
    except (TypeError, ValueError):
        return default


@app.get("/api/jobs/<job_id>")
def job_status(job_id):
    job = jobs.get(job_id) or abort(404)
    return jsonify(status=job["status"], log=job["log"],
                   filename=Path(job["output"]).name if job["output"] else None)


@app.get("/api/jobs/<job_id>/video")
def job_video(job_id):
    job = jobs.get(job_id) or abort(404)
    if not job["output"]:
        abort(404)
    return send_file(job["output"], mimetype="video/mp4", conditional=True,
                     as_attachment=request.args.get("download") == "1")


@app.post("/api/jobs/<job_id>/reveal")
def reveal(job_id):
    job = jobs.get(job_id) or abort(404)
    if job["output"] and sys.platform == "darwin":
        subprocess.run(["open", "-R", job["output"]])
    return jsonify(ok=True, path=job["output"])


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print(f"Karaoke Maker {VERSION} running at {url}  (videos are saved to {OUT_DIR})")
    print("Close this window or press Ctrl+C to quit.")
    if "--no-browser" not in sys.argv:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True)
