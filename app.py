from __future__ import annotations

import difflib
import json
import os
import re
import stat
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template, request, url_for
from ai_app import bp as ai_bp, init_app as init_ai_app
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                         login_user, logout_user)
import whisper
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import firebase_admin
    from firebase_admin import credentials, firestore
except ImportError:  # pragma: no cover - optional dependency
    firebase_admin = None
    credentials = None
    firestore = None

if TYPE_CHECKING:
    from firebase_admin import App
    from google.cloud.firestore import Client

load_dotenv()

# Write Firebase credentials from secret to file for Render deployment
def write_firebase_credentials():
    """Write Firebase credentials from environment variable to file for cloud deployment."""
    creds_json = os.environ.get("FIREBASE_CREDENTIALS_JSON")
    if not creds_json:
        return
    target_path = os.environ.get("FIREBASE_CREDENTIALS", "/tmp/firebase-admin-key.json")
    # Write securely
    with open(target_path, "w") as f:
        f.write(creds_json)
    # Make file readable only by owner where possible
    try:
        os.chmod(target_path, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        pass
    # Ensure Google SDKs pick it up
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = target_path
    os.environ["FIREBASE_CREDENTIALS"] = target_path

write_firebase_credentials()

app = Flask(__name__)
init_ai_app(app)
app.register_blueprint(ai_bp)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-me")
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message_category = "info"

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ROLE_OPTIONS = {"admin", "user"}
USERS: dict[str, dict[str, str]] = {}

IST = timezone(timedelta(hours=5, minutes=30))


def _store_user(email: str, password_hash: str, role: str = "user") -> None:
    record = {
        "email": email,
        "password_hash": password_hash,
        "role": role,
    }
    USERS[email.lower()] = record
    _persist_user_to_firestore(record)


def _persist_user_to_firestore(record: dict[str, str]) -> None:
    if firestore is None:
        return
    email = (record.get("email") or "").strip()
    password_hash = record.get("password_hash")
    if not email or not password_hash:
        return
    client = _get_firestore_client()
    if client is None:
        return
    payload = {
        "email": email,
        "role": record.get("role", "user"),
        "password_hash": password_hash,
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    try:
        client.collection("users").document(email.lower()).set(payload, merge=True)
    except Exception as exc:  # pragma: no cover - remote call safety
        app.logger.warning("Failed to persist user to Firestore: %s", exc)



def _fetch_user_from_firestore(email: str) -> Optional[dict[str, str]]:
    if not email:
        return None
    client = _get_firestore_client()
    if client is None:
        return None
    try:
        document = client.collection("users").document(email.lower()).get()
    except Exception as exc:  # pragma: no cover - remote call safety
        app.logger.warning("Failed to fetch user from Firestore: %s", exc)
        return None
    if not document.exists:
        return None
    data = document.to_dict() or {}
    stored_email = (data.get("email") or document.id or "").strip()
    password_hash = data.get("password_hash")
    if not stored_email or not password_hash:
        return None
    role = data.get("role", "user")
    record = {
        "email": stored_email,
        "password_hash": password_hash,
        "role": role,
    }
    USERS[stored_email.lower()] = record
    return record



def _delete_user_from_firestore(email: str) -> None:
    if firestore is None:
        return
    email = (email or "").strip()
    if not email:
        return
    client = _get_firestore_client()
    if client is None:
        return
    try:
        client.collection("users").document(email.lower()).delete()
    except Exception as exc:  # pragma: no cover - remote call safety
        app.logger.warning("Failed to delete user from Firestore: %s", exc)



def _refresh_users_from_firestore() -> None:
    client = _get_firestore_client()
    if client is None:
        return
    try:
        documents = client.collection("users").stream()
    except Exception as exc:  # pragma: no cover - remote call safety
        app.logger.warning("Failed to refresh users from Firestore: %s", exc)
        return
    for document in documents:
        data = document.to_dict() or {}
        email = (data.get("email") or document.id or "").strip()
        password_hash = data.get("password_hash")
        if not email or not password_hash:
            continue
        role = data.get("role", "user")
        USERS[email.lower()] = {
            "email": email,
            "password_hash": password_hash,
            "role": role,
        }


SENTENCES = [
    {"id": 1, "text": "She sells seashells by the seashore."},
    {"id": 2, "text": "The quick brown fox jumps over the lazy dog."},
    {"id": 3, "text": "Practice makes perfect when learning pronunciation."},
    {"id": 4, "text": "Speaking clearly requires focus and confidence."},
]

PARAGRAPHS = [
    {"id": 101, "text": "Yesterday, I went to the park with my family. The weather was beautiful, so we enjoyed a picnic lunch under a large tree. After eating, my children played on the swings and on the slide. I read a book and relaxed on the grass. It was a peaceful and enjoyable afternoon before we headed home in the late afternoon"},
    {"id": 102, "text": "Don’t be fooled by its name – small talk is anything but small. Various studies show that nearly a third of our speech is small talk. Practicing this part of daily English conversation is vital. To ace your next interaction with a native speaker, we recommend learning open-ended questions and rehearsing how to answer them, and expanding your vocabulary for fluent conversation either alone or with a speaking partner. "},
    {"id": 103, "text": "Try to spend 15 minutes every day reading English texts. Find a comfortable spot where you can focus on a book, an article, etc. without the risk of being interrupted. Don’t know what to read? Try news websites like the BBC for free daily articles featuring easy-to-read paragraphs to improve your English."},
]

WHISPER_MODEL_NAME = os.environ.get("WHISPER_MODEL", "base")
MODEL_CACHE_DIR = Path(os.environ.get("WHISPER_CACHE_DIR", Path.cwd() / "models"))
_MODEL: Optional[whisper.Whisper] = None


PROGRESS_CACHE: dict[str, list[dict[str, Any]]] = {}
FIREBASE_APP: Any | None = None
FIRESTORE_CLIENT: Any | None = None



def init_firebase() -> None:
    """Initialise a Firebase app if credentials are available."""
    global FIREBASE_APP, FIRESTORE_CLIENT
    if FIRESTORE_CLIENT is not None:
        return
    if firebase_admin is None or credentials is None or firestore is None:
        return

    try:
        FIREBASE_APP = firebase_admin.get_app()  # type: ignore[assignment]
    except ValueError:
        cred_path = os.environ.get("FIREBASE_CREDENTIALS")
        cred = None
        if cred_path:
            cred_file = Path(cred_path)
            if cred_file.exists():
                cred = credentials.Certificate(str(cred_file))
            else:
                try:
                    cred = credentials.Certificate(json.loads(cred_path))
                except json.JSONDecodeError:
                    app.logger.warning("FIREBASE_CREDENTIALS must be a path or raw JSON credentials.")
                    return
        try:
            FIREBASE_APP = firebase_admin.initialize_app(cred) if cred else firebase_admin.initialize_app()  # type: ignore[assignment]
        except Exception as exc:  # pragma: no cover - startup safety
            app.logger.warning("Firebase initialisation failed: %s", exc)
            FIREBASE_APP = None
            return

    try:
        FIRESTORE_CLIENT = firestore.client(app=FIREBASE_APP)  # type: ignore[arg-type]
    except Exception as exc:  # pragma: no cover - startup safety
        app.logger.warning("Firestore client initialisation failed: %s", exc)
        FIRESTORE_CLIENT = None
    else:
        _refresh_users_from_firestore()



def _get_firestore_client() -> Optional[Any]:
    if FIRESTORE_CLIENT is not None:
        return FIRESTORE_CLIENT
    init_firebase()
    return FIRESTORE_CLIENT



DEFAULT_ADMIN_EMAIL = (
    os.environ.get("APP_ADMIN_EMAIL")
    or os.environ.get("APP_DEFAULT_EMAIL")
    or os.environ.get("APP_DEFAULT_USER")
    or "coach@example.com"
)
DEFAULT_ADMIN_PASSWORD = (
    os.environ.get("APP_ADMIN_PASSWORD")
    or os.environ.get("APP_DEFAULT_PASSWORD")
    or "practice123"
)
if DEFAULT_ADMIN_EMAIL and DEFAULT_ADMIN_PASSWORD:
    _store_user(DEFAULT_ADMIN_EMAIL, generate_password_hash(DEFAULT_ADMIN_PASSWORD), role="admin")

def _coerce_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(IST)
    else:
        return datetime.now(IST)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)



def save_progress_entry(user_id: str, content: dict[str, Any], transcript: str, score: float, content_type: str = "sentence") -> None:
    if not user_id:
        return
    created_at = datetime.now(IST)
    entry = {
        "id": uuid.uuid4().hex,
        "sentence_id": content.get("id"),
        "content_type": content_type,
        "target": content.get("text"),
        "transcript": transcript,
        "score": float(score),
        "created_at": created_at,
    }

    client = _get_firestore_client()
    if client is not None:
        try:
            client.collection("users").document(user_id).collection("sessions").document(entry["id"]).set(entry)
        except Exception as exc:  # pragma: no cover - remote call safety
            app.logger.warning("Failed to persist progress to Firestore: %s", exc)

    history = PROGRESS_CACHE.setdefault(user_id, [])
    history.append(entry)
    if len(history) > 100:
        del history[:-100]



def fetch_progress_entries(user_id: str, limit: int = 50) -> list[dict[str, Any]]:
    client = _get_firestore_client()
    entries: list[dict[str, Any]] = []

    if client is not None:
        try:
            query = (
                client.collection("users")
                .document(user_id)
                .collection("sessions")
                .order_by("created_at", direction=firestore.Query.DESCENDING)
                .limit(limit)
            )
            for document in query.stream():
                payload = document.to_dict() or {}
                payload.setdefault("id", document.id)
                payload["created_at"] = _coerce_timestamp(payload.get("created_at"))
                entries.append(payload)
        except Exception as exc:  # pragma: no cover - remote call safety
            app.logger.warning("Failed to load progress from Firestore: %s", exc)

    if not entries:
        fallback = PROGRESS_CACHE.get(user_id, [])
        entries = sorted(fallback, key=lambda item: item.get("created_at", datetime.min.replace(tzinfo=IST)), reverse=True)[:limit]

    return entries



def summarise_progress(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries:
        return {
            "total_sessions": 0,
            "average_score": None,
            "best_score": None,
            "last_score": None,
            "last_practiced": None,
        }

    scores = [float(entry.get("score", 0.0)) for entry in entries if entry.get("score") is not None]
    best_score = max(scores, default=None)
    average_score = sum(scores) / len(scores) if scores else None
    last_entry = entries[0]

    return {
        "total_sessions": len(entries),
        "average_score": average_score,
        "best_score": best_score,
        "last_score": float(last_entry.get("score", 0.0)) if scores else None,
        "last_practiced": last_entry.get("created_at"),
    }


class User(UserMixin):
    def __init__(self, email: str, role: str = "user") -> None:
        self.id = email
        self.role = role

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @staticmethod
    def get(email: str) -> Optional["User"]:
        record = USERS.get(email.lower())
        if record:
            return User(record["email"], record.get("role", "user"))
        fetched = _fetch_user_from_firestore(email)
        if fetched:
            return User(fetched["email"], fetched.get("role", "user"))
        return None


@login_manager.user_loader
def load_user(user_id: str) -> Optional[User]:
    return User.get(user_id)


@login_manager.unauthorized_handler
def unauthorized():
    if request.path == "/transcribe":
        return jsonify({"error": "Unauthorized"}), 401
    return redirect(url_for("login", next=request.url))


@app.context_processor
def inject_role_options():
    return {"ROLE_OPTIONS": sorted(ROLE_OPTIONS)}


def _is_safe_redirect(target: Optional[str]) -> bool:
    if not target:
        return False
    host_url = urlparse(request.host_url)
    redirect_url = urlparse(urljoin(request.host_url, target))
    return redirect_url.scheme in ("http", "https") and host_url.netloc == redirect_url.netloc


def _validate_email(email: str) -> Optional[str]:
    if not email:
        return "Email is required."
    if not EMAIL_PATTERN.match(email):
        return "Enter a valid email address."
    return None


def _validate_password(password: str) -> list[str]:
    errors: list[str] = []
    if not password:
        errors.append("Password is required.")
        return errors
    if len(password) < 8:
        errors.append("Password must be at least 8 characters long.")
    if not re.search(r"[A-Za-z]", password):
        errors.append("Password must include at least one letter.")
    if not re.search(r"[0-9]", password):
        errors.append("Password must include at least one number.")
    return errors


def _count_admins() -> int:
    return sum(1 for record in USERS.values() if record.get("role") == "admin")


def _require_admin() -> None:
    if not current_user.is_authenticated or not getattr(current_user, "is_admin", False):
        abort(403)


def load_model() -> whisper.Whisper:
    global _MODEL
    if _MODEL is None:
        MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _MODEL = whisper.load_model(WHISPER_MODEL_NAME, download_root=str(MODEL_CACHE_DIR))
    return _MODEL


def normalize_text(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", value.lower())
    return " ".join(cleaned.split())


def calculate_similarity(reference: str, attempt: str) -> float:
    reference_normalized = normalize_text(reference)
    attempt_normalized = normalize_text(attempt)
    if not reference_normalized or not attempt_normalized:
        return 0.0

    ratio = difflib.SequenceMatcher(None, reference_normalized, attempt_normalized).ratio()
    return round(ratio * 100, 2)


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/coach")
@login_required
def coach():
    return render_template("index.html", sentences=SENTENCES, paragraphs=PARAGRAPHS)


@app.get("/dashboard")
@login_required
def dashboard():
    entries = fetch_progress_entries(current_user.id)
    summary = summarise_progress(entries)
    return render_template("dashboard.html", progress=summary, entries=entries)


@app.post("/transcribe")
@login_required
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "No audio file provided."}), 400

    content_type_raw = request.form.get("contentType", "sentence")
    content_type = (content_type_raw or "sentence").strip().lower()
    if content_type not in {"sentence", "paragraph"}:
        return jsonify({"error": "Invalid content type."}), 400

    content_id = request.form.get("contentId") or request.form.get("sentenceId")
    if not content_id:
        return jsonify({"error": "Content identifier missing."}), 400

    try:
        content_id_int = int(content_id)
    except ValueError:
        return jsonify({"error": "Content identifier must be an integer."}), 400

    items = SENTENCES if content_type == "sentence" else PARAGRAPHS
    content = next((item for item in items if item["id"] == content_id_int), None)
    if content is None:
        label = "paragraph" if content_type == "paragraph" else "sentence"
        return jsonify({"error": f"{label.capitalize()} not found."}), 404

    audio_file = request.files["audio"]
    if audio_file.filename == "":
        return jsonify({"error": "Empty audio file."}), 400

    temp_path: str | None = None
    try:
        suffix = Path(audio_file.filename).suffix or ".webm"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as temp_audio:
            audio_file.save(temp_audio.name)
            temp_path = temp_audio.name

        model = load_model()
        transcription = model.transcribe(temp_path, fp16=False, language="en")
        transcript_text = transcription.get("text", "").strip()
        similarity_score = calculate_similarity(content["text"], transcript_text)

        response_payload = {
            "transcript": transcript_text,
            "target": content["text"],
            "score": similarity_score,
            "contentType": content_type,
        }

        if current_user.is_authenticated:
            try:
                save_progress_entry(
                    current_user.id,
                    content,
                    transcript_text,
                    similarity_score,
                    content_type,
                )
            except Exception as exc:  # pragma: no cover - defensive logging
                app.logger.warning("Failed to record practice session: %s", exc)

        return jsonify(response_payload)
    except Exception as exc:  # pragma: no cover - safety net for runtime issues
        return jsonify({"error": "Transcription failed.", "details": str(exc)}), 500
    finally:
        if temp_path:
            temp_file = Path(temp_path)
            if temp_file.exists():
                try:
                    temp_file.unlink()
                except OSError:
                    pass


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.args.get("next")
    if not _is_safe_redirect(next_url):
        next_url = url_for("dashboard")

    if current_user.is_authenticated:
        return redirect(next_url)

    if request.method == "POST":
        email_raw = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        form_next = request.form.get("next")
        if _is_safe_redirect(form_next):
            next_url = form_next

        record = USERS.get(email_raw.lower()) or _fetch_user_from_firestore(email_raw)
        if record and check_password_hash(record["password_hash"], password):
            login_user(User(record["email"], record.get("role", "user")))
            flash("Welcome back!", "success")
            return redirect(next_url)

        flash("Invalid email or password.", "error")

    return render_template("login.html", next_url=next_url)


@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    next_url = request.args.get("next")
    if not _is_safe_redirect(next_url):
        next_url = url_for("dashboard")

    if request.method == "POST":
        email_raw = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        errors: list[str] = []
        email_error = _validate_email(email_raw)
        if email_error:
            errors.append(email_error)
        if email_raw:
            existing_user = USERS.get(email_raw.lower()) or _fetch_user_from_firestore(email_raw)
            if existing_user:
                errors.append("An account with this email already exists.")

        errors.extend(_validate_password(password))
        if not confirm:
            errors.append("Please confirm your password.")
        elif password != confirm:
            errors.append("Passwords do not match.")

        if errors:
            for err in errors:
                flash(err, "error")
            return render_template(
                "register.html",
                next_url=next_url,
                form_email=email_raw,
            )

        _store_user(email_raw, generate_password_hash(password), role="user")
        login_user(User(email_raw, "user"))
        flash("Registration successful!", "success")
        redirect_target = request.form.get("next") or next_url
        if not _is_safe_redirect(redirect_target):
            redirect_target = url_for("dashboard")
        return redirect(redirect_target)

    return render_template("register.html", next_url=next_url)


@app.route("/admin/users", methods=["GET", "POST"])
@login_required
def admin_users():
    _require_admin()

    if request.method == "POST":
        action = (request.form.get("action") or "").lower()
        if action == "create":
            _admin_create_user()
        elif action == "update":
            _admin_update_user()
        elif action == "delete":
            _admin_delete_user()
        else:
            flash("Unknown action.", "error")
        return redirect(url_for("admin_users"))

    _refresh_users_from_firestore()
    users = sorted(USERS.values(), key=lambda item: item["email"].lower())

    selected_email = (request.args.get("user") or "").strip()
    selected_user = USERS.get(selected_email.lower()) if selected_email else None
    if selected_email and selected_user is None:
        selected_user = _fetch_user_from_firestore(selected_email)

    audit_entries: list[dict[str, Any]] = []
    audit_summary: Optional[dict[str, Any]] = None
    selected_missing = False

    if selected_email:
        if selected_user:
            audit_entries = fetch_progress_entries(selected_user["email"], limit=25)
            audit_summary = summarise_progress(audit_entries)
        else:
            selected_missing = True

    return render_template(
        "admin_users.html",
        users=users,
        selected_user=selected_user,
        selected_email=selected_email,
        audit_entries=audit_entries,
        audit_summary=audit_summary,
        selected_missing=selected_missing,
    )


def _admin_create_user() -> None:
    email_raw = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")
    role = (request.form.get("role") or "user").lower()

    errors: list[str] = []
    email_error = _validate_email(email_raw)
    if email_error:
        errors.append(email_error)
    if email_raw:
        existing_user = USERS.get(email_raw.lower()) or _fetch_user_from_firestore(email_raw)
        if existing_user:
            errors.append("An account with this email already exists.")

    errors.extend(_validate_password(password))
    if not confirm:
        errors.append("Please confirm the password.")
    elif password != confirm:
        errors.append("Passwords do not match.")

    if role not in ROLE_OPTIONS:
        errors.append("Invalid role selection.")

    if errors:
        for err in errors:
            flash(err, "error")
        return

    _store_user(email_raw, generate_password_hash(password), role=role)
    flash("User created successfully.", "success")


def _admin_update_user() -> None:
    original_email = request.form.get("original_email", "").strip()
    lookup_key = original_email.lower() if original_email else ""
    record = USERS.get(lookup_key)
    if record is None and original_email:
        record = _fetch_user_from_firestore(original_email)
    if not record:
        flash("User not found.", "error")
        return

    new_email = request.form.get("email", "").strip()
    role = (request.form.get("role") or record.get("role", "user")).lower()
    password = request.form.get("password", "")
    confirm = request.form.get("confirm_password", "")

    errors: list[str] = []
    email_error = _validate_email(new_email)
    if email_error:
        errors.append(email_error)
    elif new_email.lower() != original_email.lower():
        existing_user = USERS.get(new_email.lower()) or _fetch_user_from_firestore(new_email)
        if existing_user:
            errors.append("Another user already uses that email.")

    if role not in ROLE_OPTIONS:
        errors.append("Invalid role selection.")

    if password or confirm:
        errors.extend(_validate_password(password))
        if not confirm:
            errors.append("Please confirm the password.")
        elif password != confirm:
            errors.append("Passwords do not match.")

    if errors:
        for err in errors:
            flash(err, "error")
        return

    was_admin = record.get("role") == "admin"
    will_be_admin = role == "admin"
    if was_admin and not will_be_admin and _count_admins() <= 1:
        flash("At least one admin must remain.", "error")
        return

    new_record = dict(record)
    new_record["email"] = new_email
    new_record["role"] = role
    if password:
        new_record["password_hash"] = generate_password_hash(password)

    original_key = lookup_key
    new_key = new_email.lower()
    if original_key in USERS:
        del USERS[original_key]
    USERS[new_key] = new_record
    _persist_user_to_firestore(new_record)
    if new_key != original_key and original_email:
        _delete_user_from_firestore(original_email)

    if current_user.is_authenticated and current_user.id.lower() == original_email.lower():
        login_user(User(new_record["email"], new_record.get("role", "user")))
        flash("Your profile has been updated.", "info")

    flash("User updated successfully.", "success")


def _admin_delete_user() -> None:
    email_raw = request.form.get("email", "").strip()
    lookup_key = email_raw.lower() if email_raw else ""
    record = USERS.get(lookup_key)
    if record is None and email_raw:
        record = _fetch_user_from_firestore(email_raw)
    if not record:
        flash("User not found.", "error")
        return

    if current_user.is_authenticated and current_user.id.lower() == lookup_key:
        flash("You cannot delete your own account while signed in.", "error")
        return

    if record.get("role") == "admin" and _count_admins() <= 1:
        flash("At least one admin must remain.", "error")
        return

    if lookup_key in USERS:
        del USERS[lookup_key]
    if email_raw:
        _delete_user_from_firestore(email_raw)
    flash("User deleted.", "success")


@app.post("/logout")
@login_required
def logout():
    logout_user()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)

