import json
import os
import threading
import time
import uuid
import smtplib
import random
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

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
PORT = int(os.environ.get("PORT", "8080"))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required.")

ADMIN_CHAT_IDS = {x.strip() for x in ADMIN_CHAT_IDS_RAW.split(",") if x.strip()}
if not ADMIN_CHAT_IDS:
    ADMIN_CHAT_IDS = set()

# SMTP CONFIG FOR OTP
SMTP_EMAIL = "cloudnestotp@gmail.com"
SMTP_PASSWORD = "smeu dhdn zdou yfwc"

# ADMIN API KEY
ADMIN_API_KEY = "rf_admin250fahim771357013"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "uploads")
USER_DATA_FILE = os.path.join(DATA_DIR, "users.json")
PREMIUM_CODES_FILE = os.path.join(DATA_DIR, "premium_codes.json")
SESSION_FILE = os.path.join(DATA_DIR, "sessions.json")

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
TEMP_AUTH_STATE = {}
DEV_OTPS = {}  # Store Developer OTPs temporarily

FREE_LIMITS = {
    "db_ops": 1073741824,      # 1 GB (per month)
    "auth_ops": 50,            # 50 Members
    "upload_ops": 2684354560,  # 2.5 GB
    "password_edits": 50,
    "otp_sends": 50            # Added Limit for OTP Sends
}

FREE_LIMITS_DISPLAY = {
    "db_ops": "1 GB/month",
    "auth_ops": "50 Members/month",
    "upload_ops": "2.5 GB/month",
    "password_edits": "50/month",
    "otp_sends": "50 OTPs/month"
}

LANGUAGES = ["JavaScript", "Python", "Kotlin", "Swift", "Dart", "PHP", "Java", "C#"]

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
        return users if isinstance(users, dict) else {}

def save_users(users: dict) -> None:
    with STORE_LOCK:
        save_json_file(USER_DATA_FILE, users)

def load_sessions() -> dict:
    with STORE_LOCK:
        sessions = load_json_file(SESSION_FILE, {})
        return sessions if isinstance(sessions, dict) else {}

def save_sessions(sessions: dict) -> None:
    with STORE_LOCK:
        save_json_file(SESSION_FILE, sessions)

def load_premium_codes() -> dict:
    with STORE_LOCK:
        codes = load_json_file(PREMIUM_CODES_FILE, {})
        return codes if isinstance(codes, dict) else {}

def save_premium_codes(codes: dict) -> None:
    with STORE_LOCK:
        save_json_file(PREMIUM_CODES_FILE, codes)

def is_admin(chat_id: str) -> bool:
    return str(chat_id) in ADMIN_CHAT_IDS

def get_public_base_url() -> str:
    # Render নিজে থেকেই লাইভ URL এই ভেরিয়েবলে সেভ করে রাখে
    render_url = os.environ.get("RENDER_EXTERNAL_URL")
    if render_url:
        return render_url.rstrip("/")
    
    # যদি লোকাল কম্পিউটারে রান করেন তার জন্য ফলব্যাক
    try:
        from flask import request
        if request and request.url_root:
            return request.url_root.rstrip("/")
    except Exception:
        pass
        
    return "http://localhost:8080"

# --- EMAIL SENDER (FACEBOOK STYLE TEMPLATE FOR BOT REGISTRATION) ---
def send_otp_email(to_email, otp_code):
    try:
        msg = MIMEMultipart("alternative")
        msg['Subject'] = "Your CloudNest Security Code"
        msg['From'] = SMTP_EMAIL
        msg['To'] = to_email

        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: auto; border: 1px solid #e5e5e5; border-radius: 8px; padding: 20px; text-align: center; color: #333;">
            <h2 style="color: #1877F2; margin-bottom: 10px;">CloudNest Security</h2>
            <p style="font-size: 16px;">We received a request to verify your email. Your security code is below. This code will expire in 5 minutes.</p>
            <div style="font-size: 28px; font-weight: bold; background: #f0f2f5; padding: 15px; margin: 20px 0; border-radius: 8px; letter-spacing: 5px; color: #1c1e21;">
                {otp_code}
            </div>
            <p style="font-size: 12px; color: #888;">If you did not request this code, you can safely ignore this email. Someone else might have typed your email address by mistake.</p>
            <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;">
            <p style="font-size: 11px; color: #aaa;">CloudNest Dev Platform</p>
        </div>
        """
        msg.attach(MIMEText(html, "html"))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print("SMTP Error:", e)
        return False

# --- EMAIL SENDER (BASIC TEMPLATE FOR API USERS WITH PROMOTION) ---
def send_user_otp_email(to_email, otp_code):
    try:
        msg = MIMEMultipart("alternative")
        msg['Subject'] = "Your Verification Code"
        msg['From'] = SMTP_EMAIL
        msg['To'] = to_email

        html = f"""
        <div style="font-family: Arial, sans-serif; max-width: 500px; margin: auto; padding: 20px; color: #333;">
            <h2>Verification Code</h2>
            <p>Your requested code is: <strong>{otp_code}</strong></p>
            <p>Please use this code to verify your action. The code will expire in 5 minutes.</p>
            <br>
            <hr style="border: 0; border-top: 1px solid #eee; margin: 20px 0;">
            <p style="font-size: 12px; color: #666;">
                <em>This OTP service is powered by CloudNest API.</em><br><br>
                Build your own advanced backend easily with <a href="https://t.me/Cloud_Nest_bot" style="color: #1877F2;">@Cloud_Nest_bot</a>
            </p>
        </div>
        """
        msg.attach(MIMEText(html, "html"))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.sendmail(SMTP_EMAIL, to_email, msg.as_string())
        server.quit()
        return True
    except Exception as e:
        print("SMTP Error (User OTP):", e)
        return False

# --- SESSION & USER MANAGMENT ---
def get_logged_in_user(chat_id: str):
    chat_id = str(chat_id)
    with STORE_LOCK:
        sessions = load_sessions()
        email = sessions.get(chat_id)
        if not email:
            return None, None
        
        users = load_users()
        user_info = users.get(email)
        if not user_info:
            return None, None
            
        # Check if premium expired
        if user_info.get("premium") and user_info.get("premium_expires_at"):
            exp_date = datetime.fromisoformat(user_info["premium_expires_at"])
            if datetime.now(timezone.utc) > exp_date:
                user_info["premium"] = False
                user_info["premium_expires_at"] = ""
                users[email] = user_info
                save_users(users)
                
        return email, user_info

def get_user_by_api_key(api_key: str):
    if not api_key:
        return None, None
    if api_key == ADMIN_API_KEY:
        return "admin@cloudnest", {
            "email": "admin@cloudnest",
            "api_key": ADMIN_API_KEY,
            "premium": True,
            "usage": {}
        }
    users = load_users()
    for email, info in users.items():
        if info.get("api_key") == api_key:
            return email, info
    return None, None

def feature_limit_status(user_info: dict, feature: str) -> tuple:
    used = int((user_info.get("usage") or {}).get(feature, 0))
    limit = int(FREE_LIMITS.get(feature, 0))
    if user_info.get("premium"):
        return used, limit, "Unlimited"
    percent = (used / limit * 100.0) if limit else 0.0
    return used, limit, round(percent, 1)

def consume_feature(email: str, feature: str) -> tuple:
    with STORE_LOCK:
        users = load_users()
        # Handle Admin API Bypass correctly
        if email == "admin@cloudnest":
            return True, {"premium": True}

        user_info = users.get(email)
        if not user_info:
            return False, {}
        if user_info.get("premium"):
            user_info.setdefault("usage", {})
            user_info["usage"][feature] = int(user_info["usage"].get(feature, 0)) + 1
            users[email] = user_info
            save_users(users)
            return True, user_info

        user_info.setdefault("usage", {})
        used = int(user_info["usage"].get(feature, 0))
        limit = int(FREE_LIMITS.get(feature, 0))
        if limit and used >= limit:
            users[email] = user_info
            save_users(users)
            return False, user_info

        user_info["usage"][feature] = used + 1
        users[email] = user_info
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
        display_limit = FREE_LIMITS_DISPLAY.get(feature, str(limit))
        if user_info.get("premium"):
            lines.append(f"- {feature}: {used} used | Premium = Unlimited")
        else:
            lines.append(f"- {feature}: {used}/{display_limit}")
    return "\n".join(lines)


# =============================================================================
# KEYBOARDS
# =============================================================================

def auth_welcome_keyboard():
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    markup.add(types.KeyboardButton("Register"), types.KeyboardButton("Login"))
    return markup

def main_keyboard(chat_id: str):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    buttons = [
        types.KeyboardButton("Database"),
        types.KeyboardButton("Authentication"),
        types.KeyboardButton("Storage"),
        types.KeyboardButton("Premium"),
        types.KeyboardButton("Project Settings"),
        types.KeyboardButton("Logout")
    ]
    if is_admin(chat_id):
        buttons.append(types.KeyboardButton("Create premium"))
    markup.add(*buttons)
    return markup

def premium_inline_keyboard(is_admin_user: bool):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Redeem Premium Code", callback_data="premium_redeem"))
    return markup

def premium_duration_keyboard():
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.add(
        types.InlineKeyboardButton("1 Day", callback_data="premgen_1"),
        types.InlineKeyboardButton("7 Days", callback_data="premgen_7"),
        types.InlineKeyboardButton("1 Month", callback_data="premgen_30"),
        types.InlineKeyboardButton("3 Months", callback_data="premgen_90"),
        types.InlineKeyboardButton("1 Year", callback_data="premgen_365")
    )
    return markup

def auth_inline_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Show Auth Users", callback_data="show_auth"))
    markup.add(types.InlineKeyboardButton("Edit Password", callback_data="edit_password"))
    return markup

def project_inline_keyboard():
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🗄 Database (DB)", callback_data="proj_db"))
    markup.add(types.InlineKeyboardButton("👥 Authentication", callback_data="proj_auth"))
    markup.add(types.InlineKeyboardButton("📁 Storage (Upload)", callback_data="proj_storage"))
    markup.add(types.InlineKeyboardButton("📧 OTP Sent (System)", callback_data="proj_otp"))
    return markup

def lang_keyboard(section: str):
    markup = types.InlineKeyboardMarkup(row_width=2)
    buttons = []
    for lang in LANGUAGES:
        buttons.append(types.InlineKeyboardButton(lang, callback_data=f"lang_{section}_{lang.lower()}"))
    markup.add(*buttons)
    return markup

def db_ops_keyboard(lang: str):
    markup = types.InlineKeyboardMarkup(row_width=2)
    ops = ["Data Save", "Data Load", "Data Change", "Data Delete"]
    for op in ops:
        op_key = op.lower().replace(" ", "_")
        markup.add(types.InlineKeyboardButton(op, callback_data=f"dbop_{lang}_{op_key}"))
    return markup

def auth_ops_keyboard(lang: str):
    markup = types.InlineKeyboardMarkup(row_width=2)
    ops = ["Login", "Register", "Auth Load", "Auth Delete", "Password Change"]
    for op in ops:
        op_key = op.lower().replace(" ", "_")
        markup.add(types.InlineKeyboardButton(op, callback_data=f"authop_{lang}_{op_key}"))
    return markup

def storage_ops_keyboard(lang: str):
    markup = types.InlineKeyboardMarkup(row_width=2)
    ops = ["Upload", "Load", "Delete"]
    for op in ops:
        op_key = op.lower().replace(" ", "_")
        markup.add(types.InlineKeyboardButton(op, callback_data=f"storop_{lang}_{op_key}"))
    return markup

def otp_ops_keyboard(lang: str):
    markup = types.InlineKeyboardMarkup(row_width=2)
    ops = ["OTP Send", "OTP Verify"]
    for op in ops:
        op_key = op.lower().replace(" ", "_")
        markup.add(types.InlineKeyboardButton(op, callback_data=f"otpop_{lang}_{op_key}"))
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

# --- FILESYSTEM HELPERS ---

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
# CODE GENERATORS — All Languages, All Operations
# =============================================================================

def get_otp_code(lang: str, op: str, api_key: str, host: str) -> str:
    lang = lang.lower()
    op = op.lower()

    codes = {
        "javascript": {
            "otp_send": f"""// ✅ JavaScript — OTP Send
