import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone


class SandboxConflict(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def initial_state():
    return {"account": None, "snapshot": None, "catalog": [], "policy": None,
            "policy_draft": None, "proposal_id": None, "stopped": False, "version": 1,
            "reconcile_error": None, "checked_at": None, "profile": None, "profile_version": 0,
            "demo_config": None, "demo_config_version": 0}


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sandbox_states (
            user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE, payload TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sandbox_proposals (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            account_id TEXT NOT NULL, payload TEXT NOT NULL, expires_at REAL NOT NULL,
            confirmed_at TEXT, revoked INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sandbox_orders (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            proposal_id TEXT NOT NULL REFERENCES sandbox_proposals(id) ON DELETE CASCADE,
            account_id TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sandbox_actions (
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE, key TEXT NOT NULL,
            kind TEXT NOT NULL, status TEXT NOT NULL, account_id TEXT, PRIMARY KEY(user_id,key)
        );
        CREATE TABLE IF NOT EXISTS sandbox_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL
        );
    ''')


class SandboxStore:
    def __init__(self, database):
        self.database = str(database)

    @contextmanager
    def transaction(self):
        conn = sqlite3.connect(self.database, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def authenticate(self, conn, owner, key):
        session = conn.execute("SELECT user_id FROM sessions WHERE token_hash=? AND expires_at>?", (key, time.time())).fetchone()
        if not session or session["user_id"] != owner:
            raise SandboxConflict("Сессия завершена. Войдите снова.")
        user = conn.execute("SELECT * FROM users WHERE id=?", (owner,)).fetchone()
        if not user:
            raise SandboxConflict("Аккаунт не найден.")
        return user

    def load(self, conn, owner):
        row = conn.execute("SELECT payload FROM sandbox_states WHERE user_id=?", (owner,)).fetchone()
        return {**initial_state(), **json.loads(row["payload"])} if row else initial_state()

    def save(self, conn, owner, data):
        conn.execute("INSERT INTO sandbox_states VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET payload=excluded.payload", (owner, encode(data)))

    def audit(self, conn, owner, kind, data):
        conn.execute("INSERT INTO sandbox_audit(user_id,kind,payload,created_at) VALUES(?,?,?,?)", (owner, kind, encode(data), now()))

    def orders(self, conn, owner, account_id=None):
        rows = conn.execute("SELECT * FROM sandbox_orders WHERE user_id=? ORDER BY created_at DESC,id", (owner,)).fetchall()
        return [{**json.loads(row["payload"]), "id": row["id"], "status": row["status"], "created_at": row["created_at"], "account_id": row["account_id"], "proposal_id": row["proposal_id"]} for row in rows if not account_id or row["account_id"] == account_id]

    def order_result(self, conn, owner, request_id, result):
        row = conn.execute("SELECT * FROM sandbox_orders WHERE id=? AND user_id=?", (request_id, owner)).fetchone()
        if not row:
            return
        previous = json.loads(row["payload"])
        old_status = row["status"]
        new_status = result.get("status", "unknown")
        old_lots = previous.get("lots_executed", 0) or 0
        new_lots = result.get("lots_executed", old_lots) or 0
        expected = previous.get("lots", previous.get("lots_requested"))
        mismatch = (result.get("instrument_uid") is not None and result["instrument_uid"] != previous.get("instrument_uid")) or (result.get("direction") is not None and result["direction"] != previous.get("direction")) or (result.get("lots_requested") is not None and result["lots_requested"] != expected) or (result.get("request_id") is not None and result["request_id"] != request_id) or (new_status == "filled" and expected is not None and new_lots != expected)
        if mismatch:
            self.audit(conn, owner, "order_mismatch", {"request_id": request_id, "result": result})
            if old_status not in ("filled", "cancelled", "rejected", "blocked"):
                conn.execute("UPDATE sandbox_orders SET status='unknown' WHERE id=?", (request_id,))
            state = self.load(conn, owner)
            state["reconcile_error"] = "Ответ по заявке не совпадает с подтверждёнными параметрами."
            self.save(conn, owner, state)
            return False
        stale = new_lots < old_lots or (old_status == "filled" and new_status != "filled") or (old_status in ("cancelled", "rejected", "blocked") and new_status != old_status) or (old_status == "partially_filled" and new_status == "new")
        if stale:
            self.audit(conn, owner, "stale_order_result", {"request_id": request_id, "result": result})
            return True
        data = {**previous, **result, "updated_at": now()}
        status = new_status
        conn.execute("UPDATE sandbox_orders SET payload=?,status=? WHERE id=? AND user_id=?", (encode(data), status, request_id, owner))
        self.audit(conn, owner, "order_result", {"request_id": request_id, "result": result})
        return True
