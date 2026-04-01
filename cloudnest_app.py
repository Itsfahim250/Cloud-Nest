import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import telebot
from telebot import types
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

# =============================================================================
# CONFIG
# =============================================================================

BOT_TOKEN = (os.environ.get("BOT_TOKEN") or "").strip()
ADMIN_CHAT_IDS_RAW = (os.environ.get("ADMIN_CHAT_ID") or "").strip()
PUBLIC_BASE_URL = (os.environ.get("PUBLIC_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").strip().rstrip("/")
PORT = int(os.environ.get("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")

ADMIN_CHAT_IDS = {x.strip() for x in ADMIN_CHAT_IDS_RAW.split(",") if x.strip()}
if not ADMIN_CHAT_IDS:
    # Keep empty set if not configured. Admin-only features will simply be hidden.
    ADMIN_CHAT_IDS = set()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
USER_DATA_FILE = os.path.join(DATA_DIR, "users.json")
PREMIUM_CODES_FILE = os.path.join(DATA_DIR, "premium_codes.json")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode=None)
app = Flask(__name__)
CORS(app)

# =============================================================================
# STORAGE / LOCKS
# =============================================================================

STORE_LOCK = threading.RLock()
PENDING_ACTIONS = {}

FREE_LIMITS = {
    "db_ops": 50,
    "auth_ops": 30,
    "upload_ops": 15,
    "password_edits": 10,
}

# =============================================================================
# HELPERS
# =============================================================================


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json_file(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return default


def save_json_file(path: str, data) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp_path, path)


def load_users() -> dict:
    with STORE_LOCK:
        users = load_json_file(USER_DATA_FILE, {})
        if not isinstance(users, dict):
            users = {}
        return users


def save_users(users: dict) -> None:
    with STORE_LOCK:
        save_json_file(USER_DATA_FILE, users)


def load_premium_codes() -> dict:
    with STORE_LOCK:
        codes = load_json_file(PREMIUM_CODES_FILE, {})
        if not isinstance(codes, dict):
            codes = {}
        return codes


def save_premium_codes(codes: dict) -> None:
    with STORE_LOCK:
        save_json_file(PREMIUM_CODES_FILE, codes)


def is_admin(chat_id: str) -> bool:
    return str(chat_id) in ADMIN_CHAT_IDS


def get_public_base_url() -> str:
    if PUBLIC_BASE_URL:
        return PUBLIC_BASE_URL
    # fallback for local testing only
    return f"http://127.0.0.1:{PORT}"


def ensure_user(chat_id: str) -> dict:
    chat_id = str(chat_id)
    with STORE_LOCK:
        users = load_users()
        if chat_id not in users:
            users[chat_id] = {
                "telegram_id": chat_id,
                "api_key": "cn_" + uuid.uuid4().hex,
                "premium": False,
                "premium_activated_at": "",
                "created_at": now_iso(),
                "usage": {
                    "db_ops": 0,
                    "auth_ops": 0,
                    "upload_ops": 0,
                    "password_edits": 0,
                },
            }
            save_users(users)
        else:
            # Backward compatible migration
            changed = False
            u = users[chat_id]
            if "telegram_id" not in u:
                u["telegram_id"] = chat_id
                changed = True
            if "api_key" not in u:
                u["api_key"] = "cn_" + uuid.uuid4().hex
                changed = True
            if "premium" not in u:
                u["premium"] = False
                changed = True
            if "premium_activated_at" not in u:
                u["premium_activated_at"] = ""
                changed = True
            if "created_at" not in u:
                u["created_at"] = now_iso()
                changed = True
            if "usage" not in u or not isinstance(u["usage"], dict):
                u["usage"] = {}
                changed = True
            for key in FREE_LIMITS:
                if key not in u["usage"]:
                    u["usage"][key] = 0
                    changed = True
            if changed:
                save_users(users)
        return users[chat_id]


def get_user_by_api_key(api_key: str):
    if not api_key:
        return None, None
    users = load_users()
    for user_id, info in users.items():
        if info.get("api_key") == api_key:
            return str(user_id), info
    return None, None


def feature_limit_status(user_info: dict, feature: str) -> tuple[int, int, float | str]:
    used = int((user_info.get("usage") or {}).get(feature, 0))
    limit = int(FREE_LIMITS.get(feature, 0))
    if user_info.get("premium"):
        return used, limit, "Unlimited"
    percent = (used / limit * 100.0) if limit else 0.0
    return used, limit, round(percent, 1)


def consume_feature(chat_id: str, feature: str) -> tuple[bool, dict]:
    """
    Returns (allowed, user_info_after_update)
    """
    chat_id = str(chat_id)
    with STORE_LOCK:
        users = load_users()
        user_info = users.get(chat_id)
        if not user_info:
            return False, {}
        if user_info.get("premium"):
            user_info.setdefault("usage", {})
            user_info["usage"][feature] = int(user_info["usage"].get(feature, 0)) + 1
            users[chat_id] = user_info
            save_users(users)
            return True, user_info

        user_info.setdefault("usage", {})
        used = int(user_info["usage"].get(feature, 0))
        limit = int(FREE_LIMITS.get(feature, 0))
        if limit and used >= limit:
            users[chat_id] = user_info
            save_users(users)
            return False, user_info

        user_info["usage"][feature] = used + 1
        users[chat_id] = user_info
        save_users(users)
        return True, user_info


def percent_text(used: int, limit: int) -> str:
    if limit <= 0:
        return "0%"
    return f"{min(100.0, (used / limit) * 100.0):.1f}%"


def usage_summary(user_info: dict) -> str:
    lines = []
    for feature, limit in FREE_LIMITS.items():
        used = int((user_info.get("usage") or {}).get(feature, 0))
        if user_info.get("premium"):
            lines.append(f"- {feature}: {used} used | Premium = Unlimited")
        else:
            lines.append(f"- {feature}: {used}/{limit} used ({percent_text(used, limit)})")
    return "\n".join(lines)


def main_keyboard(chat_id: str):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("Database"),
        types.KeyboardButton("Authentication"),
        types.KeyboardButton("Premium"),
        types.KeyboardButton("Project Settings"),
    ]
    if is_admin(chat_id):
        buttons.append(types.KeyboardButton("Create premium"))
    markup.add(*buttons)
    return markup