fetch('{host}/api/otp/send', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{ api_key: '{api_key}', email: 'user@gmail.com' }})
}}).then(r => r.json()).then(console.log);""",
            "otp_verify": f"""// ✅ JavaScript — OTP Verify
fetch('{host}/api/otp/verify', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{ api_key: '{api_key}', email: 'user@gmail.com', otp: '123456' }})
}}).then(r => r.json()).then(console.log);"""
        },
        "python": {
            "otp_send": f"""# ✅ Python — OTP Send
import requests
res = requests.post('{host}/api/otp/send', json={{ 'api_key': '{api_key}', 'email': 'user@gmail.com' }})
print(res.json())""",
            "otp_verify": f"""# ✅ Python — OTP Verify
import requests
res = requests.post('{host}/api/otp/verify', json={{ 'api_key': '{api_key}', 'email': 'user@gmail.com', 'otp': '123456' }})
print(res.json())"""
        },
        "kotlin": {
            "otp_send": f"""// ✅ Kotlin — OTP Send
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("email", "user@gmail.com")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/otp/send").post(body).build()
OkHttpClient().newCall(request).execute().use {{ println(it.body?.string()) }}""",
            "otp_verify": f"""// ✅ Kotlin — OTP Verify
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("email", "user@gmail.com")
json.put("otp", "123456")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/otp/verify").post(body).build()
OkHttpClient().newCall(request).execute().use {{ println(it.body?.string()) }}"""
        },
        "swift": {
            "otp_send": f"""// ✅ Swift — OTP Send
var req = URLRequest(url: URL(string: "{host}/api/otp/send")!)
req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try? JSONSerialization.data(withJSONObject: ["api_key": "{api_key}", "email": "user@gmail.com"])
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()""",
            "otp_verify": f"""// ✅ Swift — OTP Verify
var req = URLRequest(url: URL(string: "{host}/api/otp/verify")!)
req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try? JSONSerialization.data(withJSONObject: ["api_key": "{api_key}", "email": "user@gmail.com", "otp": "123456"])
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()"""
        },
        "dart": {
            "otp_send": f"""// ✅ Dart — OTP Send
final res = await http.post(Uri.parse('{host}/api/otp/send'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{'api_key': '{api_key}', 'email': 'user@gmail.com'}}));
print(res.body);""",
            "otp_verify": f"""// ✅ Dart — OTP Verify
final res = await http.post(Uri.parse('{host}/api/otp/verify'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{'api_key': '{api_key}', 'email': 'user@gmail.com', 'otp': '123456'}}));
print(res.body);"""
        },
        "php": {
            "otp_send": f"""<?php // ✅ PHP — OTP Send
$ch = curl_init('{host}/api/otp/send');
curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true, CURLOPT_HTTPHEADER => ['Content-Type: application/json'], CURLOPT_POSTFIELDS => json_encode(['api_key' => '{api_key}', 'email' => 'user@gmail.com'])]);
echo curl_exec($ch);""",
            "otp_verify": f"""<?php // ✅ PHP — OTP Verify
$ch = curl_init('{host}/api/otp/verify');
curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true, CURLOPT_HTTPHEADER => ['Content-Type: application/json'], CURLOPT_POSTFIELDS => json_encode(['api_key' => '{api_key}', 'email' => 'user@gmail.com', 'otp' => '123456'])]);
echo curl_exec($ch);"""
        },
        "java": {
            "otp_send": f"""// ✅ Java — OTP Send
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"email\\":\\"user@gmail.com\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/otp/send").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}""",
            "otp_verify": f"""// ✅ Java — OTP Verify
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"email\\":\\"user@gmail.com\\",\\"otp\\":\\"123456\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/otp/verify").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}"""
        },
        "c#": {
            "otp_send": f"""// ✅ C# — OTP Send
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", email = "user@gmail.com" }};
var res = await client.PostAsync("{host}/api/otp/send", new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",
            "otp_verify": f"""// ✅ C# — OTP Verify
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", email = "user@gmail.com", otp = "123456" }};
var res = await client.PostAsync("{host}/api/otp/verify", new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());"""
        }
    }
    return codes.get(lang, {}).get(op, f"// Code for {lang} - {op} not available yet.")

