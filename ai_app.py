from __future__ import annotations

import os
import uuid
import shutil
from pathlib import Path
from typing import Iterable

from flask import Blueprint, current_app, jsonify, render_template, request, session, url_for
from flask_login import login_required
import ffmpeg  # type: ignore
import requests
import speech_recognition as sr
from requests.exceptions import HTTPError

from gtts import gTTS  # type: ignore

bp = Blueprint("ai", __name__, url_prefix="/aicompanion")

_GEMINI_MODEL = "gemini-2.0-flash"
_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_GEMINI_MODEL}:generateContent"
)


def init_app(app) -> None:
    """Configure the hosting Flask application for the AI Companion module.

    This sets up upload locations, validates external tool availability, and
    ensures the Gemini API key is present so requests can be made when the
    blueprint routes execute.
    """

    audio_folder = Path(app.root_path, "static", "audio")
    video_folder = Path(app.root_path, "static", "video")
    tmp_folder = Path(app.root_path, "tmp")

    for folder in (audio_folder, video_folder, tmp_folder):
        folder.mkdir(parents=True, exist_ok=True)

    app.config.setdefault("AI_UPLOAD_FOLDER", str(audio_folder))
    app.config.setdefault("AI_VIDEO_FOLDER", str(video_folder))
    app.config.setdefault("AI_TMP_FOLDER", str(tmp_folder))

    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("ffmpeg not found on PATH. Please install ffmpeg.")
    os.environ.setdefault("FFMPEG_BINARY", ffmpeg_bin)
    app.config.setdefault("AI_FFMPEG_BIN", ffmpeg_bin)

    api_key = app.config.get("GOOGLE_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Missing GOOGLE_API_KEY environment variable.")
    app.config["GOOGLE_API_KEY"] = api_key

    gemini_url = f"{_GEMINI_ENDPOINT}?key={api_key}"
    app.config.setdefault("GEMINI_API_URL", gemini_url)
    app.config.setdefault(
        "AI_SYSTEM_PROMPT", "You are restricted to respond in 20 words."
    )


@bp.get("/")
@login_required
def ai_home():
    """Render the conversational AI Companion interface."""
    return render_template("ai.html")


@bp.post("/process_audio")
@login_required
def process_audio():
    """Handle audio submissions from the AI Companion interface."""

    history = list(session.get("history", []))
    system_msg = {
        "author": "system",
        "text": current_app.config.get(
            "AI_SYSTEM_PROMPT", "You are restricted to respond in 20 words."
        ),
    }

    try:
        audio_file = request.files.get("audio_data")
        if not audio_file:
            return jsonify({"error": "No audio_data provided"}), 400

        tmp_folder = Path(current_app.config["AI_TMP_FOLDER"])
        webm_path = tmp_folder / f"{uuid.uuid4().hex}.webm"
        audio_file.save(webm_path)

        wav_path = webm_path.with_suffix(".wav")
        ffmpeg.input(str(webm_path)).output(
            str(wav_path), ac=1, ar=16000
        ).run(quiet=True, overwrite_output=True)

        recognizer = sr.Recognizer()
        with sr.AudioFile(str(wav_path)) as src:
            audio_data = recognizer.record(src)

        try:
            transcript = recognizer.recognize_google(audio_data)
        except sr.UnknownValueError:
            transcript = ""
        except sr.RequestError as exc:  # pragma: no cover - upstream failure
            raise RuntimeError(f"Speech recognition error: {exc}")

        history.append({"author": "user", "text": transcript})
        session["history"] = history

        messages = _truncate_history([system_msg] + history)
        response_text = call_gemini_api(messages).replace("*", "")

        history.append({"author": "assistant", "text": response_text})
        session["history"] = history

        webm_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)

        audio_folder = Path(current_app.config["AI_UPLOAD_FOLDER"])
        mp3_path = audio_folder / f"resp_{uuid.uuid4().hex}.mp3"
        synthesize_conversational(response_text, mp3_path)

        audio_url = url_for("static", filename=f"audio/{mp3_path.name}")
        loop_video_url = url_for("static", filename="video/demo.mp4")

        return jsonify(
            {
                "transcript": transcript,
                "response_text": response_text,
                "audio_url": audio_url,
                "audio_filename": mp3_path.name,
                "loop_video_url": loop_video_url,
            }
        )

    except Exception as exc:  # pragma: no cover - surfaces to JSON error payload
        current_app.logger.exception("process_audio error")
        return jsonify({"error": str(exc)}), 500



@bp.post("/cleanup_audio")
@login_required
def cleanup_audio():
    """Remove a generated assistant audio file once playback finishes."""

    try:
        payload = request.get_json(silent=True) or {}
        original = (payload.get("filename") or "").strip()
        if not original:
            return jsonify({"error": "Filename required"}), 400

        filename = os.path.basename(original)
        if filename != original or not filename.endswith('.mp3'):
            return jsonify({"error": "Invalid filename"}), 400

        audio_folder = Path(current_app.config["AI_UPLOAD_FOLDER"])
        file_path = audio_folder / filename
        if file_path.exists():
            file_path.unlink()
        return jsonify({"status": "ok"})
    except Exception as exc:  # pragma: no cover - best effort cleanup
        current_app.logger.exception("cleanup_audio error")
        return jsonify({"error": str(exc)}), 500


def call_gemini_api(message_list: Iterable[dict], retries: int = 3, backoff: float = 1.0) -> str:
    """Send a prompt to the Gemini API with lightweight retry handling."""

    import time

    lines = []
    for msg in message_list:
        author = msg.get("author", "assistant") or "assistant"
        speaker = (
            "System" if author == "system" else "User" if author == "user" else "Assistant"
        )
        lines.append(f"{speaker}: {msg.get('text', '')}")
    lines.append("Assistant:")

    payload = {"contents": [{"parts": [{"text": "\n".join(lines)}]}]}
    headers = {"Content-Type": "application/json"}
    api_url = current_app.config["GEMINI_API_URL"]

    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(api_url, headers=headers, json=payload, timeout=15)
            response.raise_for_status()
            data = response.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise RuntimeError("No response from Gemini API")
            content = candidates[0].get("content")
            if isinstance(content, dict) and "text" in content:
                return content["text"]
            if isinstance(content, dict):
                return "".join(part.get("text", "") for part in content.get("parts", []))
            return str(content)
        except HTTPError as exc:
            last_exc = exc
            status = getattr(exc.response, "status_code", None)
            if status == 503 and attempt < retries:
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            raise
        except Exception as exc:  # pragma: no cover - guards transient issues
            last_exc = exc
            if attempt < retries:
                time.sleep(backoff * (2 ** (attempt - 1)))
                continue
            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("Gemini API call failed")



def synthesize_conversational(text: str, out_path: Path | str) -> None:
    '''Create a spoken response for the assistant reply.'''

    path = Path(out_path)
    words = text.split()
    if len(words) > 150:
        text = " ".join(words[:150]) + "..."

    path.parent.mkdir(parents=True, exist_ok=True)
    gTTS(text=text, lang="en").save(str(path))

def _truncate_history(messages: list[dict]) -> list[dict]:
    total_words = 0
    trimmed: list[dict] = []
    for entry in reversed(messages):
        words = len((entry.get("text") or "").split())
        if total_words + words > 3000 and trimmed:
            break
        trimmed.append(entry)
        total_words += words
    return list(reversed(trimmed))


__all__ = ["bp", "init_app", "call_gemini_api", "cleanup_audio", "synthesize_conversational"]