def premium_inline_keyboard(is_admin_user: bool):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Redeem Premium Code", callback_data="premium_redeem"))
    if is_admin_user:
        markup.add(types.InlineKeyboardButton("Create Premium Code", callback_data="premium_create"))
    return markup


def auth_inline_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Show Auth Users", callback_data="show_auth"))
    markup.add(types.InlineKeyboardButton("Edit Password", callback_data="edit_password"))
    return markup


def project_inline_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("💻 Database API Code", callback_data="code_db"))
    markup.add(types.InlineKeyboardButton("💻 Auth API Code", callback_data="code_auth"))
    markup.add(types.InlineKeyboardButton("🔐 Password Edit API Code", callback_data="code_password_edit"))
    markup.add(types.InlineKeyboardButton("📁 File Upload Code", callback_data="code_upload"))
    return markup


def set_pending_action(chat_id: str, action: str):
    with STORE_LOCK:
        PENDING_ACTIONS[str(chat_id)] = action


def pop_pending_action(chat_id: str):
    with STORE_LOCK:
        return PENDING_ACTIONS.pop(str(chat_id), None)


def get_pending_action(chat_id: str):
    with STORE_LOCK:
        return PENDING_ACTIONS.get(str(chat_id))


def escape_text(value) -> str:
    return str(value).replace("`", "'")


def create_premium_code(created_by: str) -> str:
    code = "PREM-" + uuid.uuid4().hex[:4].upper() + "-" + uuid.uuid4().hex[:4].upper() + "-" + uuid.uuid4().hex[:4].upper()
    with STORE_LOCK:
        codes = load_premium_codes()
        codes[code] = {
            "used": False,
            "created_by": str(created_by),
            "created_at": now_iso(),
            "used_by": "",
            "used_at": "",
        }
        save_premium_codes(codes)
    return code