def get_db_code(lang: str, op: str, api_key: str, host: str) -> str:
    lang = lang.lower()
    op = op.lower()

    codes = {
        "javascript": {
            "data_save": f"""// ✅ JavaScript — Data Save
fetch('{host}/api/db', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'save',
    key: 'my_key',
    data: 'Hello World'
  }})
}}).then(r => r.json()).then(console.log);""",

            "data_load": f"""// ✅ JavaScript — Data Load
fetch('{host}/api/db', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'load',
    key: 'my_key'
  }})
}}).then(r => r.json()).then(data => console.log(data.data));""",

            "data_change": f"""// ✅ JavaScript — Data Change (Overwrite)
fetch('{host}/api/db', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'save',
    key: 'my_key',
    data: 'Updated Value'
  }})
}}).then(r => r.json()).then(console.log);""",

            "data_delete": f"""// ✅ JavaScript — Data Delete
fetch('{host}/api/db', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'delete',
    key: 'my_key'
  }})
}}).then(r => r.json()).then(console.log);""",
        },

        "python": {
            "data_save": f"""# ✅ Python — Data Save
import requests
res = requests.post('{host}/api/db', json={{
    'api_key': '{api_key}',
    'action': 'save',
    'key': 'my_key',
    'data': 'Hello World'
}})
print(res.json())""",

            "data_load": f"""# ✅ Python — Data Load
import requests
res = requests.post('{host}/api/db', json={{
    'api_key': '{api_key}',
    'action': 'load',
    'key': 'my_key'
}})
print(res.json()['data'])""",

            "data_change": f"""# ✅ Python — Data Change (Overwrite)
import requests
res = requests.post('{host}/api/db', json={{
    'api_key': '{api_key}',
    'action': 'save',
    'key': 'my_key',
    'data': 'Updated Value'
}})
print(res.json())""",

            "data_delete": f"""# ✅ Python — Data Delete
import requests
res = requests.post('{host}/api/db', json={{
    'api_key': '{api_key}',
    'action': 'delete',
    'key': 'my_key'
}})
print(res.json())""",
        },

        "kotlin": {
            "data_save": f"""// ✅ Kotlin — Data Save
val client = OkHttpClient()
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "save")
json.put("key", "my_key")
json.put("data", "Hello World")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/db").post(body).build()
client.newCall(request).execute().use {{ println(it.body?.string()) }}""",

            "data_load": f"""// ✅ Kotlin — Data Load
val client = OkHttpClient()
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "load")
json.put("key", "my_key")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/db").post(body).build()
client.newCall(request).execute().use {{ println(it.body?.string()) }}""",

            "data_change": f"""// ✅ Kotlin — Data Change
val client = OkHttpClient()
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "save")
json.put("key", "my_key")
json.put("data", "Updated Value")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/db").post(body).build()
client.newCall(request).execute().use {{ println(it.body?.string()) }}""",

            "data_delete": f"""// ✅ Kotlin — Data Delete
val client = OkHttpClient()
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "delete")
json.put("key", "my_key")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/db").post(body).build()
client.newCall(request).execute().use {{ println(it.body?.string()) }}""",
        },

        "swift": {
            "data_save": f"""// ✅ Swift — Data Save
var request = URLRequest(url: URL(string: "{host}/api/db")!)
request.httpMethod = "POST"
request.setValue("application/json", forHTTPHeaderField: "Content-Type")
let body: [String: Any] = ["api_key": "{api_key}", "action": "save", "key": "my_key", "data": "Hello World"]
request.httpBody = try? JSONSerialization.data(withJSONObject: body)
URLSession.shared.dataTask(with: request) {{ data, _, _ in
    if let data = data {{ print(String(data: data, encoding: .utf8)!) }}
}}.resume()""",

            "data_load": f"""// ✅ Swift — Data Load
var request = URLRequest(url: URL(string: "{host}/api/db")!)
request.httpMethod = "POST"
request.setValue("application/json", forHTTPHeaderField: "Content-Type")
let body: [String: Any] = ["api_key": "{api_key}", "action": "load", "key": "my_key"]
request.httpBody = try? JSONSerialization.data(withJSONObject: body)
URLSession.shared.dataTask(with: request) {{ data, _, _ in
    if let data = data {{ print(String(data: data, encoding: .utf8)!) }}
}}.resume()""",

            "data_change": f"""// ✅ Swift — Data Change
var request = URLRequest(url: URL(string: "{host}/api/db")!)
request.httpMethod = "POST"
request.setValue("application/json", forHTTPHeaderField: "Content-Type")
let body: [String: Any] = ["api_key": "{api_key}", "action": "save", "key": "my_key", "data": "Updated Value"]
request.httpBody = try? JSONSerialization.data(withJSONObject: body)
URLSession.shared.dataTask(with: request) {{ data, _, _ in
    if let data = data {{ print(String(data: data, encoding: .utf8)!) }}
}}.resume()""",

            "data_delete": f"""// ✅ Swift — Data Delete
var request = URLRequest(url: URL(string: "{host}/api/db")!)
request.httpMethod = "POST"
request.setValue("application/json", forHTTPHeaderField: "Content-Type")
let body: [String: Any] = ["api_key": "{api_key}", "action": "delete", "key": "my_key"]
request.httpBody = try? JSONSerialization.data(withJSONObject: body)
URLSession.shared.dataTask(with: request) {{ data, _, _ in
    if let data = data {{ print(String(data: data, encoding: .utf8)!) }}
}}.resume()""",
        },

        "dart": {
            "data_save": f"""// ✅ Dart — Data Save
import 'package:http/http.dart' as http;
import 'dart:convert';

final res = await http.post(
  Uri.parse('{host}/api/db'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{
    'api_key': '{api_key}',
    'action': 'save',
    'key': 'my_key',
    'data': 'Hello World'
  }}),
);
print(res.body);""",

            "data_load": f"""// ✅ Dart — Data Load
import 'package:http/http.dart' as http;
import 'dart:convert';

final res = await http.post(
  Uri.parse('{host}/api/db'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{
    'api_key': '{api_key}',
    'action': 'load',
    'key': 'my_key'
  }}),
);
print(jsonDecode(res.body)['data']);""",

            "data_change": f"""// ✅ Dart — Data Change
import 'package:http/http.dart' as http;
import 'dart:convert';

final res = await http.post(
  Uri.parse('{host}/api/db'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{
    'api_key': '{api_key}',
    'action': 'save',
    'key': 'my_key',
    'data': 'Updated Value'
  }}),
);
print(res.body);""",

            "data_delete": f"""// ✅ Dart — Data Delete
import 'package:http/http.dart' as http;
import 'dart:convert';

final res = await http.post(
  Uri.parse('{host}/api/db'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{
    'api_key': '{api_key}',
    'action': 'delete',
    'key': 'my_key'
  }}),
);
print(res.body);""",
        },

        "php": {
            "data_save": f"""<?php // ✅ PHP — Data Save
$ch = curl_init('{host}/api/db');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode([
  'api_key' => '{api_key}',
  'action' => 'save',
  'key' => 'my_key',
  'data' => 'Hello World'
]));
echo curl_exec($ch);""",

            "data_load": f"""<?php // ✅ PHP — Data Load
$ch = curl_init('{host}/api/db');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode([
  'api_key' => '{api_key}',
  'action' => 'load',
  'key' => 'my_key'
]));
$res = json_decode(curl_exec($ch), true);
echo $res['data'];""",

            "data_change": f"""<?php // ✅ PHP — Data Change
$ch = curl_init('{host}/api/db');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode([
  'api_key' => '{api_key}',
  'action' => 'save',
  'key' => 'my_key',
  'data' => 'Updated Value'
]));
echo curl_exec($ch);""",

            "data_delete": f"""<?php // ✅ PHP — Data Delete
$ch = curl_init('{host}/api/db');
curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
curl_setopt($ch, CURLOPT_POST, true);
curl_setopt($ch, CURLOPT_HTTPHEADER, ['Content-Type: application/json']);
curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode([
  'api_key' => '{api_key}',
  'action' => 'delete',
  'key' => 'my_key'
]));
echo curl_exec($ch);""",
        },

        "java": {
            "data_save": f"""// ✅ Java — Data Save
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"save\\",\\"key\\":\\"my_key\\",\\"data\\":\\"Hello World\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/db").post(body).build();
try (Response response = client.newCall(request).execute()) {{
    System.out.println(response.body().string());
}}""",

            "data_load": f"""// ✅ Java — Data Load
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"load\\",\\"key\\":\\"my_key\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/db").post(body).build();
try (Response response = client.newCall(request).execute()) {{
    System.out.println(response.body().string());
}}""",

            "data_change": f"""// ✅ Java — Data Change
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"save\\",\\"key\\":\\"my_key\\",\\"data\\":\\"Updated Value\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/db").post(body).build();
try (Response response = client.newCall(request).execute()) {{
    System.out.println(response.body().string());
}}""",

            "data_delete": f"""// ✅ Java — Data Delete
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"delete\\",\\"key\\":\\"my_key\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/db").post(body).build();
try (Response response = client.newCall(request).execute()) {{
    System.out.println(response.body().string());
}}""",
        },

        "c#": {
            "data_save": f"""// ✅ C# — Data Save
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "save", key = "my_key", data = "Hello World" }};
var res = await client.PostAsync("{host}/api/db",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",

            "data_load": f"""// ✅ C# — Data Load
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "load", key = "my_key" }};
var res = await client.PostAsync("{host}/api/db",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",

            "data_change": f"""// ✅ C# — Data Change
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "save", key = "my_key", data = "Updated Value" }};
var res = await client.PostAsync("{host}/api/db",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",

            "data_delete": f"""// ✅ C# — Data Delete
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "delete", key = "my_key" }};
var res = await client.PostAsync("{host}/api/db",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",
        },
    }

    return codes.get(lang, {}).get(op, f"// Code for {lang} - {op} not available yet.")


