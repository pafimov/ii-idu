import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from broker_sessions import BrokerSessionError, BrokerSessions
from engine import ValidationError, analytics, export_csv, import_csv, money, paper_eligibility, paper_plan, text, validate_positions, validate_profile
from tinvest import TInvestClient, TInvestError
from sandbox import SandboxClient, SandboxError
from sandbox_service import SandboxService
from sandbox_store import SandboxConflict, initialize as initialize_sandbox


ROOT = Path(__file__).resolve().parent
SESSION_AGE = 7 * 24 * 60 * 60
BODY_LIMIT = 256_000
DEMO_PROFILE = {"goal_name": "Капитал на будущее", "goal_amount": 1_500_000, "horizon_years": 5, "monthly_contribution": 15_000, "risk_tolerance": "medium", "experience": "some", "reserve_months": 6, "expensive_debt": False, "loss_reaction": "wait", "excluded_sectors": []}


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def empty_portfolio():
    return {"positions": [], "source": "manual", "updated_at": now(), "version": 1}


def empty_paper():
    return {"enabled": False, "balance": 100_000, "positions": [], "orders": [], "proposal_id": None}


def demo_portfolio():
    return {"positions": import_csv((ROOT / "fixtures" / "sample.csv").read_text(encoding="utf-8")), "source": "demo", "updated_at": now(), "version": 1}