def activate_premium_for_user(chat_id: str, code: str) -> tuple[bool, str]:
    chat_id = str(chat_id)
    with STORE_LOCK:
        codes = load_premium_codes()
        if code not in codes:
            return False, "Invalid premium code."
        if codes[code].get("used"):
            return False, "This premium code was already used."

        users = load_users()
        if chat_id not in users:
            return False, "User account not found."

        users[chat_id]["premium"] = True
        users[chat_id]["premium_activated_at"] = now_iso()
        save_users(users)

        codes[code]["used"] = True
        codes[code]["used_by"] = chat_id
        codes[code]["used_at"] = now_iso()
        save_premium_codes(codes)

    return True, "Premium activated successfully."


def get_db_file(dev_info: dict) -> str:
    return os.path.join(DATA_DIR, f"{dev_info['api_key']}_db.json")


def get_auth_file(dev_info: dict) -> str:
    return os.path.join(DATA_DIR, f"{dev_info['api_key']}_auth.json")


def load_dev_db(dev_info: dict) -> dict:
    path = get_db_file(dev_info)
    data = load_json_file(path, {})
    return data if isinstance(data, dict) else {}


def save_dev_db(dev_info: dict, data: dict) -> None:
    save_json_file(get_db_file(dev_info), data)


def load_dev_auth(dev_info: dict) -> dict:
    path = get_auth_file(dev_info)
    data = load_json_file(path, {})
    return data if isinstance(data, dict) else {}


def save_dev_auth(dev_info: dict, data: dict) -> None:
    save_json_file(get_auth_file(dev_info), data)


# =============================================================================
# FLASK ROUTES
# =============================================================================

@app.route("/", methods=["GET"])
def index():
    return jsonify(
        {
            "status": "ok",
            "service": "CloudNest Backend Manager",
            "time": now_iso(),
        }
    ), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify(
        {
            "status": "healthy",
            "service": "CloudNest Backend Manager",
            "time": now_iso(),
        }
    ), 200


@app.route("/api/db", methods=["POST"])
def api_db():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()
    action = (data.get("action") or "").strip().lower()
    key = str(data.get("key", "default"))
    payload = data.get("data", "")

    user_id, dev_info = get_user_by_api_key(api_key)
    if not user_id:
        return jsonify({"status": "error", "message": "Invalid API Key."}), 401

    allowed, user_info = consume_feature(user_id, "db_ops")
    if not allowed and not user_info.get("premium"):
        used, limit, pct = feature_limit_status(user_info, "db_ops")
        return jsonify(
            {
                "status": "error",
                "message": "Free database limit reached.",
                "usage": {"used": used, "limit": limit, "percent": pct},
            }
        ), 429

    db_data = load_dev_db(dev_info)

    if action == "save":
        db_data[key] = payload
        save_dev_db(dev_info, db_data)
        return jsonify({"status": "success", "message": "Data saved!"})

    if action == "load":
        return jsonify({"status": "success", "data": db_data.get(key, "")})

    return jsonify({"status": "error", "message": "Invalid action."}), 400