def get_auth_code(lang: str, op: str, api_key: str, host: str) -> str:
    lang = lang.lower()
    op = op.lower()

    codes = {
        "javascript": {
            "login": f"""// ✅ JavaScript — Login
fetch('{host}/api/auth', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'login',
    username: 'user1',
    password: 'pass123'
  }})
}}).then(r => r.json()).then(console.log);""",

            "register": f"""// ✅ JavaScript — Register
fetch('{host}/api/auth', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'register',
    username: 'user1',
    password: 'pass123'
  }})
}}).then(r => r.json()).then(console.log);""",

            "auth_load": f"""// ✅ JavaScript — Auth Load (all users)
fetch('{host}/api/auth', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'list'
  }})
}}).then(r => r.json()).then(console.log);""",

            "auth_delete": f"""// ✅ JavaScript — Auth Delete User
fetch('{host}/api/auth', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'delete',
    username: 'user1'
  }})
}}).then(r => r.json()).then(console.log);""",

            "password_change": f"""// ✅ JavaScript — Password Change
fetch('{host}/api/auth', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    action: 'update_password',
    username: 'user1',
    new_password: 'newpass456'
  }})
}}).then(r => r.json()).then(console.log);""",
        },

        "python": {
            "login": f"""# ✅ Python — Login
import requests
res = requests.post('{host}/api/auth', json={{
    'api_key': '{api_key}',
    'action': 'login',
    'username': 'user1',
    'password': 'pass123'
}})
print(res.json())""",

            "register": f"""# ✅ Python — Register
import requests
res = requests.post('{host}/api/auth', json={{
    'api_key': '{api_key}',
    'action': 'register',
    'username': 'user1',
    'password': 'pass123'
}})
print(res.json())""",

            "auth_load": f"""# ✅ Python — Auth Load
import requests
res = requests.post('{host}/api/auth', json={{
    'api_key': '{api_key}',
    'action': 'list'
}})
print(res.json())""",

            "auth_delete": f"""# ✅ Python — Auth Delete User
import requests
res = requests.post('{host}/api/auth', json={{
    'api_key': '{api_key}',
    'action': 'delete',
    'username': 'user1'
}})
print(res.json())""",

            "password_change": f"""# ✅ Python — Password Change
import requests
res = requests.post('{host}/api/auth', json={{
    'api_key': '{api_key}',
    'action': 'update_password',
    'username': 'user1',
    'new_password': 'newpass456'
}})
print(res.json())""",
        },

        "kotlin": {
            "login": f"""// ✅ Kotlin — Login
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "login")
json.put("username", "user1")
json.put("password", "pass123")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/auth").post(body).build()
OkHttpClient().newCall(request).execute().use {{ println(it.body?.string()) }}""",

            "register": f"""// ✅ Kotlin — Register
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "register")
json.put("username", "user1")
json.put("password", "pass123")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/auth").post(body).build()
OkHttpClient().newCall(request).execute().use {{ println(it.body?.string()) }}""",

            "auth_load": f"""// ✅ Kotlin — Auth Load
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "list")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/auth").post(body).build()
OkHttpClient().newCall(request).execute().use {{ println(it.body?.string()) }}""",

            "auth_delete": f"""// ✅ Kotlin — Auth Delete
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "delete")
json.put("username", "user1")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/auth").post(body).build()
OkHttpClient().newCall(request).execute().use {{ println(it.body?.string()) }}""",

            "password_change": f"""// ✅ Kotlin — Password Change
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("action", "update_password")
json.put("username", "user1")
json.put("new_password", "newpass456")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/auth").post(body).build()
OkHttpClient().newCall(request).execute().use {{ println(it.body?.string()) }}""",
        },

        "dart": {
            "login": f"""// ✅ Dart — Login
final res = await http.post(Uri.parse('{host}/api/auth'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{'api_key': '{api_key}', 'action': 'login', 'username': 'user1', 'password': 'pass123'}}));
print(res.body);""",

            "register": f"""// ✅ Dart — Register
final res = await http.post(Uri.parse('{host}/api/auth'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{'api_key': '{api_key}', 'action': 'register', 'username': 'user1', 'password': 'pass123'}}));
print(res.body);""",

            "auth_load": f"""// ✅ Dart — Auth Load
final res = await http.post(Uri.parse('{host}/api/auth'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{'api_key': '{api_key}', 'action': 'list'}}));
print(res.body);""",

            "auth_delete": f"""// ✅ Dart — Auth Delete
final res = await http.post(Uri.parse('{host}/api/auth'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{'api_key': '{api_key}', 'action': 'delete', 'username': 'user1'}}));
print(res.body);""",

            "password_change": f"""// ✅ Dart — Password Change
final res = await http.post(Uri.parse('{host}/api/auth'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{'api_key': '{api_key}', 'action': 'update_password', 'username': 'user1', 'new_password': 'newpass456'}}));
print(res.body);""",
        },

        "php": {
            "login": f"""<?php // ✅ PHP — Login
$ch = curl_init('{host}/api/auth');
curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
  CURLOPT_POSTFIELDS => json_encode(['api_key' => '{api_key}', 'action' => 'login', 'username' => 'user1', 'password' => 'pass123'])]);
echo curl_exec($ch);""",

            "register": f"""<?php // ✅ PHP — Register
$ch = curl_init('{host}/api/auth');
curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
  CURLOPT_POSTFIELDS => json_encode(['api_key' => '{api_key}', 'action' => 'register', 'username' => 'user1', 'password' => 'pass123'])]);
echo curl_exec($ch);""",

            "auth_load": f"""<?php // ✅ PHP — Auth Load
$ch = curl_init('{host}/api/auth');
curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
  CURLOPT_POSTFIELDS => json_encode(['api_key' => '{api_key}', 'action' => 'list'])]);
echo curl_exec($ch);""",

            "auth_delete": f"""<?php // ✅ PHP — Auth Delete
$ch = curl_init('{host}/api/auth');
curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
  CURLOPT_POSTFIELDS => json_encode(['api_key' => '{api_key}', 'action' => 'delete', 'username' => 'user1'])]);
echo curl_exec($ch);""",

            "password_change": f"""<?php // ✅ PHP — Password Change
$ch = curl_init('{host}/api/auth');
curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
  CURLOPT_POSTFIELDS => json_encode(['api_key' => '{api_key}', 'action' => 'update_password', 'username' => 'user1', 'new_password' => 'newpass456'])]);
echo curl_exec($ch);""",
        },

        "swift": {
            "login": f"""// ✅ Swift — Login
var req = URLRequest(url: URL(string: "{host}/api/auth")!)
req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try? JSONSerialization.data(withJSONObject: ["api_key": "{api_key}", "action": "login", "username": "user1", "password": "pass123"])
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()""",

            "register": f"""// ✅ Swift — Register
var req = URLRequest(url: URL(string: "{host}/api/auth")!)
req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try? JSONSerialization.data(withJSONObject: ["api_key": "{api_key}", "action": "register", "username": "user1", "password": "pass123"])
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()""",

            "auth_load": f"""// ✅ Swift — Auth Load
var req = URLRequest(url: URL(string: "{host}/api/auth")!)
req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try? JSONSerialization.data(withJSONObject: ["api_key": "{api_key}", "action": "list"])
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()""",

            "auth_delete": f"""// ✅ Swift — Auth Delete
var req = URLRequest(url: URL(string: "{host}/api/auth")!)
req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try? JSONSerialization.data(withJSONObject: ["api_key": "{api_key}", "action": "delete", "username": "user1"])
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()""",

            "password_change": f"""// ✅ Swift — Password Change
var req = URLRequest(url: URL(string: "{host}/api/auth")!)
req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try? JSONSerialization.data(withJSONObject: ["api_key": "{api_key}", "action": "update_password", "username": "user1", "new_password": "newpass456"])
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()""",
        },

        "java": {
            "login": f"""// ✅ Java — Login
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"login\\",\\"username\\":\\"user1\\",\\"password\\":\\"pass123\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/auth").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}""",

            "register": f"""// ✅ Java — Register
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"register\\",\\"username\\":\\"user1\\",\\"password\\":\\"pass123\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/auth").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}""",

            "auth_load": f"""// ✅ Java — Auth Load
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"list\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/auth").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}""",

            "auth_delete": f"""// ✅ Java — Auth Delete
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"delete\\",\\"username\\":\\"user1\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/auth").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}""",

            "password_change": f"""// ✅ Java — Password Change
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"action\\":\\"update_password\\",\\"username\\":\\"user1\\",\\"new_password\\":\\"newpass456\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/auth").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}""",
        },

        "c#": {
            "login": f"""// ✅ C# — Login
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "login", username = "user1", password = "pass123" }};
var res = await client.PostAsync("{host}/api/auth",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",

            "register": f"""// ✅ C# — Register
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "register", username = "user1", password = "pass123" }};
var res = await client.PostAsync("{host}/api/auth",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",

            "auth_load": f"""// ✅ C# — Auth Load
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "list" }};
var res = await client.PostAsync("{host}/api/auth",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",

            "auth_delete": f"""// ✅ C# — Auth Delete
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "delete", username = "user1" }};
var res = await client.PostAsync("{host}/api/auth",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",

            "password_change": f"""// ✅ C# — Password Change
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", action = "update_password", username = "user1", new_password = "newpass456" }};
var res = await client.PostAsync("{host}/api/auth",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",
        },
    }

    return codes.get(lang, {}).get(op, f"// Code for {lang} - {op} not available yet.")


def get_storage_code(lang: str, op: str, api_key: str, host: str) -> str:
    lang = lang.lower()
    op = op.lower()

    codes = {
        "javascript": {
            "upload": f"""// ✅ JavaScript — File Upload
// HTML: <input type="file" id="fileInput">
const fileInput = document.getElementById('fileInput');
const formData = new FormData();
formData.append('file', fileInput.files[0]);
formData.append('api_key', '{api_key}');

fetch('{host}/api/upload', {{
  method: 'POST',
  body: formData
}}).then(r => r.json()).then(data => {{
  console.log('File URL:', data.url);
}});""",

            "load": f"""// ✅ JavaScript — Load/View File
// Just use the URL returned from upload directly:
// data.url is a public link you can use in <img>, <video>, etc.
const fileUrl = 'YOUR_FILE_URL_HERE';
const img = document.createElement('img');
img.src = fileUrl;
document.body.appendChild(img);
// OR for download:
window.open(fileUrl, '_blank');""",

            "delete": f"""// ✅ JavaScript — Delete File
fetch('{host}/api/storage/delete', {{
  method: 'POST',
  headers: {{ 'Content-Type': 'application/json' }},
  body: JSON.stringify({{
    api_key: '{api_key}',
    filename: 'your_filename_here'
  }})
}}).then(r => r.json()).then(console.log);""",
        },

        "python": {
            "upload": f"""# ✅ Python — File Upload
import requests
with open('myfile.jpg', 'rb') as f:
    res = requests.post('{host}/api/upload',
        data={{'api_key': '{api_key}'}},
        files={{'file': f}})
print(res.json())  # res.json()['url'] = file URL""",

            "load": f"""# ✅ Python — Load/Download File
import requests
file_url = 'YOUR_FILE_URL_HERE'
res = requests.get(file_url)
with open('downloaded_file.jpg', 'wb') as f:
    f.write(res.content)
print('Downloaded!')""",

            "delete": f"""# ✅ Python — Delete File
import requests
res = requests.post('{host}/api/storage/delete', json={{
    'api_key': '{api_key}',
    'filename': 'your_filename_here'
}})
print(res.json())""",
        },

        "kotlin": {
            "upload": f"""// ✅ Kotlin — File Upload
val client = OkHttpClient()
val file = File("myfile.jpg")
val body = MultipartBody.Builder().setType(MultipartBody.FORM)
    .addFormDataPart("api_key", "{api_key}")
    .addFormDataPart("file", file.name, file.asRequestBody("image/jpeg".toMediaType()))
    .build()
val request = Request.Builder().url("{host}/api/upload").post(body).build()
client.newCall(request).execute().use {{ println(it.body?.string()) }}""",

            "load": f"""// ✅ Kotlin — Download File
val client = OkHttpClient()
val request = Request.Builder().url("YOUR_FILE_URL_HERE").get().build()
client.newCall(request).execute().use {{
    File("downloaded_file.jpg").writeBytes(it.body!!.bytes())
    println("Downloaded!")
}}""",

            "delete": f"""// ✅ Kotlin — Delete File
val json = JSONObject()
json.put("api_key", "{api_key}")
json.put("filename", "your_filename_here")
val body = json.toString().toRequestBody("application/json".toMediaType())
val request = Request.Builder().url("{host}/api/storage/delete").post(body).build()
OkHttpClient().newCall(request).execute().use {{ println(it.body?.string()) }}""",
        },

        "dart": {
            "upload": f"""// ✅ Dart — File Upload
import 'package:http/http.dart' as http;
var request = http.MultipartRequest('POST', Uri.parse('{host}/api/upload'));
request.fields['api_key'] = '{api_key}';
request.files.add(await http.MultipartFile.fromPath('file', 'myfile.jpg'));
var res = await request.send();
print(await res.stream.bytesToString());""",

            "load": f"""// ✅ Dart — Download File
import 'package:http/http.dart' as http;
import 'dart:io';
final res = await http.get(Uri.parse('YOUR_FILE_URL_HERE'));
await File('downloaded_file.jpg').writeAsBytes(res.bodyBytes);
print('Downloaded!');""",

            "delete": f"""// ✅ Dart — Delete File
final res = await http.post(Uri.parse('{host}/api/storage/delete'),
  headers: {{'Content-Type': 'application/json'}},
  body: jsonEncode({{'api_key': '{api_key}', 'filename': 'your_filename_here'}}));
print(res.body);""",
        },

        "php": {
            "upload": f"""<?php // ✅ PHP — File Upload
$ch = curl_init('{host}/api/upload');
curl_setopt_array($ch, [
  CURLOPT_RETURNTRANSFER => true,
  CURLOPT_POST => true,
  CURLOPT_POSTFIELDS => [
    'api_key' => '{api_key}',
    'file' => new CURLFile('/path/to/myfile.jpg')
  ]
]);
echo curl_exec($ch);""",

            "load": f"""<?php // ✅ PHP — Download File
$url = 'YOUR_FILE_URL_HERE';
$content = file_get_contents($url);
file_put_contents('downloaded_file.jpg', $content);
echo 'Downloaded!';""",

            "delete": f"""<?php // ✅ PHP — Delete File
$ch = curl_init('{host}/api/storage/delete');
curl_setopt_array($ch, [CURLOPT_RETURNTRANSFER => true, CURLOPT_POST => true,
  CURLOPT_HTTPHEADER => ['Content-Type: application/json'],
  CURLOPT_POSTFIELDS => json_encode(['api_key' => '{api_key}', 'filename' => 'your_filename_here'])]);
echo curl_exec($ch);""",
        },

        "swift": {
            "upload": f"""// ✅ Swift — File Upload
let url = URL(string: "{host}/api/upload")!
var req = URLRequest(url: url)
req.httpMethod = "POST"
let boundary = UUID().uuidString
req.setValue("multipart/form-data; boundary=\\(boundary)", forHTTPHeaderField: "Content-Type")
var body = Data()
body.append("--\\(boundary)\\r\\nContent-Disposition: form-data; name=\\"api_key\\"\\r\\n\\r\\n{api_key}\\r\\n".data(using: .utf8)!)
// Add file data similarly
req.httpBody = body
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()""",

            "load": f"""// ✅ Swift — Download File
let fileUrl = URL(string: "YOUR_FILE_URL_HERE")!
URLSession.shared.dataTask(with: fileUrl) {{ data, _, _ in
    if let data = data {{
        try? data.write(to: URL(fileURLWithPath: "downloaded_file.jpg"))
        print("Downloaded!")
    }}
}}.resume()""",

            "delete": f"""// ✅ Swift — Delete File
var req = URLRequest(url: URL(string: "{host}/api/storage/delete")!)
req.httpMethod = "POST"; req.setValue("application/json", forHTTPHeaderField: "Content-Type")
req.httpBody = try? JSONSerialization.data(withJSONObject: ["api_key": "{api_key}", "filename": "your_filename_here"])
URLSession.shared.dataTask(with: req) {{ d,_,_ in print(String(data: d!, encoding: .utf8)!) }}.resume()""",
        },

        "java": {
            "upload": f"""// ✅ Java — File Upload
OkHttpClient client = new OkHttpClient();
File file = new File("myfile.jpg");
RequestBody body = new MultipartBody.Builder().setType(MultipartBody.FORM)
    .addFormDataPart("api_key", "{api_key}")
    .addFormDataPart("file", file.getName(), RequestBody.create(file, MediaType.parse("image/jpeg")))
    .build();
Request request = new Request.Builder().url("{host}/api/upload").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}""",

            "load": f"""// ✅ Java — Download File
OkHttpClient client = new OkHttpClient();
Request request = new Request.Builder().url("YOUR_FILE_URL_HERE").get().build();
try (Response r = client.newCall(request).execute()) {{
    Files.write(Paths.get("downloaded_file.jpg"), r.body().bytes());
    System.out.println("Downloaded!");
}}""",

            "delete": f"""// ✅ Java — Delete File
OkHttpClient client = new OkHttpClient();
String json = "{{\\"api_key\\":\\"{api_key}\\",\\"filename\\":\\"your_filename_here\\"}}";
RequestBody body = RequestBody.create(json, MediaType.parse("application/json"));
Request request = new Request.Builder().url("{host}/api/storage/delete").post(body).build();
try (Response r = client.newCall(request).execute()) {{ System.out.println(r.body().string()); }}""",
        },

        "c#": {
            "upload": f"""// ✅ C# — File Upload
using var client = new HttpClient();
using var form = new MultipartFormDataContent();
form.Add(new StringContent("{api_key}"), "api_key");
form.Add(new StreamContent(File.OpenRead("myfile.jpg")), "file", "myfile.jpg");
var res = await client.PostAsync("{host}/api/upload", form);
Console.WriteLine(await res.Content.ReadAsStringAsync());""",

            "load": f"""// ✅ C# — Download File
using var client = new HttpClient();
var bytes = await client.GetByteArrayAsync("YOUR_FILE_URL_HERE");
await File.WriteAllBytesAsync("downloaded_file.jpg", bytes);
Console.WriteLine("Downloaded!");""",

            "delete": f"""// ✅ C# — Delete File
using var client = new HttpClient();
var payload = new {{ api_key = "{api_key}", filename = "your_filename_here" }};
var res = await client.PostAsync("{host}/api/storage/delete",
    new StringContent(JsonSerializer.Serialize(payload), Encoding.UTF8, "application/json"));
Console.WriteLine(await res.Content.ReadAsStringAsync());""",
        },
    }

    return codes.get(lang, {}).get(op, f"// Code for {lang} - {op} not available yet.")


