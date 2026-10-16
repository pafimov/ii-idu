import copy
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import AppServer, connect, now
from broker_sessions import BrokerSessionError, BrokerSessions
from engine import analytics, export_csv, import_csv, ValidationError
from test_app import Client, POSITION
from tinvest import TInvestError


SECRET = "unit-test-readonly-token"
ACCOUNTS = [{"id": "account-1", "name": "Мой брокерский счёт", "type": "ACCOUNT_TYPE_TINKOFF", "access_level": "READ_ONLY"}]
BOND = {"id": "bond-1", "ticker": "TESTBOND", "name": "Тестовая облигация", "asset_class": "bonds",
        "sector": "Финансы", "currency": "RUB", "quantity": 2, "price": 1005, "average_price": 905,
        "source_price": 1000, "principal_average_price": 900, "accrued_interest": 5, "blocked": False}
CASH = {"id": "cash", "ticker": "RUB", "name": "Рубли", "asset_class": "cash", "sector": "Деньги",
        "currency": "RUB", "quantity": 500, "price": 1, "average_price": 1}


class FakeBroker:
    def __init__(self, token):
        if token != SECRET:
            raise TInvestError("INVALID_TOKEN")
        self.calls = []
        self.failure = None
        self.entered = None
        self.release = None

    def accounts(self):
        self.calls.append("accounts")
        return copy.deepcopy(ACCOUNTS)

    def portfolio(self, account_id):
        self.calls.append("portfolio")
        if self.entered:
            self.entered.set()
            if not self.release.wait(3):
                raise TInvestError("TIMEOUT")
        if self.failure:
            raise TInvestError(self.failure)
        return {"positions": copy.deepcopy([BOND, CASH]), "source": "tinvest", "account_id": account_id,
                "broker_total": 2510, "broker_timestamp": None, "fetched_at": now(),
                "accrued_interest_total": 10, "warnings": ["Время котировки не предоставлено."]}


class BrokerApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / "test.sqlite3"
        cls.server = AppServer(("127.0.0.1", 0), cls.database, broker_factory=FakeBroker)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temp.cleanup()

    def setUp(self):
        self.client = Client(self.server.server_port)
        import uuid
        self.email = f"{uuid.uuid4()}@example.invalid"
        self.client.post("/api/register", {"name": "Проверка", "email": self.email, "password": "test-password", "consent": True})

    def state(self):
        return self.client.request("GET", "/api/state")[1]

    def connected_client(self):
        status, result, _ = self.client.post("/api/broker/connect", {"token": SECRET, "consent": True})
        self.assertEqual(status, 200, result)
        self.assertTrue(result["broker"]["connected"])
        with self.server.brokers.lock:
            entry = next(entry for entry in self.server.brokers.connections.values() if entry["accounts"] == ACCOUNTS and entry["owner"] == self.user_id())
            return entry["client"]

    def user_id(self):
        with connect(self.database) as conn:
            return conn.execute("SELECT id FROM users WHERE email=?", (self.email,)).fetchone()["id"]

    def select(self, client=None):
        return (client or self.client).post("/api/broker/select", {"account_id": "account-1", "consent": True})

    def test_connect_requires_registered_user_and_consent(self):
        self.assertEqual(self.client.post("/api/broker/connect", {"token": SECRET})[0], 422)
        guest = Client(self.server.server_port)
        guest.post("/api/demo")
        self.assertEqual(guest.post("/api/broker/connect", {"token": SECRET, "consent": True})[0], 409)
        self.assertEqual(self.client.request("GET", "/api/broker/connect")[0], 405)

    def test_connect_does_not_replace_portfolio_or_leak_secret(self):
        before = self.state()["portfolio"]
        broker = self.connected_client()
        for path in ("/api/state", "/api/report"):
            status, result, _ = self.client.request("GET", path)
            self.assertEqual(status, 200)
            self.assertNotIn(SECRET, json.dumps(result))
        self.assertEqual(self.state()["portfolio"], before)
        self.assertEqual(broker.calls, ["accounts"])
        with connect(self.database) as conn:
            conn.execute("SELECT id FROM users LIMIT 1").fetchone()
            for path in self.database.parent.glob("test.sqlite3*"):
                self.assertNotIn(SECRET.encode(), path.read_bytes())

    def test_selection_is_explicit_and_price_basis_excludes_nkd(self):
        self.connected_client()
        self.assertEqual(self.client.post("/api/broker/select", {"account_id": "account-1"})[0], 422)
        self.assertEqual(self.client.post("/api/broker/select", {"account_id": "other", "consent": True})[0], 422)
        status, result, _ = self.select()
        self.assertEqual(status, 200, result)
        self.assertEqual(result["analytics"]["total_value"], 2510)
        self.assertEqual(result["analytics"]["invested_value"], 2300)
        self.assertEqual(result["analytics"]["pnl"], 200)
        self.assertEqual(result["analytics"]["pnl_percent"], 8.7)
        self.assertEqual(result["portfolio"]["source"], "tinvest")
        status, csv, _ = self.client.request("GET", "/api/portfolio/export")
        self.assertEqual(status, 200)
        self.assertIn("principal_average_price", csv)
        with self.assertRaises(ValidationError):
            import_csv(csv)

    def test_failed_refresh_preserves_snapshot_and_disconnect_blocks_calls(self):
        broker = self.connected_client()
        self.select()
        before = self.state()["portfolio"]
        broker.failure = "RECONCILIATION"
        self.assertEqual(self.client.post("/api/broker/refresh")[0], 422)
        self.assertEqual(self.state()["portfolio"], before)
        self.assertIn("не совпала", self.state()["broker"]["last_error"])
        self.assertEqual(self.client.post("/api/broker/disconnect")[0], 200)
        calls = len(broker.calls)
        self.assertEqual(self.client.post("/api/broker/refresh")[0], 409)
        self.assertEqual(len(broker.calls), calls)
        self.assertEqual(self.state()["portfolio"], before)

    def test_disconnect_discards_inflight_result(self):
        broker = self.connected_client()
        broker.entered, broker.release = threading.Event(), threading.Event()
        other_request = Client(self.server.server_port)
        other_request.cookie = self.client.cookie
        before = self.state()["portfolio"]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.select, other_request)
            self.assertTrue(broker.entered.wait(2))
            self.assertEqual(self.client.post("/api/broker/disconnect")[0], 200)
            broker.release.set()
            self.assertEqual(future.result(timeout=3)[0], 409)
        self.assertEqual(self.state()["portfolio"], before)
        self.assertFalse(self.state()["broker"]["connected"])

    def test_manual_edit_discards_inflight_result(self):
        broker = self.connected_client()
        broker.entered, broker.release = threading.Event(), threading.Event()
        other_request = Client(self.server.server_port)
        other_request.cookie = self.client.cookie
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self.select, other_request)
            self.assertTrue(broker.entered.wait(2))
            self.assertEqual(self.client.request("PUT", "/api/portfolio", {"positions": [POSITION]})[0], 200)
            broker.release.set()
            self.assertEqual(future.result(timeout=3)[0], 409)
        self.assertEqual(self.state()["portfolio"]["source"], "manual")
        self.assertEqual(len(self.state()["portfolio"]["positions"]), 1)

    def test_logout_and_account_deletion_remove_connections(self):
        owner = self.user_id()
        self.connected_client()
        self.select()
        self.client.post("/api/logout")
        self.client.post("/api/login", {"email": self.email, "password": "test-password"})
        self.assertFalse(self.state()["broker"]["connected"])
        self.assertEqual(self.state()["portfolio"]["source"], "tinvest")
        self.connected_client()
        self.assertEqual(self.client.request("DELETE", "/api/account")[0], 200)
        with self.server.brokers.lock:
            self.assertFalse(any(entry["owner"] == owner for entry in self.server.brokers.connections.values()))

    def test_connections_are_isolated_between_app_sessions(self):
        self.connected_client()
        other = Client(self.server.server_port)
        other.post("/api/login", {"email": self.email, "password": "test-password"})
        self.assertFalse(other.request("GET", "/api/state")[1]["broker"]["connected"])
        self.assertEqual(self.select(other)[0], 409)
        self.assertTrue(self.state()["broker"]["connected"])

    def test_upstream_auth_failure_forgets_connection(self):
        broker = self.connected_client()
        self.select()
        before = self.state()["portfolio"]
        broker.failure = "UNAUTHORIZED"
        self.assertEqual(self.client.post("/api/broker/refresh")[0], 422)
        self.assertFalse(self.state()["broker"]["connected"])
        self.assertEqual(self.state()["portfolio"], before)


class BrokerMemoryTests(unittest.TestCase):
    def test_idle_and_hard_expiry_and_stale_identity(self):
        current_time = [1000]
        memory = BrokerSessions(clock=lambda: current_time[0], idle_seconds=30, max_seconds=80)
        try:
            first = memory.prepare("owner", "session", object())
            first.update(ready=True, busy=False)
            current_time[0] += 31
            self.assertFalse(memory.status("owner", "session")["connected"])
            with self.assertRaises(BrokerSessionError):
                memory.begin("owner", "session")
            second = memory.prepare("owner", "session", object())
            second.update(ready=True, busy=False)
            for _ in range(3):
                current_time[0] += 25
                entry = memory.begin("owner", "session")
                memory.finish(entry)
            current_time[0] += 6
            self.assertFalse(memory.status("owner", "session")["connected"])
            with self.assertRaises(BrokerSessionError):
                with memory.accept(first):
                    self.fail("Expired entry accepted")
        finally:
            memory.close()

    def test_zero_cost_has_no_invented_percent(self):
        position = {**POSITION, "average_price": 0}
        self.assertIsNone(analytics([position], None)["pnl_percent"])

    def test_busy_connection_cannot_start_another_token_probe(self):
        memory = BrokerSessions()
        try:
            entry = memory.prepare("owner", "session", object())
            with self.assertRaises(BrokerSessionError):
                memory.prepare("owner", "session", object())
            self.assertFalse(memory.status("other-owner", "session")["connected"])
            with memory.accept(entry):
                self.assertTrue(entry["busy"])
        finally:
            memory.close()


if __name__ == "__main__":
    unittest.main()