@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()
    action = (data.get("action") or "").strip().lower()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    new_password = str(data.get("new_password") or data.get("password_new") or "").strip()

    user_id, dev_info = get_user_by_api_key(api_key)
    if not user_id:
        return jsonify({"status": "error", "message": "Invalid API Key."}), 401

    allowed, user_info = consume_feature(user_id, "auth_ops")
    if not allowed and not user_info.get("premium"):
        used, limit, pct = feature_limit_status(user_info, "auth_ops")
        return jsonify(
            {
                "status": "error",
                "message": "Free authentication limit reached.",
                "usage": {"used": used, "limit": limit, "percent": pct},
            }
        ), 429

    auth_data = load_dev_auth(dev_info)

    if action == "register":
        if not username or not password:
            return jsonify({"status": "error", "message": "username and password are required."}), 400
        if username in auth_data:
            return jsonify({"status": "error", "message": "User exists!"}), 409
        auth_data[username] = {"password": password, "created_at": now_iso()}
        save_dev_auth(dev_info, auth_data)
        return jsonify({"status": "success", "message": "Registered successfully!"})

    if action == "login":
        if username in auth_data and auth_data[username].get("password") == password:
            return jsonify({"status": "success", "message": "Logged in successfully!"})
        return jsonify({"status": "error", "message": "Wrong credentials."}), 401

    if action == "update_password":
        if not username or not new_password:
            return jsonify({"status": "error", "message": "username and new_password are required."}), 400
        if username not in auth_data:
            return jsonify({"status": "error", "message": "User not found."}), 404

        # Admin can update directly. Others must provide current password.
        old_password = str(data.get("old_password") or "").strip()
        if not is_admin(user_id):
            if auth_data[username].get("password") != password and auth_data[username].get("password") != old_password:
                return jsonify({"status": "error", "message": "Current password is wrong."}), 401

        auth_data[username]["password"] = new_password
        auth_data[username]["updated_at"] = now_iso()
        save_dev_auth(dev_info, auth_data)
        return jsonify({"status": "success", "message": "Password updated successfully!"})

    return jsonify({"status": "error", "message": "Invalid action."}), 400


@app.route("/api/upload", methods=["POST"])
def upload_file():
    api_key = (request.form.get("api_key") or "").strip()
    user_id, dev_info = get_user_by_api_key(api_key)
    if not user_id:
        return jsonify({"status": "error", "message": "Invalid API key"}), 401

    allowed, user_info = consume_feature(user_id, "upload_ops")
    if not allowed and not user_info.get("premium"):
        used, limit, pct = feature_limit_status(user_info, "upload_ops")
        return jsonify(
            {
                "status": "error",
                "message": "Free upload limit reached.",
                "usage": {"used": used, "limit": limit, "percent": pct},
            }
        ), 429

    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400

    file = request.files["file"]
    if not file or file.filename == "":
        return jsonify({"status": "error", "message": "Empty file"}), 400

    filename = secure_filename(file.filename)
    unique_filename = f"{dev_info['api_key']}_{uuid.uuid4().hex[:8]}_{filename}"
    file.save(os.path.join(UPLOAD_FOLDER, unique_filename))

    file_url = f"{get_public_base_url()}/uploads/{unique_filename}"
    return jsonify({"status": "success", "url": file_url, "filename": filename})


@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# =============================================================================
# TELEGRAM BOT
# =============================================================================

def send_welcome(chat_id: str):
    user_info = ensure_user(chat_id)
    text = (
        "🎉 Account Auto-Registered!\n"
        "Your Telegram ID is linked with your CloudNest API key.\n\n"
        f"API Key: {user_info['api_key']}\n\n"
        "Use the menu below."
    )
    bot.send_message(chat_id, text, reply_markup=main_keyboard(chat_id))


@bot.message_handler(commands=["start", "restart"])
def command_start(message):
    chat_id = str(message.chat.id)
    send_welcome(chat_id)


@bot.message_handler(commands=["health"])
def command_health(message):
    bot.send_message(message.chat.id, "healthy")


def show_database(chat_id: str):
    user_info = ensure_user(chat_id)
    db_file = get_db_file(user_info)
    db_data = load_json_file(db_file, {})
    if not db_data:
        bot.send_message(chat_id, "🗄 Database is empty.\n\n" + usage_summary(user_info), reply_markup=main_keyboard(chat_id))
        return

    msg = ["🗄 Your Database Entries:\n"]
    for key, val in db_data.items():
        preview = str(val)
        if len(preview) > 80:
            preview = preview[:80] + "..."
        msg.append(f"- {key}: {preview}")
    msg.append("")
    msg.append(usage_summary(user_info))
    bot.send_message(chat_id, "\n".join(msg), reply_markup=main_keyboard(chat_id))