# =============================================================================
# FLASK ROUTES (Secure)
# =============================================================================

@app.route("/", methods=["GET"])
def index():
    return jsonify({"status": "ok", "service": "CloudNest API Framework", "security": "Active"}), 200

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy"}), 200

@app.route("/api/otp/send", methods=["POST"])
def api_otp_send():
    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "").strip()
    
    if not api_key:
        return jsonify({"status": "error", "message": "Method not allowed"}), 405

    email = data.get("email", "").strip()

    dev_email, dev_info = get_user_by_api_key(api_key)
    if not dev_email:
        return jsonify({"status": "error", "message": "Invalid API Key."}), 401

    if not email:
        return jsonify({"status": "error", "message": "Email is required."}), 400

    allowed, user_info = consume_feature(dev_email, "otp_sends")
    if not allowed and not user_info.get("premium"):
        used, limit, pct = feature_limit_status(user_info, "otp_sends")
        return jsonify({"status": "error", "message": "Free OTP limit reached.", "usage": {"used": used, "limit": limit, "percent": pct}}), 429

    otp_code = str(random.randint(100000, 999999))
    if send_user_otp_email(email, otp_code):
        key = f"{api_key}_{email}"
        DEV_OTPS[key] = {
            "otp": otp_code,
            "expires": time.time() + 300 # 5 minutes
        }
        return jsonify({"status": "success", "message": "OTP sent successfully."})
    return jsonify({"status": "error", "message": "Failed to send email."}), 500

