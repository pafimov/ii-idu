import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone


class BrokerSessionError(Exception):
    pass


class BrokerSessions:
    def __init__(self, clock=time.time, idle_seconds=1800, max_seconds=28800):
        self.clock = clock
        self.idle_seconds = idle_seconds
        self.max_seconds = max_seconds
        self.lock = threading.RLock()
        self.connections = {}
        self.stopped = threading.Event()
        self.cleaner = threading.Thread(target=self.clean_expired, daemon=True)
        self.cleaner.start()

    def clean_expired(self):
        while not self.stopped.wait(30):
            with self.lock:
                self.purge()

    def purge(self):
        now = self.clock()
        for key, entry in list(self.connections.items()):
            if now >= entry["created_at"] + self.max_seconds or now >= entry["used_at"] + self.idle_seconds:
                del self.connections[key]

    def prepare(self, owner, key, client):
        with self.lock:
            self.purge()
            if key in self.connections and self.connections[key]["busy"]:
                raise BrokerSessionError("Проверка доступа или загрузка уже выполняется. Дождитесь ответа либо отключите подключение.")
            if key not in self.connections and len(self.connections) >= 32:
                raise BrokerSessionError("Слишком много подключений. Отключите неиспользуемое и повторите.")
            now = self.clock()
            entry = {"owner": owner, "key": key, "client": client, "created_at": now,
                     "used_at": now, "busy": True, "ready": False, "accounts": [],
                     "account_id": None, "account_name": None, "last_error": None, "send_lock": threading.RLock()}
            self.connections[key] = entry
            return entry

    def current(self, owner, key):
        self.purge()
        entry = self.connections.get(key)
        if not entry or entry["owner"] != owner:
            raise BrokerSessionError("Подключение завершено. Подключите токен только для чтения заново.")
        return entry

    @contextmanager
    def accept(self, entry):
        with self.lock:
            current = self.current(entry["owner"], entry["key"])
            if current is not entry:
                raise BrokerSessionError("Подключение изменилось. Ответ прежнего запроса не сохранён.")
            yield entry

    @contextmanager
    def send_guard(self, entry):
        with entry["send_lock"]:
            with self.lock:
                current = self.current(entry["owner"], entry["key"])
                if current is not entry:
                    raise BrokerSessionError("Подключение изменилось. Запрос не отправлен.")
            yield entry

    def touch(self, owner, key):
        with self.lock:
            entry = self.current(owner, key)
            entry["used_at"] = self.clock()

    def begin(self, owner, key, touch=True):
        with self.lock:
            entry = self.current(owner, key)
            if entry["busy"] or not entry["ready"]:
                raise BrokerSessionError("Предыдущий запрос ещё выполняется. Подождите его завершения.")
            entry["busy"] = True
            if touch:
                entry["used_at"] = self.clock()
            return entry

    def finish(self, entry, error=None):
        with self.lock:
            if self.connections.get(entry["key"]) is entry:
                entry["busy"] = False
                entry["last_error"] = error

    def disconnect(self, owner, key=None):
        with self.lock:
            entries = [entry for current_key, entry in self.connections.items() if entry["owner"] == owner and (key is None or key == current_key)]
        for entry in entries:
            with entry["send_lock"], self.lock:
                if self.connections.get(entry["key"]) is entry:
                    del self.connections[entry["key"]]

    def discard(self, entry):
        with self.lock:
            if self.connections.get(entry["key"]) is entry:
                del self.connections[entry["key"]]

    def status(self, owner, key):
        with self.lock:
            try:
                entry = self.current(owner, key)
            except BrokerSessionError:
                return {"connected": False, "accounts": [], "account_id": None, "read_only": True}
            deadline = min(entry["created_at"] + self.max_seconds, entry["used_at"] + self.idle_seconds)
            return {"connected": entry["ready"], "connecting": not entry["ready"],
                    "busy": entry["busy"], "read_only": True,
                    "accounts": [dict(account) for account in entry["accounts"]],
                    "account_id": entry["account_id"], "account_name": entry["account_name"],
                    "expires_at": datetime.fromtimestamp(deadline, timezone.utc).isoformat(timespec="seconds"),
                    "last_error": entry["last_error"]}

    def close(self):
        self.stopped.set()
        with self.lock:
            self.connections.clear()
        self.cleaner.join(timeout=1)