def show_auth_users(chat_id: str):
    user_info = ensure_user(chat_id)
    auth_file = get_auth_file(user_info)
    auth_data = load_json_file(auth_file, {})
    if not auth_data:
        bot.send_message(chat_id, "No auth users registered yet.\n\n" + usage_summary(user_info), reply_markup=main_keyboard(chat_id))
        return

    lines = ["👥 App Users List:\n"]
    for username, details in auth_data.items():
        password = str(details.get("password", ""))
        lines.append(f"- {username} | password: {password}")
    lines.append("")
    lines.append(usage_summary(user_info))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=main_keyboard(chat_id))


def show_premium_menu(chat_id: str):
    user_info = ensure_user(chat_id)
    premium = bool(user_info.get("premium"))
    lines = []
    if premium:
        lines.append("⭐ Premium status: ACTIVE")
        lines.append(f"Activated at: {user_info.get('premium_activated_at') or 'N/A'}")
        lines.append("All features are unlimited.")
    else:
        lines.append("⭐ Premium status: FREE")
        lines.append("Redeem a one-time code to activate Premium.")
    lines.append("")
    lines.append("Usage:")
    lines.append(usage_summary(user_info))
    bot.send_message(
        chat_id,
        "\n".join(lines),
        reply_markup=premium_inline_keyboard(is_admin(chat_id)),
    )


def show_project_settings(chat_id: str):
    user_info = ensure_user(chat_id)
    host = get_public_base_url()
    api_key = user_info["api_key"]
    text = (
        f"⚙️ Your API Key:\n{api_key}\n\n"
        f"Base URL:\n{host}\n\n"
        f"Usage:\n{usage_summary(user_info)}"
    )
    bot.send_message(chat_id, text, reply_markup=project_inline_keyboard())


def generate_and_send_premium_code(chat_id: str):
    if not is_admin(chat_id):
        bot.send_message(chat_id, "You are not allowed to create premium codes.")
        return
    code = create_premium_code(chat_id)
    bot.send_message(
        chat_id,
        f"✅ Premium code created successfully:\n\n{code}\n\nThis code can be used only once.",
        reply_markup=main_keyboard(chat_id),
    )


def prompt_redeem_premium(chat_id: str):
    set_pending_action(chat_id, "redeem_premium")
    bot.send_message(chat_id, "Send your premium redeem code now.")