@app.route("/api/otp/verify", methods=["POST"])
def api_otp_verify():
    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "").strip()

    if not api_key:
        return jsonify({"status": "error", "message": "Method not allowed"}), 405

    email = data.get("email", "").strip()
    otp = str(data.get("otp", "")).strip()

    dev_email, _ = get_user_by_api_key(api_key)
    if not dev_email:
        return jsonify({"status": "error", "message": "Invalid API Key."}), 401

    key = f"{api_key}_{email}"
    record = DEV_OTPS.get(key)
    
    if not record: 
        return jsonify({"status": "error", "message": "OTP not found or not sent."}), 404
    if time.time() > record["expires"]:
        del DEV_OTPS[key]
        return jsonify({"status": "error", "message": "OTP expired."}), 400
    if record["otp"] == otp:
        del DEV_OTPS[key]
        return jsonify({"status": "success", "message": "OTP verified successfully."})
    
    return jsonify({"status": "error", "message": "Invalid OTP."}), 400

@app.route("/api/db", methods=["POST"])
def api_db():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()

    if not api_key:
        return jsonify({"status": "error", "message": "Method not allowed"}), 405

    action = (data.get("action") or "").strip().lower()
    key = str(data.get("key", "default"))
    payload = data.get("data", "")

    dev_email, dev_info = get_user_by_api_key(api_key)
    if not dev_email:
        return jsonify({"status": "error", "message": "Invalid API Key."}), 401

    allowed, user_info = consume_feature(dev_email, "db_ops")
    if not allowed and not user_info.get("premium"):
        used, limit, pct = feature_limit_status(user_info, "db_ops")
        return jsonify({"status": "error", "message": "Free database limit reached.", "usage": {"used": used, "limit": limit, "percent": pct}}), 429

    db_data = load_dev_db(dev_info)

    if action == "save":
        db_data[key] = payload
        save_dev_db(dev_info, db_data)
        return jsonify({"status": "success", "message": "Data saved!"})

    if action == "load":
        return jsonify({"status": "success", "data": db_data.get(key, "")})

    if action == "delete":
        if key in db_data:
            del db_data[key]
            save_dev_db(dev_info, db_data)
            return jsonify({"status": "success", "message": f"Key '{key}' deleted."})
        return jsonify({"status": "error", "message": "Key not found."}), 404

    return jsonify({"status": "error", "message": "Invalid action."}), 400

@app.route("/api/auth", methods=["POST"])
def api_auth():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()

    if not api_key:
        return jsonify({"status": "error", "message": "Method not allowed"}), 405

    action = (data.get("action") or "").strip().lower()
    username = str(data.get("username") or "").strip()
    password = str(data.get("password") or "")
    new_password = str(data.get("new_password") or data.get("password_new") or "").strip()

    dev_email, dev_info = get_user_by_api_key(api_key)
    if not dev_email:
        return jsonify({"status": "error", "message": "Invalid API Key."}), 401

    allowed, user_info = consume_feature(dev_email, "auth_ops")
    if not allowed and not user_info.get("premium"):
        used, limit, pct = feature_limit_status(user_info, "auth_ops")
        return jsonify({"status": "error", "message": "Free authentication limit reached.", "usage": {"used": used, "limit": limit, "percent": pct}}), 429

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

    if action == "list":
        users_list = [{"username": u, "created_at": d.get("created_at", "")} for u, d in auth_data.items()]
        return jsonify({"status": "success", "users": users_list, "count": len(users_list)})

    if action == "delete":
        if not username:
            return jsonify({"status": "error", "message": "username is required."}), 400
        if username not in auth_data:
            return jsonify({"status": "error", "message": "User not found."}), 404
        del auth_data[username]
        save_dev_auth(dev_info, auth_data)
        return jsonify({"status": "success", "message": f"User '{username}' deleted."})

    if action == "update_password":
        if not username or not new_password:
            return jsonify({"status": "error", "message": "username and new_password are required."}), 400
        if username not in auth_data:
            return jsonify({"status": "error", "message": "User not found."}), 404

        auth_data[username]["password"] = new_password
        auth_data[username]["updated_at"] = now_iso()
        save_dev_auth(dev_info, auth_data)
        return jsonify({"status": "success", "message": "Password updated successfully!"})

    return jsonify({"status": "error", "message": "Invalid action."}), 400

@app.route("/api/upload", methods=["POST"])
def upload_file():
    api_key = (request.form.get("api_key") or "").strip()
    
    if not api_key:
        return jsonify({"status": "error", "message": "Method not allowed"}), 405

    dev_email, dev_info = get_user_by_api_key(api_key)
    if not dev_email:
        return jsonify({"status": "error", "message": "Invalid API Key."}), 401

    allowed, user_info = consume_feature(dev_email, "upload_ops")
    if not allowed and not user_info.get("premium"):
        used, limit, pct = feature_limit_status(user_info, "upload_ops")
        return jsonify({"status": "error", "message": "Free upload limit reached.", "usage": {"used": used, "limit": limit, "percent": pct}}), 429

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

@app.route("/api/storage/delete", methods=["POST"])
def delete_storage_file():
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()

    if not api_key:
        return jsonify({"status": "error", "message": "Method not allowed"}), 405

    filename = (data.get("filename") or "").strip()

    dev_email, dev_info = get_user_by_api_key(api_key)
    if not dev_email:
        return jsonify({"status": "error", "message": "Invalid API Key."}), 401

    if not filename:
        return jsonify({"status": "error", "message": "filename is required"}), 400

    safe_prefix = dev_info["api_key"] + "_"
    if not filename.startswith(safe_prefix) and dev_email != "admin@cloudnest":
        return jsonify({"status": "error", "message": "Access denied."}), 403

    filepath = os.path.join(UPLOAD_FOLDER, secure_filename(filename))
    if os.path.exists(filepath):
        os.remove(filepath)
        return jsonify({"status": "success", "message": f"File '{filename}' deleted."})
    return jsonify({"status": "error", "message": "File not found."}), 404

@app.route("/uploads/<filename>")
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


# =============================================================================
# TELEGRAM BOT LOGIC
# =============================================================================

