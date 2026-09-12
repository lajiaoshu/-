from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
PUBLIC_DIR = ROOT / "public"
DATA_DIR = Path(os.environ.get("DATA_DIR", str(ROOT / "data"))).resolve()
DB_PATH = DATA_DIR / "flytrack.db"
SESSION_COOKIE = "flytrack_session"
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "30"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "1").lower() not in {"0", "false", "no"}
MAX_BODY_SIZE = 20 * 1024 * 1024
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
DB_LOCK = threading.RLock()
LOGIN_ATTEMPTS: dict[str, deque[float]] = defaultdict(deque)

DATA_DIR.mkdir(parents=True, exist_ok=True)
DB = sqlite3.connect(DB_PATH, check_same_thread=False)
DB.row_factory = sqlite3.Row
DB.execute("PRAGMA journal_mode=WAL")
DB.execute("PRAGMA foreign_keys=ON")
DB.execute("PRAGMA busy_timeout=5000")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return f"scrypt$16384$8$1${b64encode(salt)}${b64encode(derived)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, n, r, p, salt_text, hash_text = stored.split("$", 5)
        if algorithm != "scrypt":
            return False
        expected = b64decode(hash_text)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=b64decode(salt_text), n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError, KeyError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def init_db() -> None:
    with DB_LOCK, DB:
        DB.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'user')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS app_data (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                revision INTEGER NOT NULL DEFAULT 0,
                data_json TEXT,
                updated_at TEXT,
                updated_by INTEGER REFERENCES users(id)
            );
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER REFERENCES users(id),
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                details TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
            INSERT OR IGNORE INTO app_data (id, revision, data_json) VALUES (1, 0, NULL);
            """
        )
    bootstrap_admin()


def bootstrap_admin() -> None:
    username = os.environ.get("ADMIN_USERNAME", "admin").strip().lower()
    password = os.environ.get("ADMIN_PASSWORD", "")
    display_name = os.environ.get("ADMIN_DISPLAY_NAME", "实验室管理员").strip() or "实验室管理员"
    reset_requested = os.environ.get("RESET_ADMIN_PASSWORD", "0").lower() in {"1", "true", "yes"}
    with DB_LOCK:
        row = DB.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None and not password:
            print("[FlyTrack Cloud] 尚未创建管理员：请设置 ADMIN_PASSWORD 环境变量后重启。")
            return
        if row is None:
            if len(password) < 8:
                print("[FlyTrack Cloud] ADMIN_PASSWORD 至少需要 8 位。")
                return
            now = utc_now()
            with DB:
                DB.execute(
                    "INSERT INTO users (username, display_name, password_hash, role, active, created_at, updated_at) VALUES (?, ?, ?, 'admin', 1, ?, ?)",
                    (username, display_name, hash_password(password), now, now),
                )
            print(f"[FlyTrack Cloud] 已创建管理员账号：{username}")
            return
        if reset_requested and password:
            if len(password) < 8:
                print("[FlyTrack Cloud] ADMIN_PASSWORD 至少需要 8 位，未重置管理员密码。")
                return
            with DB:
                DB.execute("UPDATE users SET password_hash = ?, role = 'admin', active = 1, updated_at = ? WHERE id = ?", (hash_password(password), utc_now(), row["id"]))
                DB.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
            print(f"[FlyTrack Cloud] 已重置管理员账号密码：{username}")


def audit(actor_id: int | None, action: str, target: str = "", details: str = "") -> None:
    with DB_LOCK, DB:
        DB.execute("INSERT INTO audit_logs (actor_id, action, target, details, created_at) VALUES (?, ?, ?, ?, ?)", (actor_id, action, target, details, utc_now()))


class FlyTrackHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False
    daemon_threads = True


class Handler(SimpleHTTPRequestHandler):
    server_version = "FlyTrackCloud/1.0"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, directory=str(PUBLIC_DIR), **kwargs)

    def end_headers(self) -> None:
        path = urlparse(self.path).path
        if path.startswith("/api/") or path == "/healthz":
            self.send_header("Cache-Control", "no-store")
        elif path.endswith((".html", ".js", ".css", ".webmanifest")):
            self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-src 'self' blob:")
        super().end_headers()

    def list_directory(self, path: str):
        self.send_error(HTTPStatus.NOT_FOUND, "Not found")
        return None

    def send_json(self, payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_api_error(self, status: int, message: str) -> None:
        self.send_json({"ok": False, "error": message}, status)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_SIZE:
            raise ValueError("请求内容过大")
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("请求内容必须是 JSON 对象")
        return payload

    def cookies(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for item in self.headers.get("Cookie", "").split(";"):
            if "=" in item:
                key, value = item.split("=", 1)
                result[key.strip()] = value.strip()
        return result

    def current_user(self) -> sqlite3.Row | None:
        token = self.cookies().get(SESSION_COOKIE)
        if not token:
            return None
        with DB_LOCK:
            return DB.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1",
                (token_hash(token), utc_now()),
            ).fetchone()

    def require_user(self, role: str | None = None) -> sqlite3.Row | None:
        user = self.current_user()
        if user is None:
            self.send_api_error(HTTPStatus.UNAUTHORIZED, "请先登录")
            return None
        if role and user["role"] != role:
            self.send_api_error(HTTPStatus.FORBIDDEN, "需要管理员权限")
            return None
        return user

    def public_user(self, user: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return {"id": int(user["id"]), "username": user["username"], "displayName": user["display_name"], "role": user["role"], "active": bool(user["active"])}

    def set_session_cookie(self, token: str) -> str:
        secure = "; Secure" if COOKIE_SECURE else ""
        return f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={SESSION_DAYS * 86400}{secure}"

    def create_session(self, user_id: int) -> str:
        token = secrets.token_urlsafe(40)
        created = datetime.now(timezone.utc)
        expires = created + timedelta(days=SESSION_DAYS)
        with DB_LOCK, DB:
            DB.execute("DELETE FROM sessions WHERE expires_at <= ?", (created.isoformat(timespec="seconds"),))
            DB.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)", (token_hash(token), user_id, created.isoformat(timespec="seconds"), expires.isoformat(timespec="seconds")))
        return token

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self.send_json({"ok": True, "service": "FlyTrack Cloud", "time": utc_now()})
            return
        if path == "/api/session":
            user = self.require_user()
            if user is not None:
                self.send_json({"ok": True, "user": self.public_user(user)})
            return
        if path == "/api/data":
            if self.require_user() is None:
                return
            with DB_LOCK:
                row = DB.execute("SELECT revision, data_json, updated_at FROM app_data WHERE id = 1").fetchone()
            data = json.loads(row["data_json"]) if row and row["data_json"] else None
            self.send_json({"ok": True, "revision": int(row["revision"]), "data": data, "savedAt": row["updated_at"]})
            return
        if path == "/api/admin/users":
            if self.require_user("admin") is None:
                return
            with DB_LOCK:
                rows = DB.execute("SELECT id, username, display_name, role, active, created_at FROM users ORDER BY created_at, id").fetchall()
            self.send_json({"ok": True, "users": [self.public_user(row) | {"createdAt": row["created_at"]} for row in rows]})
            return
        super().do_GET()

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_api_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if path == "/api/login":
            self.handle_login(payload)
            return
        if path == "/api/logout":
            token = self.cookies().get(SESSION_COOKIE)
            if token:
                with DB_LOCK, DB:
                    DB.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))
            self.send_json({"ok": True}, headers={"Set-Cookie": f"{SESSION_COOKIE}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"})
            return
        if path == "/api/data":
            user = self.require_user()
            if user is None:
                return
            self.handle_save_data(payload, user)
            return
        if path == "/api/admin/users":
            admin = self.require_user("admin")
            if admin is None:
                return
            self.handle_create_user(payload, admin)
            return
        match = re.fullmatch(r"/api/admin/users/(\d+)/password", path)
        if match:
            admin = self.require_user("admin")
            if admin is None:
                return
            self.handle_reset_password(int(match.group(1)), payload, admin)
            return
        self.send_api_error(HTTPStatus.NOT_FOUND, "接口不存在")

    def do_PATCH(self) -> None:
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/admin/users/(\d+)", path)
        if not match:
            self.send_api_error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        admin = self.require_user("admin")
        if admin is None:
            return
        try:
            payload = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.send_api_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        self.handle_update_user(int(match.group(1)), payload, admin)

    def handle_login(self, payload: dict[str, Any]) -> None:
        address = self.client_address[0]
        now = time.time()
        attempts = LOGIN_ATTEMPTS[address]
        while attempts and now - attempts[0] > 900:
            attempts.popleft()
        if len(attempts) >= 10:
            self.send_api_error(HTTPStatus.TOO_MANY_REQUESTS, "登录尝试过多，请稍后重试")
            return
        username = str(payload.get("username") or "").strip().lower()
        password = str(payload.get("password") or "")
        with DB_LOCK:
            user = DB.execute("SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if user is None or not bool(user["active"]) or not verify_password(password, user["password_hash"]):
            attempts.append(now)
            self.send_api_error(HTTPStatus.UNAUTHORIZED, "用户名或密码不正确")
            return
        attempts.clear()
        token = self.create_session(int(user["id"]))
        audit(int(user["id"]), "login", user["username"])
        self.send_json({"ok": True, "user": self.public_user(user)}, headers={"Set-Cookie": self.set_session_cookie(token)})

    def handle_save_data(self, payload: dict[str, Any], user: sqlite3.Row) -> None:
        incoming = payload.get("data")
        if not isinstance(incoming, dict) or not isinstance(incoming.get("bottles"), list):
            self.send_api_error(HTTPStatus.BAD_REQUEST, "同步数据缺少 bottles 数组")
            return
        base_revision = int(payload.get("revision") or 0)
        with DB_LOCK, DB:
            row = DB.execute("SELECT revision, data_json, updated_at FROM app_data WHERE id = 1").fetchone()
            server_revision = int(row["revision"])
            accepted = base_revision == server_revision or not row["data_json"]
            if accepted:
                revision = server_revision + 1
                updated_at = utc_now()
                DB.execute("UPDATE app_data SET revision = ?, data_json = ?, updated_at = ?, updated_by = ? WHERE id = 1", (revision, json.dumps(incoming, ensure_ascii=False), updated_at, user["id"]))
            else:
                revision = server_revision
                updated_at = row["updated_at"]
        current_data = incoming if accepted else json.loads(row["data_json"])
        self.send_json({"ok": True, "accepted": accepted, "revision": revision, "data": current_data, "savedAt": updated_at, "updatedBy": self.public_user(user)["displayName"] or user["username"]})

    def validate_user_fields(self, payload: dict[str, Any]) -> tuple[str, str, str, str]:
        username = str(payload.get("username") or "").strip().lower()
        display_name = str(payload.get("displayName") or "").strip()[:40]
        password = str(payload.get("password") or "")
        role = str(payload.get("role") or "user")
        if not USERNAME_RE.fullmatch(username):
            raise ValueError("用户名需为 3–32 位英文、数字、点、横线或下划线")
        if len(password) < 8:
            raise ValueError("密码至少需要 8 位")
        if role not in {"admin", "user"}:
            raise ValueError("无效的用户角色")
        return username, display_name, password, role

    def handle_create_user(self, payload: dict[str, Any], admin: sqlite3.Row) -> None:
        try:
            username, display_name, password, role = self.validate_user_fields(payload)
        except ValueError as error:
            self.send_api_error(HTTPStatus.BAD_REQUEST, str(error))
            return
        now = utc_now()
        try:
            with DB_LOCK, DB:
                cursor = DB.execute("INSERT INTO users (username, display_name, password_hash, role, active, created_at, updated_at) VALUES (?, ?, ?, ?, 1, ?, ?)", (username, display_name, hash_password(password), role, now, now))
                user_id = int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            self.send_api_error(HTTPStatus.CONFLICT, "用户名已存在")
            return
        audit(int(admin["id"]), "create_user", username, f"role={role}")
        self.send_json({"ok": True, "userId": user_id}, HTTPStatus.CREATED)

    def handle_update_user(self, user_id: int, payload: dict[str, Any], admin: sqlite3.Row) -> None:
        with DB_LOCK:
            target = DB.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if target is None:
            self.send_api_error(HTTPStatus.NOT_FOUND, "用户不存在")
            return
        changes: list[str] = []
        values: list[Any] = []
        if "displayName" in payload:
            changes.append("display_name = ?")
            values.append(str(payload.get("displayName") or "").strip()[:40])
        if "role" in payload:
            role = str(payload.get("role") or "")
            if role not in {"admin", "user"}:
                self.send_api_error(HTTPStatus.BAD_REQUEST, "无效的用户角色")
                return
            if user_id == int(admin["id"]) and role != "admin":
                self.send_api_error(HTTPStatus.BAD_REQUEST, "不能降低自己的管理员权限")
                return
            changes.append("role = ?")
            values.append(role)
        if "active" in payload:
            active = bool(payload.get("active"))
            if user_id == int(admin["id"]) and not active:
                self.send_api_error(HTTPStatus.BAD_REQUEST, "不能停用当前管理员账号")
                return
            changes.append("active = ?")
            values.append(1 if active else 0)
            if not active:
                with DB_LOCK, DB:
                    DB.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        if not changes:
            self.send_api_error(HTTPStatus.BAD_REQUEST, "没有可更新的字段")
            return
        changes.append("updated_at = ?")
        values.append(utc_now())
        values.append(user_id)
        with DB_LOCK, DB:
            DB.execute(f"UPDATE users SET {', '.join(changes)} WHERE id = ?", values)
        audit(int(admin["id"]), "update_user", target["username"], json.dumps(payload, ensure_ascii=False))
        self.send_json({"ok": True})

    def handle_reset_password(self, user_id: int, payload: dict[str, Any], admin: sqlite3.Row) -> None:
        password = str(payload.get("password") or "")
        if len(password) < 8:
            self.send_api_error(HTTPStatus.BAD_REQUEST, "密码至少需要 8 位")
            return
        with DB_LOCK:
            target = DB.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone()
            if target is None:
                self.send_api_error(HTTPStatus.NOT_FOUND, "用户不存在")
                return
            with DB:
                DB.execute("UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?", (hash_password(password), utc_now(), user_id))
                DB.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        audit(int(admin["id"]), "reset_password", target["username"])
        self.send_json({"ok": True})

    def log_message(self, fmt: str, *args: object) -> None:
        if not urlparse(self.path).path.startswith("/api/session"):
            print(f"[FlyTrack Cloud] {self.address_string()} - {fmt % args}")


def main() -> int:
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    server = FlyTrackHTTPServer(("0.0.0.0", port), Handler)
    print("\nFlyTrack Cloud 已启动")
    print(f"监听端口：{port}")
    print(f"数据库文件：{DB_PATH}")
    print(f"会话 Cookie Secure：{'是' if COOKIE_SECURE else '否'}")
    if not os.environ.get("ADMIN_PASSWORD"):
        print("提示：首次部署请设置 ADMIN_PASSWORD 环境变量。")
    print("生产环境请放在 HTTPS 反向代理之后，并持久化 DATA_DIR。\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
        DB.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