def prompt_edit_password(chat_id: str):
    set_pending_action(chat_id, "edit_password")
    bot.send_message(chat_id, "Send in this format:\nusername|new_password")


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_messages(message):
    chat_id = str(message.chat.id)
    text = (message.text or "").strip()

    if not text:
        return

    ensure_user(chat_id)

    pending = get_pending_action(chat_id)
    if pending == "redeem_premium":
        pop_pending_action(chat_id)
        ok, msg = activate_premium_for_user(chat_id, text)
        bot.send_message(chat_id, ("✅ " if ok else "❌ ") + msg, reply_markup=main_keyboard(chat_id))
        return

    if pending == "edit_password":
        pop_pending_action(chat_id)
        # expected: username|new_password
        if "|" in text:
            username, new_password = [x.strip() for x in text.split("|", 1)]
        elif "," in text:
            username, new_password = [x.strip() for x in text.split(",", 1)]
        else:
            bot.send_message(chat_id, "Wrong format. Use: username|new_password", reply_markup=main_keyboard(chat_id))
            return

        user_info = ensure_user(chat_id)
        allowed, _ = consume_feature(chat_id, "password_edits")
        if not allowed and not user_info.get("premium"):
            used, limit, pct = feature_limit_status(user_info, "password_edits")
            bot.send_message(
                chat_id,
                f"Free password-edit limit reached.\nUsed: {used}/{limit} ({pct}%)",
                reply_markup=main_keyboard(chat_id),
            )
            return

        auth_file = get_auth_file(user_info)
        auth_data = load_json_file(auth_file, {})
        if username not in auth_data:
            bot.send_message(chat_id, "User not found.", reply_markup=main_keyboard(chat_id))
            return
        auth_data[username]["password"] = new_password
        auth_data[username]["updated_at"] = now_iso()
        save_json_file(auth_file, auth_data)
        bot.send_message(chat_id, f"✅ Password updated for user: {username}", reply_markup=main_keyboard(chat_id))
        return

    # Main menu
    if text == "Database":
        allowed, user_info = consume_feature(chat_id, "db_ops")
        if not allowed and not user_info.get("premium"):
            used, limit, pct = feature_limit_status(user_info, "db_ops")
            bot.send_message(chat_id, f"Free database limit reached.\nUsed: {used}/{limit} ({pct}%)", reply_markup=main_keyboard(chat_id))
            return
        show_database(chat_id)
        return

    if text == "Authentication":
        allowed, user_info = consume_feature(chat_id, "auth_ops")
        if not allowed and not user_info.get("premium"):
            used, limit, pct = feature_limit_status(user_info, "auth_ops")
            bot.send_message(chat_id, f"Free authentication limit reached.\nUsed: {used}/{limit} ({pct}%)", reply_markup=main_keyboard(chat_id))
            return
        bot.send_message(chat_id, "Authentication panel:", reply_markup=auth_inline_keyboard())
        return

    if text == "Premium":
        show_premium_menu(chat_id)
        return

    if text == "Project Settings":
        show_project_settings(chat_id)
        return

    if text == "Create premium":
        generate_and_send_premium_code(chat_id)
        return

    # Unknown text: just show menu again
    bot.send_message(chat_id, "Use the menu buttons below.", reply_markup=main_keyboard(chat_id))


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = str(call.message.chat.id)
    data = call.data

    ensure_user(chat_id)

    if data == "show_auth":
        bot.answer_callback_query(call.id)
        show_auth_users(chat_id)
        return

    if data == "edit_password":
        bot.answer_callback_query(call.id)
        prompt_edit_password(chat_id)
        return

    if data == "premium_redeem":
        bot.answer_callback_query(call.id)
        prompt_redeem_premium(chat_id)
        return

    if data == "premium_create":
        bot.answer_callback_query(call.id)
        generate_and_send_premium_code(chat_id)
        return

    user_info = ensure_user(chat_id)
    api_key = user_info["api_key"]
    host = get_public_base_url()

    if data == "code_db":
        code = f"""fetch('{host}/api/db', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'save',
    key: 'message_1',
    data: 'Hello World'
  }})
}}).then(r => r.json()).then(console.log);

fetch('{host}/api/db', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'load',
    key: 'message_1'
  }})
}}).then(r => r.json()).then(console.log);"""
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"Database API Code:\n\n{code}")
        return

    if data == "code_auth":
        code = f"""fetch('{host}/api/auth', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'register',
    username: 'user1',
    password: '123'
  }})
}}).then(r => r.json()).then(console.log);

fetch('{host}/api/auth', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'login',
    username: 'user1',
    password: '123'
  }})
}}).then(r => r.json()).then(console.log);"""
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"Auth API Code:\n\n{code}")
        return

    if data == "code_password_edit":
        code = f"""fetch('{host}/api/auth', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'update_password',
    username: 'user1',
    new_password: 'new123'
  }})
}}).then(r => r.json()).then(console.log);"""
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"Password Edit API Code:\n\n{code}")
        return

    if data == "code_upload":
        code = f"""const formData = new FormData();
formData.append('api_key', '{api_key}');
formData.append('file', fileInput.files[0]);

fetch('{host}/api/upload', {{
  method: 'POST',
  body: formData
}}).then(r => r.json()).then(console.log);"""
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"File Upload API Code:\n\n{code}")
        return

    bot.answer_callback_query(call.id, "Unknown action.")


# =============================================================================
# BOT / APP RUNNER
# =============================================================================

def run_bot():
    while True:
        try:
            bot.remove_webhook()
        except Exception:
            pass
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=True)
        except Exception as e:
            print(f"[BOT] polling error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    print("CloudNest backend starting...")
    print(f"Port: {PORT}")
    print(f"Base URL: {get_public_base_url()}")
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