@bot.message_handler(commands=["start", "restart"])
def command_start(message):
    chat_id = str(message.chat.id)
    TEMP_AUTH_STATE.pop(chat_id, None) # reset any pending auth states
    email, user_info = get_logged_in_user(chat_id)
    if email:
        text = f"🎉 Welcome back to CloudNest!\n\nYour API Key:\n<code>{user_info['api_key']}</code>\n\n(Tap the key to copy it)"
        bot.send_message(chat_id, text, reply_markup=main_keyboard(chat_id), parse_mode="HTML")
    else:
        bot.send_message(chat_id, "Welcome to CloudNest Database & Backend Bot!\n\nPlease Register or Login to continue.", reply_markup=auth_welcome_keyboard())

def show_database(chat_id: str, email: str):
    user_info = load_users().get(email)
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

def show_auth_users(chat_id: str, email: str):
    user_info = load_users().get(email)
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

def show_storage(chat_id: str, email: str):
    user_info = load_users().get(email)
    api_key = user_info["api_key"]
    host = get_public_base_url()

    all_files = []
    if os.path.exists(UPLOAD_FOLDER):
        for fname in os.listdir(UPLOAD_FOLDER):
            if fname.startswith(api_key + "_"):
                fpath = os.path.join(UPLOAD_FOLDER, fname)
                stat = os.stat(fpath)
                all_files.append({
                    "name": fname,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                    "url": f"{host}/uploads/{fname}",
                })

    if not all_files:
        bot.send_message(
            chat_id,
            "📁 Storage is empty. No files uploaded yet.\n\n" + usage_summary(user_info),
            reply_markup=main_keyboard(chat_id),
        )
        return

    all_files.sort(key=lambda x: x["modified"], reverse=True)
    recent = all_files[:5]

    lines = ["📁 Recent 5 Files:\n"]
    markup = types.InlineKeyboardMarkup()
    for i, f in enumerate(recent, 1):
        size_kb = f["size"] / 1024
        size_str = f"{size_kb:.1f} KB" if size_kb < 1024 else f"{size_kb/1024:.2f} MB"
        parts = f["name"].split("_", 2)
        display_name = parts[2] if len(parts) >= 3 else f["name"]
        lines.append(f"{i}. {display_name} ({size_str})")
        markup.add(
            types.InlineKeyboardButton(f"💾 Save File {i}", url=f["url"]),
            types.InlineKeyboardButton(f"🗑 Delete File {i}", callback_data=f"storage_del_{f['name']}"),
        )

    lines.append("")
    lines.append(f"Total files: {len(all_files)}")
    lines.append(usage_summary(user_info))
    bot.send_message(chat_id, "\n".join(lines), reply_markup=markup)

def send_code_message(chat_id: str, title: str, code: str, lang_label: str):
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("📋 Copy Code", switch_inline_query=code))
    header = f"💻 {title} — {lang_label}:\n\n"
    full_msg = header + "```\n" + code + "\n```"

    try:
        bot.send_message(chat_id, full_msg, parse_mode="Markdown", reply_markup=markup)
    except Exception:
        bot.send_message(chat_id, header + code, reply_markup=markup)

@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_messages(message):
    chat_id = str(message.chat.id)
    text = (message.text or "").strip()

    if not text:
        return

    # Check for ongoing Auth Flow
    auth_state = TEMP_AUTH_STATE.get(chat_id)
    if auth_state:
        action = auth_state["action"]
        state = auth_state["state"]

        # --- REGISTRATION ---
        if action == "register":
            if state == "await_email":
                if not text.endswith("@gmail.com"):
                    bot.send_message(chat_id, "❌ Error: Only @gmail.com is allowed. Please enter a valid Gmail address:")
                    return
                users = load_users()
                if text in users:
                    bot.send_message(chat_id, "❌ This email is already registered. Please login.", reply_markup=auth_welcome_keyboard())
                    del TEMP_AUTH_STATE[chat_id]
                    return
                
                bot.send_message(chat_id, "Sending Verification OTP to your email... Please wait.")
                otp = str(random.randint(100000, 999999))
                if send_otp_email(text, otp):
                    TEMP_AUTH_STATE[chat_id]["email"] = text
                    TEMP_AUTH_STATE[chat_id]["otp"] = otp
                    TEMP_AUTH_STATE[chat_id]["state"] = "await_otp"
                    bot.send_message(chat_id, "✅ OTP Sent! Please check your email and enter the 6-digit code:")
                else:
                    bot.send_message(chat_id, "❌ Failed to send OTP email. Please try again later.", reply_markup=auth_welcome_keyboard())
                    del TEMP_AUTH_STATE[chat_id]
                return

            if state == "await_otp":
                if text != auth_state["otp"]:
                    bot.send_message(chat_id, "❌ Incorrect OTP. Try again:")
                    return
                TEMP_AUTH_STATE[chat_id]["state"] = "await_pass"
                bot.send_message(chat_id, "✅ Email Verified!\n\nPlease set a password for your CloudNest account:")
                return

            if state == "await_pass":
                email = auth_state["email"]
                password = text
                
                users = load_users()
                api_key = "cn_" + uuid.uuid4().hex
                users[email] = {
                    "email": email,
                    "password": password,
                    "api_key": api_key,
                    "premium": False,
                    "premium_expires_at": "",
                    "created_at": now_iso(),
                    "usage": {}
                }
                save_users(users)

                sessions = load_sessions()
                sessions[chat_id] = email
                save_sessions(sessions)
                
                del TEMP_AUTH_STATE[chat_id]
                msg = f"🎉 Account Registered Successfully!\n\nYour API Key:\n<code>{api_key}</code>\n\n(Tap the key to copy)"
                bot.send_message(chat_id, msg, reply_markup=main_keyboard(chat_id), parse_mode="HTML")
                return

        # --- LOGIN ---
        if action == "login":
            if state == "await_email":
                users = load_users()
                if text not in users:
                    bot.send_message(chat_id, "❌ Email not found. Please register first.", reply_markup=auth_welcome_keyboard())
                    del TEMP_AUTH_STATE[chat_id]
                    return
                
                bot.send_message(chat_id, "Sending Login OTP... Please wait.")
                otp = str(random.randint(100000, 999999))
                if send_otp_email(text, otp):
                    TEMP_AUTH_STATE[chat_id]["email"] = text
                    TEMP_AUTH_STATE[chat_id]["otp"] = otp
                    TEMP_AUTH_STATE[chat_id]["state"] = "await_otp"
                    bot.send_message(chat_id, "✅ OTP Sent! Please check your email and enter the 6-digit code:")
                else:
                    bot.send_message(chat_id, "❌ Failed to send OTP.", reply_markup=auth_welcome_keyboard())
                    del TEMP_AUTH_STATE[chat_id]
                return

            if state == "await_otp":
                if text != auth_state["otp"]:
                    bot.send_message(chat_id, "❌ Incorrect OTP. Try again:")
                    return
                TEMP_AUTH_STATE[chat_id]["state"] = "await_pass"
                bot.send_message(chat_id, "✅ OTP Verified!\n\nEnter your account password:")
                return

            if state == "await_pass":
                email = auth_state["email"]
                users = load_users()
                if users[email]["password"] != text:
                    bot.send_message(chat_id, "❌ Incorrect Password. Login failed.", reply_markup=auth_welcome_keyboard())
                    del TEMP_AUTH_STATE[chat_id]
                    return

                sessions = load_sessions()
                sessions[chat_id] = email
                save_sessions(sessions)

                del TEMP_AUTH_STATE[chat_id]
                api_key = users[email]["api_key"]
                msg = f"✅ Logged in successfully!\n\nYour API Key:\n<code>{api_key}</code>\n\n(Tap the key to copy)"
                bot.send_message(chat_id, msg, reply_markup=main_keyboard(chat_id), parse_mode="HTML")
                return

    # Check for Login commands triggers
    if text == "Register":
        TEMP_AUTH_STATE[chat_id] = {"action": "register", "state": "await_email"}
        bot.send_message(chat_id, "Enter your Gmail address (@gmail.com):", reply_markup=types.ReplyKeyboardRemove())
        return

    if text == "Login":
        TEMP_AUTH_STATE[chat_id] = {"action": "login", "state": "await_email"}
        bot.send_message(chat_id, "Enter your registered Gmail address:", reply_markup=types.ReplyKeyboardRemove())
        return

    # Ensure user is logged in for normal operations
    email, user_info = get_logged_in_user(chat_id)
    if not email:
        bot.send_message(chat_id, "You must login first.", reply_markup=auth_welcome_keyboard())
        return

    if text == "Logout":
        sessions = load_sessions()
        if chat_id in sessions:
            del sessions[chat_id]
            save_sessions(sessions)
        bot.send_message(chat_id, "✅ Logged out successfully.", reply_markup=auth_welcome_keyboard())
        return

    # Normal user flows
    pending = get_pending_action(chat_id)
    if pending == "redeem_premium":
        pop_pending_action(chat_id)
        
        codes = load_premium_codes()
        code = text
        if code not in codes:
            bot.send_message(chat_id, "❌ Invalid premium code.", reply_markup=main_keyboard(chat_id))
            return
        if codes[code].get("used"):
            bot.send_message(chat_id, "❌ This premium code was already used.", reply_markup=main_keyboard(chat_id))
            return

        users = load_users()
        days = codes[code].get("duration_days", 30)
        
        users[email]["premium"] = True
        exp_date = datetime.now(timezone.utc) + timedelta(days=days)
        users[email]["premium_expires_at"] = exp_date.isoformat()
        save_users(users)

        codes[code]["used"] = True
        codes[code]["used_by"] = email
        codes[code]["used_at"] = now_iso()
        save_premium_codes(codes)

        bot.send_message(chat_id, f"✅ Premium Activated for {days} days!\nExpires at: {exp_date.strftime('%Y-%m-%d')}", reply_markup=main_keyboard(chat_id))
        return

    if pending == "edit_password":
        pop_pending_action(chat_id)
        if "|" in text:
            username, new_password = [x.strip() for x in text.split("|", 1)]
        elif "," in text:
            username, new_password = [x.strip() for x in text.split(",", 1)]
        else:
            bot.send_message(chat_id, "Wrong format. Use: username|new_password", reply_markup=main_keyboard(chat_id))
            return

        allowed, _ = consume_feature(email, "password_edits")
        if not allowed and not user_info.get("premium"):
            used, limit, pct = feature_limit_status(user_info, "password_edits")
            bot.send_message(chat_id, f"Free password-edit limit reached.\nUsed: {used}/{limit} ({pct}%)", reply_markup=main_keyboard(chat_id))
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

    if text == "Database":
        show_database(chat_id, email)
        return

    if text == "Authentication":
        allowed, _ = consume_feature(email, "auth_ops")
        if not allowed and not user_info.get("premium"):
            used, limit, pct = feature_limit_status(user_info, "auth_ops")
            bot.send_message(chat_id, f"Free authentication limit reached.\nUsed: {used}/{limit} ({pct}%)", reply_markup=main_keyboard(chat_id))
            return
        bot.send_message(chat_id, "Authentication panel:", reply_markup=auth_inline_keyboard())
        return

    if text == "Storage":
        show_storage(chat_id, email)
        return

    if text == "Premium":
        premium = bool(user_info.get("premium"))
        lines = []
        if premium:
            lines.append("⭐ Premium status: ACTIVE")
            lines.append(f"Expires at: {user_info.get('premium_expires_at')[:10]}")
            lines.append("All features are unlimited.")
        else:
            lines.append("⭐ Premium status: FREE")
            lines.append("Redeem a code to activate Premium.")
        lines.append("")
        lines.append("Usage:")
        lines.append(usage_summary(user_info))
        bot.send_message(chat_id, "\n".join(lines), reply_markup=premium_inline_keyboard(is_admin(chat_id)))
        return

    if text == "Project Settings":
        api_key = user_info["api_key"]
        text_msg = (
            f"⚙️ Project Settings\n\n"
            f"Your API Key:\n<code>{api_key}</code>\n\n(Tap the key above to copy)\n\n"
            f"Usage:\n{usage_summary(user_info)}\n\n"
            f"Choose a section to get code:"
        )
        bot.send_message(chat_id, text_msg, reply_markup=project_inline_keyboard(), parse_mode="HTML")
        return

    if text == "Create premium":
        if not is_admin(chat_id):
            bot.send_message(chat_id, "You are not allowed.")
            return
        bot.send_message(chat_id, "Select Premium Duration:", reply_markup=premium_duration_keyboard())
        return

    bot.send_message(chat_id, "Use the menu buttons below.", reply_markup=main_keyboard(chat_id))