def password_hash(password):
    salt = secrets.token_bytes(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
    return f"pbkdf2_sha256$210000${salt.hex()}${derived.hex()}"


def password_matches(password, stored):
    try:
        _, iterations, salt, expected = stored.split("$")
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def session_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DatabaseConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(database):
    conn = sqlite3.connect(database, timeout=10, factory=DatabaseConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_database(database):
    path = Path(database)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=0o700)
    with connect(database) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                password TEXT NOT NULL, is_demo INTEGER NOT NULL, profile TEXT,
                profile_version INTEGER NOT NULL DEFAULT 0, portfolio TEXT NOT NULL,
                paper TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS proposals (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                snapshot TEXT NOT NULL, expires_at REAL NOT NULL, profile_version INTEGER NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0, executed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS idempotency (
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                key TEXT NOT NULL, proposal_id TEXT NOT NULL, PRIMARY KEY(user_id, key)
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                kind TEXT NOT NULL, title TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                email_hash TEXT PRIMARY KEY, attempts INTEGER NOT NULL, blocked_until REAL NOT NULL
            );
        """)
        initialize_sandbox(conn)
    os.chmod(path, 0o600)


def event(conn, user_id, kind, title, detail):
    conn.execute("INSERT INTO events(user_id,kind,title,detail,created_at) VALUES(?,?,?,?,?)", (user_id, kind, title, detail, now()))


def get_user(conn, user_id):
    return conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()


def state(conn, user):
    profile = json.loads(user["profile"]) if user["profile"] else None
    if profile:
        profile["version"] = user["profile_version"]
    portfolio, paper = json.loads(user["portfolio"]), json.loads(user["paper"])
    proposal = None
    if paper["proposal_id"]:
        row = conn.execute("SELECT * FROM proposals WHERE id=? AND user_id=?", (paper["proposal_id"], user["id"])).fetchone()
        if row and not row["revoked"] and not row["executed"] and row["expires_at"] > time.time():
            proposal = json.loads(row["snapshot"])
    paper.pop("proposal_id", None)
    paper["proposal"] = proposal
    rows = conn.execute("SELECT id,kind,title,detail,created_at FROM events WHERE user_id=? ORDER BY id DESC LIMIT 100", (user["id"],)).fetchall()
    return {"user": {"name": user["name"], "email": user["email"], "is_demo": bool(user["is_demo"])}, "profile": profile, "portfolio": portfolio, "analytics": analytics(portfolio["positions"], profile), "paper": paper, "events": [dict(row) for row in rows]}


class ApiError(Exception):
    def __init__(self, status, message, fields=None):
        super().__init__(message)
        self.status = status
        self.fields = fields


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, database, broker_factory=TInvestClient, sandbox_factory=SandboxClient, sandbox_worker=True):
        if address[0] != "127.0.0.1":
            raise ValueError("Сервер можно запускать только на 127.0.0.1.")
        self.database = str(database)
        self.broker_factory = broker_factory
        init_database(self.database)
        super().__init__(address, Handler)
        self.brokers = BrokerSessions()
        self.sandboxes = BrokerSessions(idle_seconds=None, max_seconds=None)
        self.sandbox_service = SandboxService(self.database, self.sandboxes, sandbox_factory, sandbox_worker)

    def server_close(self):
        if hasattr(self, "sandbox_service"):
            self.sandbox_service.close()
        if hasattr(self, "sandboxes"):
            self.sandboxes.close()
        if hasattr(self, "brokers"):
            self.brokers.close()
        super().server_close()


class Handler(BaseHTTPRequestHandler):
    server_version = "IIDU"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def do_DELETE(self):
        self.handle_request()

    def do_OPTIONS(self):
        self.send_json(405, {"error": "Метод не поддерживается."})

    def headers_common(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    def send_bytes(self, status, content, content_type, filename=None):
        self.send_response(status)
        self.headers_common()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        if getattr(self, "set_cookie", None):
            self.send_header("Set-Cookie", self.set_cookie)
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, status, data, filename=None):
        self.send_bytes(status, encoded(data).encode("utf-8"), "application/json; charset=utf-8", filename)

    def check_request(self):
        port = self.server.server_port
        host = self.headers.get("Host", "")
        if len(self.headers.get_all("Host", [])) != 1 or host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            raise ApiError(403, "Используйте адрес приложения.")
        if self.command != "GET":
            origin = self.headers.get("Origin", "")
            if origin != f"http://{host}":
                raise ApiError(403, "Запрос должен исходить со страницы этого приложения.")
            if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                raise ApiError(415, "Используйте формат application/json.")
        if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
            raise ApiError(400, "Неподдерживаемый формат запроса.")

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(400, "Неверная длина запроса.") from exc
        if length < 0 or length > BODY_LIMIT:
            raise ApiError(413, "Запрос слишком большой. Лимит — 256 КБ.")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "Запрос получен не полностью.")
        def pairs(items):
            result = {}
            for key, value in items:
                if key in result:
                    raise ValueError("duplicate key")
                result[key] = value
            return result
        def reject_constant(_):
            raise ValueError("non-finite number")
        try:
            data = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject_constant)
        except (ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise ApiError(400, "Некорректный JSON.") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "Ожидается JSON-объект.")
        return data

    def session_token(self):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            token = cookies["session"].value if "session" in cookies else ""
            return token if re.fullmatch(r"[A-Za-z0-9_-]{40,100}", token) else None
        except Exception:
            return None

    def authenticated_user(self, conn):
        token = self.session_token()
        row = conn.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?", (session_hash(token), time.time())).fetchone() if token else None
        if not row:
            raise ApiError(401, "Войдите в аккаунт или откройте демоверсию.")
        user = get_user(conn, row["user_id"])
        if not user:
            raise ApiError(401, "Сессия завершена. Войдите снова.")
        return user

    def start_session(self, conn, user_id):
        previous = self.session_token()
        if previous:
            previous_session = conn.execute("SELECT user_id FROM sessions WHERE token_hash=?", (session_hash(previous),)).fetchone()
            if previous_session:
                self.server.brokers.disconnect(previous_session["user_id"], session_hash(previous))
                self.server.sandboxes.disconnect(previous_session["user_id"], session_hash(previous))
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (session_hash(previous),))
        conn.execute("DELETE FROM sessions WHERE expires_at<=?", (time.time(),))
        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions VALUES(?,?,?)", (session_hash(token), user_id, time.time() + SESSION_AGE))
        self.set_cookie = f"session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={SESSION_AGE}"

    def clear_session(self, conn):
        token = self.session_token()
        if token:
            previous_session = conn.execute("SELECT user_id FROM sessions WHERE token_hash=?", (session_hash(token),)).fetchone()
            if previous_session:
                self.server.brokers.disconnect(previous_session["user_id"], session_hash(token))
                self.server.sandboxes.disconnect(previous_session["user_id"], session_hash(token))
            conn.execute("DELETE FROM sessions WHERE token_hash=?", (session_hash(token),))
        self.set_cookie = "session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"

    def save_portfolio(self, conn, user, positions, source):
        self.server.brokers.disconnect(user["id"], session_hash(self.session_token()))
        portfolio = {"positions": positions, "source": source, "updated_at": now(), "version": json.loads(user["portfolio"])["version"] + 1}
        conn.execute("UPDATE users SET portfolio=? WHERE id=?", (encoded(portfolio), user["id"]))
        event(conn, user["id"], "portfolio", "Портфель обновлен", f"Источник: {source}. Позиций: {len(positions)}. Источник цен указан в снимке.")

    def handle_request(self):
        self.set_cookie = None
        try:
            self.check_request()
            parsed = urlsplit(self.path)
            if parsed.scheme or parsed.netloc:
                raise ApiError(400, "Неверный путь запроса.")
            path = unquote(parsed.path)
            if not path.startswith("/api/"):
                if self.command != "GET":
                    raise ApiError(405, "Метод не поддерживается.")
                return self.serve_static(path)
            if self.command == "GET" and path == "/api/health":
                return self.send_json(200, {"status": "ok", "version": "0.5.0", "sandbox_worker": self.server.sandbox_service.worker.is_alive()})
            if self.command == "GET" and path == "/api/sample.csv":
                return self.send_bytes(200, (ROOT / "fixtures" / "sample.csv").read_bytes(), "text/csv; charset=utf-8", "portfolio-sample.csv")
            data = self.read_json() if self.command != "GET" else {}
            if path.startswith("/api/sandbox/"):
                return self.handle_sandbox(path, data)
            if path.startswith("/api/broker/"):
                return self.handle_broker(path, data)
            conn = connect(self.server.database)
            try:
                conn.execute("BEGIN" if self.command == "GET" else "BEGIN IMMEDIATE")
                result = self.route(conn, path, data)
                conn.commit()
                if result is not None:
                    self.send_json(200, result)
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        except ValidationError as exc:
            self.send_json(422, {"error": str(exc), "fields": exc.fields})
        except ApiError as exc:
            response = {"error": str(exc)}
            if exc.fields:
                response["fields"] = exc.fields
            self.send_json(exc.status, response)
        except SandboxConflict as exc:
            self.send_json(409, {"error": str(exc)})
        except SandboxError as exc:
            self.send_json(422, {"error": str(exc), "code": exc.code})
        except BrokerSessionError as exc:
            self.send_json(409, {"error": str(exc)})
        except TInvestError as exc:
            self.send_json(422, {"error": str(exc), "code": exc.code})
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except Exception:
            self.send_json(500, {"error": "Не удалось выполнить действие. Повторите запрос."})

    def serve_static(self, path):
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        static = (ROOT / "static").resolve()
        target = (static / relative).resolve()
        if not target.is_relative_to(static) or target.suffix not in (".html", ".css", ".js", ".svg", ".png", ".ico", ".woff2") or not target.is_file():
            raise ApiError(404, "Страница не найдена.")
        content_type = mimetypes.guess_type(target)[0] or "application/octet-stream"
        if target.suffix in (".html", ".css", ".js", ".svg"):
            content_type += "; charset=utf-8"
        self.send_bytes(200, target.read_bytes(), content_type)

    def route(self, conn, path, data):
        method = self.command
        if method == "POST" and path in ("/api/demo", "/api/register", "/api/login"):
            return self.authenticate(conn, path, data)
        user = self.authenticated_user(conn)
        user_id = user["id"]
        if method == "GET" and path == "/api/state":
            return self.client_state(conn, user)
        if method == "POST" and path == "/api/logout":
            self.clear_session(conn)
            return {"ok": True}
        if method == "DELETE" and path == "/api/account":
            self.server.brokers.disconnect(user_id)
            self.server.sandboxes.disconnect(user_id)
            self.clear_session(conn)
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            return {"ok": True}
        if method == "GET" and path == "/api/portfolio/export":
            self.send_bytes(200, export_csv(json.loads(user["portfolio"])["positions"]).encode("utf-8"), "text/csv; charset=utf-8", "portfolio.csv")
            return None
        if method == "GET" and path == "/api/report":
            result = {"generated_at": now(), "methodology": {"prices": "Источник и время получения записаны в portfolio. Т-Инвестиции обновляются по кнопке; время получения не гарантирует свежесть котировки.", "pnl": "Изменение цены quantity × (source_price − principal_average_price) для брокерских бумаг, для ручных позиций price − average_price. Текущий НКД исключён из результата и процентной базы; в стоимость портфеля он включён. Без дивидендов, купонов, налогов и комиссий; не полная доходность.", "risk": "Упрощенная классификация состава: доля акций и фондов, максимальная доля одной бумаги без денег. Фонды без раскрытия состава целиком входят в рискованную корзину. Это не рыночная риск-модель и не расчет вероятности потерь.", "stress": "Сценарные изменения классов активов заданы вручную; не прогноз и не оценка вероятности.", "goal": "Текущая стоимость плюс взносы без роста, инфляции, налогов и расходов.", "paper": "Искусственные инструменты по 100 ₽; модельная комиссия 0,1%, нет рыночного исполнения."}, "state": self.client_state(conn, user)}
            self.send_json(200, result, "investment-report.json")
            return None
        if method == "POST" and path == "/api/profile":
            profile = validate_profile(data)
            if not user["profile"] or profile != json.loads(user["profile"]):
                paper = json.loads(user["paper"])
                paper.update(enabled=False, proposal_id=None)
                conn.execute("UPDATE proposals SET revoked=1 WHERE user_id=? AND executed=0", (user_id,))
                conn.execute("UPDATE users SET profile=?,profile_version=profile_version+1,paper=? WHERE id=?", (encoded(profile), encoded(paper), user_id))
                event(conn, user_id, "profile", "Профиль сохранен", "Версия профиля обновлена. Прежние предложения отозваны.")
        elif method == "PUT" and path == "/api/portfolio":
            self.save_portfolio(conn, user, validate_positions(data.get("positions")), "manual")
        elif method == "POST" and path == "/api/portfolio/import":
            self.save_portfolio(conn, user, import_csv(data.get("csv")), "csv")
        elif method == "POST" and path == "/api/portfolio/sample":
            self.save_portfolio(conn, user, demo_portfolio()["positions"], "demo")
        elif method == "POST" and path.startswith("/api/paper/"):
            self.paper_action(conn, user, path, data)
        else:
            raise ApiError(404, "Действие не найдено.")
        return self.client_state(conn, get_user(conn, user_id))

    def client_state(self, conn, user):
        result = state(conn, user)
        token = self.session_token()
        result["sandbox"] = self.server.sandbox_service.public_state(conn, user, session_hash(token)) if token else {}
        result["broker"] = self.server.brokers.status(user["id"], session_hash(token)) if token else {"connected": False}
        return result

    def handle_sandbox(self, path, data):
        if self.command == "GET" and path == "/api/sandbox/report":
            with connect(self.server.database) as conn:
                user = self.authenticated_user(conn)
                return self.send_json(200, self.server.sandbox_service.report(conn, user["id"]), "sandbox-history.json")
        if self.command != "POST":
            raise ApiError(405, "Используйте действие на странице песочницы.")
        with connect(self.server.database) as conn:
            user = self.authenticated_user(conn)
            owner = user["id"]
        key = session_hash(self.session_token())
        self.server.sandbox_service.action(owner, key, path.removeprefix("/api/sandbox/"), data)
        with connect(self.server.database) as conn:
            user = self.authenticated_user(conn)
            self.send_json(200, self.client_state(conn, user))

    def handle_broker(self, path, data):
        if self.command != "POST":
            raise ApiError(405, "Подключение изменяется только явным действием.")
        with connect(self.server.database) as conn:
            user = self.authenticated_user(conn)
            owner = user["id"]
            if user["is_demo"]:
                raise ApiError(409, "Для своего портфеля сначала создайте аккаунт или войдите.")
            old_version = json.loads(user["portfolio"])["version"]
        key = session_hash(self.session_token())
        if path == "/api/broker/disconnect":
            self.server.brokers.disconnect(owner, key)
            with connect(self.server.database) as conn:
                user = self.authenticated_user(conn)
                event(conn, owner, "broker_disconnect", "Т-Инвестиции отключены", "Подключение завершено. Последний снимок сохранён.")
                result = self.client_state(conn, user)
            return self.send_json(200, result)
        if path == "/api/broker/connect":
            if data.get("consent") is not True:
                raise ValidationError("Нажмите «Подключить», чтобы разрешить чтение счёта.")
            entry = self.server.brokers.prepare(owner, key, self.server.broker_factory(data.get("token")))
            try:
                accounts = entry["client"].accounts()
                with connect(self.server.database) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    user = self.authenticated_user(conn)
                    with self.server.brokers.accept(entry):
                        entry.update(accounts=accounts, ready=True, busy=False)
                        event(conn, owner, "broker_connect", "Доступ только для чтения проверен", "Выберите счёт для загрузки портфеля.")
                        conn.commit()
                        result = self.client_state(conn, user)
                return self.send_json(200, result)
            except Exception:
                self.server.brokers.discard(entry)
                raise
        if path not in ("/api/broker/select", "/api/broker/refresh"):
            raise ApiError(404, "Действие не найдено.")
        entry = self.server.brokers.begin(owner, key)
        try:
            account_id = data.get("account_id") if path.endswith("/select") else entry["account_id"]
            if path.endswith("/select") and data.get("consent") is not True:
                raise ValidationError("Подтвердите загрузку выбранного счёта.")
            account = next((account for account in entry["accounts"] if account["id"] == account_id), None)
            if not account:
                raise ValidationError("Выберите доступный счёт из списка.")
            snapshot = entry["client"].portfolio(account_id)
            with connect(self.server.database) as conn:
                conn.execute("BEGIN IMMEDIATE")
                user = self.authenticated_user(conn)
                with self.server.brokers.accept(entry):
                    if json.loads(user["portfolio"])["version"] != old_version:
                        raise ApiError(409, "Портфель изменился во время запроса. Снимок брокера не сохранён; повторите обновление.")
                    portfolio = {**snapshot, "source": "tinvest", "updated_at": snapshot["fetched_at"],
                                 "version": old_version + 1, "account_name": account["name"]}
                    conn.execute("UPDATE users SET portfolio=? WHERE id=?", (encoded(portfolio), owner))
                    event(conn, owner, "broker_sync", "Снимок Т-Инвестиций обновлён", "Счёт выбран пользователем, состав сверён с общей стоимостью брокера. Торговые полномочия не запрашивались.")
                    entry.update(account_id=account_id, account_name=account["name"], last_error=None, busy=False)
                    conn.commit()
                    result = self.client_state(conn, get_user(conn, owner))
            return self.send_json(200, result)
        except Exception as exc:
            if isinstance(exc, TInvestError) and exc.code in ("UNAUTHORIZED", "FORBIDDEN", "READ_ONLY_REQUIRED", "NO_ACCOUNTS"):
                self.server.brokers.discard(entry)
            else:
                self.server.brokers.finish(entry, str(exc) if isinstance(exc, (TInvestError, ValidationError, ApiError, BrokerSessionError)) else "Не удалось обновить снимок. Прежние данные сохранены.")
            raise

    def authenticate(self, conn, path, data):
        if path == "/api/demo":
            user_id = str(uuid.uuid4())
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?)", (user_id, "Александр", f"demo-{user_id}@example.invalid", "", 1, encoded(DEMO_PROFILE), 1, encoded(demo_portfolio()), encoded(empty_paper()), now()))
            event(conn, user_id, "welcome", "Демопортфель готов", "Пример с искусственными ценами. Данные не связаны с брокерским счетом; реальные поручения недоступны.")
        else:
            email = text(data.get("email"), "email", 254).casefold()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                raise ValidationError("Проверьте email.", {"email": "Введите адрес вида name@example.ru."})
            password = data.get("password")
            if not isinstance(password, str) or not 8 <= len(password) <= 128:
                raise ValidationError("Пароль должен содержать от 8 до 128 символов.", {"password": "От 8 до 128 символов."})
            if path == "/api/register":
                name = text(data.get("name"), "name", 60)
                if data.get("consent") is not True:
                    raise ValidationError("Нажмите «Создать аккаунт» для продолжения.")
                if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                    raise ApiError(409, "Аккаунт с таким email уже существует.", {"email": "Попробуйте войти."})
                copy_demo = data.get("copy_demo", False)
                if not isinstance(copy_demo, bool):
                    raise ValidationError("Укажите, нужно ли сохранить работу из демо.", {"copy_demo": "Выберите да или нет."})
                source = None
                if copy_demo:
                    try:
                        source = self.authenticated_user(conn)
                    except ApiError as exc:
                        raise ValidationError("Для копирования работы сначала откройте демоверсию.") from exc
                    if not source["is_demo"]:
                        raise ValidationError("Копирование доступно только из текущей демоверсии.")
                user_id = str(uuid.uuid4())
                conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?)", (user_id, name, email, password_hash(password), 0, source["profile"] if source else None, source["profile_version"] if source else 0, source["portfolio"] if source else encoded(empty_portfolio()), encoded(empty_paper()), now()))
                event(conn, user_id, "welcome", "Аккаунт создан", "Укажите цель и подключите счёт.")
                if source:
                    event(conn, user_id, "demo_copy", "Работа из демо сохранена", "По отдельному согласию скопированы профиль и портфель с исходным источником и временем обновления. Виртуальные покупки и торговые разрешения не переносились.")
            else:
                email_hash = hashlib.sha256(email.encode("utf-8")).hexdigest()
                attempt = conn.execute("SELECT * FROM login_attempts WHERE email_hash=?", (email_hash,)).fetchone()
                if attempt and attempt["blocked_until"] > time.time():
                    raise ApiError(429, "Слишком много попыток входа. Попробуйте через минуту.")
                row = conn.execute("SELECT * FROM users WHERE email=? AND is_demo=0", (email,)).fetchone()
                stored = row["password"] if row else "pbkdf2_sha256$210000$00000000000000000000000000000000$0000000000000000000000000000000000000000000000000000000000000000"
                if not password_matches(password, stored):
                    count = (attempt["attempts"] if attempt and attempt["blocked_until"] > time.time() - 60 else 0) + 1
                    conn.execute("INSERT OR REPLACE INTO login_attempts VALUES(?,?,?)", (email_hash, count, time.time() + 60 if count >= 10 else time.time()))
                    conn.commit()
                    raise ApiError(401, "Неверный email или пароль.")
                conn.execute("DELETE FROM login_attempts WHERE email_hash=?", (email_hash,))
                user_id = row["id"]
        self.start_session(conn, user_id)
        return self.client_state(conn, get_user(conn, user_id))

    def paper_action(self, conn, user, path, data):
        user_id = user["id"]
        paper = json.loads(user["paper"])
        profile = json.loads(user["profile"]) if user["profile"] else None
        if path == "/api/paper/stop":
            paper.update(enabled=False, proposal_id=None)
            conn.execute("UPDATE proposals SET revoked=1 WHERE user_id=? AND executed=0", (user_id,))
            event(conn, user_id, "paper_stop", "Тренажёр остановлена", "Новые операции заблокированы. Возобновление требует отдельного согласия и нового предложения.")
        elif path == "/api/paper/enable":
            if data.get("consent") is not True:
                raise ValidationError("Подтвердите запуск тренажёра.")
            paper_eligibility(profile)
            paper.update(enabled=True, proposal_id=None)
            conn.execute("UPDATE proposals SET revoked=1 WHERE user_id=? AND executed=0", (user_id,))
            event(conn, user_id, "paper_enable", "Тренажёр включена", f"Согласие на виртуальную торговлю по версии профиля {user['profile_version']}. Баланс виртуальный; банковских подключений нет.")
        elif path == "/api/paper/preview":
            if not paper["enabled"]:
                raise ApiError(409, "Сначала включите тренажёр и подтвердите согласие.")
            paper_eligibility(profile)
            if paper["orders"]:
                raise ApiError(409, "Покупка в тренажёре уже выполнена. В этой версии доступна одна серия; пополнение и ребалансировка будут добавлены позже.")
            conn.execute("UPDATE proposals SET revoked=1 WHERE user_id=? AND executed=0", (user_id,))
            proposal_id, expires = str(uuid.uuid4()), time.time() + 600
            plan = paper_plan(profile, paper["balance"])
            plan.update(id=proposal_id, expires_at=datetime.fromtimestamp(expires, timezone.utc).isoformat(timespec="seconds"), profile_version=user["profile_version"])
            snapshot = {**plan, "profile_snapshot": profile, "balance_snapshot": paper["balance"], "model_version": "paper-1"}
            conn.execute("INSERT INTO proposals(id,user_id,snapshot,expires_at,profile_version) VALUES(?,?,?,?,?)", (proposal_id, user_id, encoded(snapshot), expires, user["profile_version"]))
            paper["proposal_id"] = proposal_id
            event(conn, user_id, "paper_preview", "Расчёт тренажёра сформирован", "Предложение действует 10 минут и связано с текущей версией профиля. Исполнение требует отдельного подтверждения.")
        elif path == "/api/paper/execute":
            proposal_id = text(data.get("proposal_id"), "proposal_id", 64)
            key = text(data.get("idempotency_key"), "idempotency_key", 64)
            try:
                uuid.UUID(key)
            except ValueError as exc:
                raise ValidationError("Некорректный ключ операции.") from exc
            repeated = conn.execute("SELECT proposal_id FROM idempotency WHERE user_id=? AND key=?", (user_id, key)).fetchone()
            if repeated:
                if repeated["proposal_id"] != proposal_id:
                    raise ApiError(409, "Ключ уже использован для другой операции.")
                return
            if data.get("consent") is not True:
                raise ValidationError("Подтвердите конкретную покупку в тренажёре.")
            if not paper["enabled"]:
                raise ApiError(409, "Торговля остановлена. Старое предложение больше не действует.")
            paper_eligibility(profile)
            row = conn.execute("SELECT * FROM proposals WHERE id=? AND user_id=?", (proposal_id, user_id)).fetchone()
            if not row or row["revoked"] or row["executed"] or paper["proposal_id"] != proposal_id:
                raise ApiError(409, "Предложение уже исполнено или отозвано. Создайте новый расчет.")
            if row["expires_at"] <= time.time() or row["profile_version"] != user["profile_version"]:
                raise ApiError(409, "Предложение устарело. Создайте новый расчет.")
            snapshot = json.loads(row["snapshot"])
            if paper["orders"] or snapshot["balance_snapshot"] != paper["balance"] or snapshot["total"] > paper["balance"]:
                raise ApiError(409, "Виртуальный баланс изменился. Создайте новый расчет.")
            created = now()
            orders = [{**order, "id": str(uuid.uuid4()), "proposal_id": proposal_id, "status": "filled", "created_at": created, "profile_version": row["profile_version"], "fee": money(order["value"] * .001)} for order in snapshot["orders"]]
            paper["positions"] = [{**order, "id": str(uuid.uuid4()), "currency": "RUB", "sector": "Инструмент тренажёра", "average_price": order["price"]} for order in snapshot["orders"]]
            paper["orders"].extend(orders)
            paper["balance"] = snapshot["remaining_cash"]
            paper["proposal_id"] = None
            conn.execute("UPDATE proposals SET executed=1 WHERE id=? AND user_id=?", (proposal_id, user_id))
            conn.execute("INSERT INTO idempotency VALUES(?,?,?)", (user_id, key, proposal_id))
            event(conn, user_id, "paper_filled", "Серия тренажёра исполнена", f"Виртуальных операций: {len(orders)}. Стоимость с модельной комиссией: {snapshot['total']:g} ₽. Пользовательский портфель не менялся.")
        else:
            raise ApiError(404, "Действие не найдено.")
        conn.execute("UPDATE users SET paper=? WHERE id=?", (encoded(paper), user_id))


def main():
    parser = argparse.ArgumentParser(description="ИИ-ИДУ")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--database", default=str(ROOT / "data" / "app.sqlite3"))
    args = parser.parse_args()
    server = AppServer(("127.0.0.1", args.port), args.database)
    print(f"ИИ-ИДУ: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