@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    chat_id = str(call.message.chat.id)
    data = call.data

    email, user_info = get_logged_in_user(chat_id)
    if not email and not data.startswith("premgen_"):
        bot.answer_callback_query(call.id, "Please login first.")
        return

    # ---- Auth panel callbacks ----
    if data == "show_auth":
        bot.answer_callback_query(call.id)
        show_auth_users(chat_id, email)
        return

    if data == "edit_password":
        bot.answer_callback_query(call.id)
        set_pending_action(chat_id, "edit_password")
        bot.send_message(chat_id, "Send in this format:\nusername|new_password")
        return

    # ---- Premium callbacks ----
    if data == "premium_redeem":
        bot.answer_callback_query(call.id)
        set_pending_action(chat_id, "redeem_premium")
        bot.send_message(chat_id, "Send your premium redeem code now:")
        return

    if data.startswith("premgen_"):
        bot.answer_callback_query(call.id)
        if not is_admin(chat_id): return
        days = int(data.split("_")[1])
        code = "PREM-" + uuid.uuid4().hex[:4].upper() + "-" + uuid.uuid4().hex[:4].upper() + "-" + uuid.uuid4().hex[:4].upper()
        
        codes = load_premium_codes()
        codes[code] = {
            "used": False,
            "duration_days": days,
            "created_by": str(chat_id),
            "created_at": now_iso()
        }
        save_premium_codes(codes)
        
        msg = f"✅ Premium code created ({days} Days):\n\n`{code}`\n\n*(Tap the code to copy)*"
        bot.send_message(chat_id, msg, parse_mode="Markdown")
        return

    # ---- Storage delete callback ----
    if data.startswith("storage_del_"):
        bot.answer_callback_query(call.id)
        filename = data[len("storage_del_"):]
        safe_prefix = user_info["api_key"] + "_"
        if not filename.startswith(safe_prefix):
            bot.send_message(chat_id, "❌ Access denied.")
            return
        filepath = os.path.join(UPLOAD_FOLDER, secure_filename(filename))
        if os.path.exists(filepath):
            os.remove(filepath)
            parts = filename.split("_", 2)
            display_name = parts[2] if len(parts) >= 3 else filename
            bot.send_message(chat_id, f"🗑 Deleted: {display_name}", reply_markup=main_keyboard(chat_id))
        else:
            bot.send_message(chat_id, "File not found.", reply_markup=main_keyboard(chat_id))
        return

    # ---- Project Settings: section selection ----
    if data == "proj_db":
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "🗄 Database — Choose Language:", reply_markup=lang_keyboard("db"))
        return

    if data == "proj_auth":
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "👥 Authentication — Choose Language:", reply_markup=lang_keyboard("auth"))
        return

    if data == "proj_storage":
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "📁 Storage — Choose Language:", reply_markup=lang_keyboard("storage"))
        return

    if data == "proj_otp":
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "📧 OTP System — Choose Language:", reply_markup=lang_keyboard("otp"))
        return

    # ---- Language selected ----
    if data.startswith("lang_db_"):
        lang = data[len("lang_db_"):]
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"🗄 DB — {lang.capitalize()} — Choose Operation:", reply_markup=db_ops_keyboard(lang))
        return

    if data.startswith("lang_auth_"):
        lang = data[len("lang_auth_"):]
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"👥 Auth — {lang.capitalize()} — Choose Operation:", reply_markup=auth_ops_keyboard(lang))
        return

    if data.startswith("lang_storage_"):
        lang = data[len("lang_storage_"):]
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"📁 Storage — {lang.capitalize()} — Choose Operation:", reply_markup=storage_ops_keyboard(lang))
        return

    if data.startswith("lang_otp_"):
        lang = data[len("lang_otp_"):]
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"📧 OTP — {lang.capitalize()} — Choose Operation:", reply_markup=otp_ops_keyboard(lang))
        return

    # ---- Operation Code Handlers ----
    if data.startswith("dbop_"):
        parts = data[len("dbop_"):].split("_", 1)
        if len(parts) == 2:
            lang, op = parts
            bot.answer_callback_query(call.id)
            code = get_db_code(lang, op, user_info["api_key"], get_public_base_url())
            send_code_message(chat_id, "Database API Code", code, lang.capitalize())
        return

    if data.startswith("authop_"):
        parts = data[len("authop_"):].split("_", 1)
        if len(parts) == 2:
            lang, op = parts
            bot.answer_callback_query(call.id)
            code = get_auth_code(lang, op, user_info["api_key"], get_public_base_url())
            send_code_message(chat_id, "Auth API Code", code, lang.capitalize())
        return

    if data.startswith("storop_"):
        parts = data[len("storop_"):].split("_", 1)
        if len(parts) == 2:
            lang, op = parts
            bot.answer_callback_query(call.id)
            code = get_storage_code(lang, op, user_info["api_key"], get_public_base_url())
            send_code_message(chat_id, "Storage API Code", code, lang.capitalize())
        return

    if data.startswith("otpop_"):
        parts = data[len("otpop_"):].split("_", 1)
        if len(parts) == 2:
            lang, op = parts
            bot.answer_callback_query(call.id)
            code = get_otp_code(lang, op, user_info["api_key"], get_public_base_url())
            send_code_message(chat_id, "OTP System API Code", code, lang.capitalize())
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
    print("CloudNest Secure Backend starting...")
    print(f"Port: {PORT}")
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True)
