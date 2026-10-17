import http.client
import json
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from app import AppServer, DEMO_PROFILE, connect
from engine import ValidationError, analytics, import_csv, paper_plan, validate_positions, validate_profile


POSITION = {"ticker": "TEST", "name": "Пример", "asset_class": "stocks", "sector": "Технологии", "currency": "RUB", "quantity": 10, "price": 120, "average_price": 100}


class Client:
    def __init__(self, port):
        self.port = port
        self.cookie = ""

    def request(self, method, path, data=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        sent = {"Origin": f"http://127.0.0.1:{self.port}", "Content-Type": "application/json"}
        if self.cookie:
            sent["Cookie"] = self.cookie
        if headers:
            sent.update(headers)
        body = raw if raw is not None else json.dumps(data or {}).encode("utf-8") if method != "GET" else None
        connection.request(method, path, body=body, headers=sent)
        response = connection.getresponse()
        content = response.read()
        cookie = response.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";", 1)[0]
        result = json.loads(content) if response.getheader("Content-Type", "").startswith("application/json") else content.decode("utf-8-sig")
        status, received = response.status, dict(response.getheaders())
        connection.close()
        return status, result, received

    def post(self, path, data=None):
        return self.request("POST", path, data)


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.database = Path(cls.temp.name) / "app.sqlite3"
        cls.server = AppServer(("127.0.0.1", 0), cls.database)
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

    def register(self, client=None):
        client = client or self.client
        email = f"{uuid.uuid4()}@example.ru"
        status, state, headers = client.post("/api/register", {"name": "Павел", "email": email, "password": "safe-password", "consent": True})
        self.assertEqual(status, 200, state)
        return email, state, headers

    def demo(self):
        status, state, _ = self.client.post("/api/demo")
        self.assertEqual(status, 200, state)
        return state

    def preview(self):
        self.demo()
        self.assertEqual(self.client.post("/api/paper/enable", {"consent": True})[0], 200)
        status, state, _ = self.client.post("/api/paper/preview")
        self.assertEqual(status, 200, state)
        return state["paper"]["proposal"]

    def execute(self, proposal, key=None):
        return self.client.post("/api/paper/execute", {"proposal_id": proposal["id"], "consent": True, "idempotency_key": key or str(uuid.uuid4())})

    def test_only_sandbox_connections_have_no_time_limit(self):
        self.assertIsNone(self.server.sandboxes.idle_seconds)
        self.assertIsNone(self.server.sandboxes.max_seconds)
        self.assertEqual(self.server.brokers.idle_seconds, 1800)
        self.assertEqual(self.server.brokers.max_seconds, 28800)

    def test_registration_login_logout_and_session_hash(self):
        self.assertEqual(self.client.request("GET", "/api/state")[0], 401)
        email, state, headers = self.register()
        self.assertIsNone(state["profile"])
        self.assertFalse(state["user"]["is_demo"])
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        with connect(self.database) as conn:
            row = conn.execute("SELECT password FROM users WHERE email=?", (email,)).fetchone()
            self.assertNotIn("safe-password", row["password"])
            self.assertFalse(conn.execute("SELECT 1 FROM sessions WHERE token_hash=?", (self.client.cookie.split("=", 1)[1],)).fetchone())
        self.assertEqual(self.client.post("/api/logout")[0], 200)
        self.assertEqual(self.client.request("GET", "/api/state")[0], 401)
        self.assertEqual(self.client.post("/api/login", {"email": email, "password": "wrong-pass"})[0], 401)
        self.assertEqual(self.client.post("/api/login", {"email": email.upper(), "password": "safe-password"})[0], 200)
        self.assertEqual(self.client.post("/api/register", {"name": "Другой", "email": email, "password": "safe-password", "consent": True})[0], 409)

    def test_accounts_are_isolated_and_account_deletion_cascades(self):
        email, _, _ = self.register()
        self.assertEqual(self.client.request("PUT", "/api/portfolio", {"positions": [POSITION]})[0], 200)
        other = Client(self.server.server_port)
        _, other_state, _ = self.register(other)
        self.assertEqual(other_state["portfolio"]["positions"], [])
        self.assertEqual(other.request("GET", "/api/state")[1]["analytics"]["total_value"], 0)
        self.assertEqual(self.client.request("DELETE", "/api/account")[0], 200)
        self.assertEqual(self.client.request("GET", "/api/state")[0], 401)
        with connect(self.database) as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone())
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_csv_import_atomic_and_export(self):
        initial = self.demo()
        csv = "ticker;name;asset_class;sector;quantity;price;average_price;currency\nTEST;Пример;stocks;Технологии;10;120,5;100;RUB\nBAD;Ошибка;stocks;Финансы;-2;10;8;USD\n"
        status, error, _ = self.client.post("/api/portfolio/import", {"csv": csv})
        self.assertEqual(status, 422)
        self.assertIn("row.3.quantity", error["fields"])
        self.assertIn("row.3.currency", error["fields"])
        self.assertEqual(self.client.request("GET", "/api/state")[1]["portfolio"], initial["portfolio"])
        valid = csv.split("BAD;")[0]
        status, state, _ = self.client.post("/api/portfolio/import", {"csv": valid})
        self.assertEqual(status, 200, state)
        self.assertEqual(state["analytics"]["total_value"], 1205)
        self.assertEqual(state["portfolio"]["source"], "csv")
        status, output, headers = self.client.request("GET", "/api/portfolio/export")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(import_csv(output)[0]["price"], 120.5)

    def test_security_headers_origins_host_and_limits(self):
        status, _, headers = self.client.request("GET", "/api/state")
        self.assertEqual(status, 401)
        self.assertEqual(headers["Cache-Control"], "no-store")
        for origin in ("https://evil.example", "null", "", f"http://localhost:{self.server.server_port}"):
            self.assertEqual(self.client.request("POST", "/api/demo", headers={"Origin": origin})[0], 403)
        self.assertEqual(self.client.request("GET", "/api/sample.csv", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.client.request("POST", "/api/demo", headers={"Content-Type": "text/plain"})[0], 415)
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        try:
            connection.putrequest("POST", "/api/demo")
            connection.putheader("Origin", f"http://127.0.0.1:{self.server.server_port}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "256002")
            connection.endheaders()
            response = connection.getresponse()
            self.assertEqual(response.status, 413)
            self.assertIn("error", json.loads(response.read()))
        finally:
            connection.close()
        self.assertEqual(self.client.request("POST", "/api/demo", raw=b'{"x":NaN}')[0], 400)
        self.assertEqual(self.client.request("POST", "/api/demo", raw=b'{"x":1,"x":2}')[0], 400)
        self.assertEqual(self.client.request("GET", "/../app.py")[0], 404)

    def test_validation_rejects_bool_nonfinite_and_bad_asset_without_mutation(self):
        initial = self.demo()
        for changes in ({"quantity": True}, {"price": float("inf")}, {"asset_class": []}, {"average_price": -1}, {"currency": "USD"}, {"quantity": 10 ** 400}):
            position = {**POSITION, **changes}
            status, _, _ = self.client.request("PUT", "/api/portfolio", {"positions": [position]})
            self.assertIn(status, (400, 422))
        self.assertEqual(self.client.request("GET", "/api/state")[1]["portfolio"], initial["portfolio"])
        self.assertEqual(self.client.request("PUT", "/api/portfolio", {"positions": [POSITION] * 101})[0], 422)

    def test_paper_consent_execution_idempotency_and_user_portfolio_unchanged(self):
        proposal = self.preview()
        initial = self.client.request("GET", "/api/state")[1]
        self.assertGreaterEqual(proposal["remaining_cash"], 10_000)
        self.assertEqual(proposal["profile_version"], initial["profile"]["version"])
        self.assertEqual(self.client.post("/api/paper/execute", {"proposal_id": proposal["id"], "idempotency_key": str(uuid.uuid4())})[0], 422)
        key = str(uuid.uuid4())
        status, executed, _ = self.execute(proposal, key)
        self.assertEqual(status, 200, executed)
        self.assertEqual(executed["portfolio"], initial["portfolio"])
        self.assertGreater(len(executed["paper"]["orders"]), 0)
        self.assertEqual(self.execute(proposal, key)[1]["paper"], executed["paper"])
        self.assertEqual(self.execute(proposal)[0], 409)
        self.assertEqual(self.client.post("/api/paper/preview")[0], 409)
        self.assertEqual(self.client.post("/api/paper/stop")[0], 200)
        self.assertEqual(self.execute(proposal, key)[0], 200)
        self.assertEqual(self.client.request("GET", "/api/state")[1]["paper"]["orders"], executed["paper"]["orders"])

    def test_paper_stop_resume_does_not_revive_proposal(self):
        proposal = self.preview()
        stopped = self.client.post("/api/paper/stop")[1]
        self.assertFalse(stopped["paper"]["enabled"])
        self.assertIsNone(stopped["paper"]["proposal"])
        self.assertEqual(self.execute(proposal)[0], 409)
        self.assertEqual(self.client.post("/api/paper/enable", {"consent": True})[0], 200)
        self.assertEqual(self.execute(proposal)[0], 409)
        fresh = self.client.post("/api/paper/preview")[1]["paper"]["proposal"]
        self.assertNotEqual(fresh["id"], proposal["id"])
        self.assertEqual(self.execute(fresh)[0], 200)

    def test_profile_change_revokes_consent_and_stale_proposal(self):
        proposal = self.preview()
        updated = {**DEMO_PROFILE, "goal_amount": 2_000_000}
        status, state, _ = self.client.post("/api/profile", updated)
        self.assertEqual(status, 200, state)
        self.assertFalse(state["paper"]["enabled"])
        self.assertIsNone(state["paper"]["proposal"])
        self.assertEqual(state["profile"]["version"], proposal["profile_version"] + 1)
        self.assertEqual(self.execute(proposal)[0], 409)
        self.client.post("/api/paper/enable", {"consent": True})
        fresh = self.client.post("/api/paper/preview")[1]["paper"]["proposal"]
        with connect(self.database) as conn:
            conn.execute("UPDATE proposals SET expires_at=? WHERE id=?", (time.time() - 1, fresh["id"]))
        self.assertEqual(self.execute(fresh)[0], 409)
        self.assertIsNone(self.client.request("GET", "/api/state")[1]["paper"]["proposal"])

    def test_paper_rejects_ineligible_profiles(self):
        self.register()
        self.assertEqual(self.client.post("/api/paper/enable", {"consent": True})[0], 422)
        for changes in ({"reserve_months": 2}, {"expensive_debt": True}, {"excluded_sectors": ["Нефтегаз"]}):
            self.assertEqual(self.client.post("/api/profile", {**DEMO_PROFILE, **changes})[0], 200)
            self.assertEqual(self.client.post("/api/paper/enable", {"consent": True})[0], 422)

    def test_copy_demo_requires_explicit_choice_and_current_demo_session(self):
        payload = {"name": "Павел", "email": f"{uuid.uuid4()}@example.ru", "password": "safe-password", "consent": True, "copy_demo": True}
        self.assertEqual(self.client.post("/api/register", payload)[0], 422)
        self.demo()
        self.client.post("/api/profile", {**DEMO_PROFILE, "goal_amount": 2_000_000})
        imported = "ticker,name,asset_class,sector,quantity,price,average_price,currency\nTEST,Пример,stocks,Финансы,10,120,100,RUB\n"
        self.client.post("/api/portfolio/import", {"csv": imported})
        self.client.post("/api/paper/enable", {"consent": True})
        proposal = self.client.post("/api/paper/preview")[1]["paper"]["proposal"]
        self.execute(proposal)
        original = self.client.request("GET", "/api/state")[1]
        status, registered, _ = self.client.post("/api/register", payload)
        self.assertEqual(status, 200, registered)
        self.assertEqual(registered["profile"], original["profile"])
        self.assertEqual(registered["portfolio"], original["portfolio"])
        self.assertEqual(registered["portfolio"]["source"], "csv")
        self.assertFalse(registered["paper"]["enabled"])
        self.assertEqual(registered["paper"]["balance"], 100000)
        self.assertEqual(registered["paper"]["orders"], [])
        self.assertTrue(any(event["kind"] == "demo_copy" for event in registered["events"]))
        self.assertEqual(self.client.post("/api/register", {**payload, "email": f"{uuid.uuid4()}@example.ru"})[0], 422)
        self.client.post("/api/demo")
        _, empty, _ = self.client.post("/api/register", {**payload, "email": f"{uuid.uuid4()}@example.ru", "copy_demo": False})
        self.assertIsNone(empty["profile"])
        self.assertEqual(empty["portfolio"]["positions"], [])

    def test_cross_user_proposal_cannot_execute(self):
        proposal = self.preview()
        other = Client(self.server.server_port)
        other.post("/api/demo")
        other.post("/api/paper/enable", {"consent": True})
        result = other.post("/api/paper/execute", {"proposal_id": proposal["id"], "consent": True, "idempotency_key": str(uuid.uuid4())})
        self.assertEqual(result[0], 409)
        self.assertEqual(self.execute(proposal)[0], 200)

    def test_concurrent_execution_fills_only_once(self):
        proposal = self.preview()
        cookie = self.client.cookie
        results = []
        def execute():
            client = Client(self.server.server_port)
            client.cookie = cookie
            results.append(client.post("/api/paper/execute", {"proposal_id": proposal["id"], "consent": True, "idempotency_key": str(uuid.uuid4())})[0])
        workers = [threading.Thread(target=execute) for _ in range(5)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        self.assertEqual(results.count(200), 1, results)
        self.assertEqual(results.count(409), 4, results)

    def test_report_has_methodology_and_no_password_or_session(self):
        self.demo()
        status, report, headers = self.client.request("GET", "/api/report")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("pnl", report["methodology"])
        self.assertNotIn("password", json.dumps(report))
        self.assertNotIn("session", json.dumps(report))


class EngineTests(unittest.TestCase):
    def test_exact_decimal_math_goal_and_stress(self):
        positions = validate_positions([POSITION, {**POSITION, "ticker": "RUB", "name": "Деньги", "asset_class": "cash", "sector": "Резерв", "quantity": 800, "price": 1, "average_price": 1}])
        result = analytics(positions, DEMO_PROFILE)
        self.assertEqual(result["total_value"], 2000)
        self.assertEqual(result["invested_value"], 1800)
        self.assertEqual(result["pnl"], 200)
        self.assertEqual(result["pnl_percent"], 11.11)
        self.assertEqual(result["largest_position"]["percent"], 60)
        self.assertEqual(result["stress"][0]["loss_value"], 360)
        self.assertEqual(result["goal"]["projected_no_growth"], 902000)
        self.assertEqual(result["goal"]["monthly_needed"], 24966.67)

    def test_csv_accepts_quoted_decimal_comma_and_rejects_invalid_number(self):
        header = "ticker,name,asset_class,sector,quantity,price,average_price,currency\n"
        csv = header + 'TEST,Пример,stocks,Финансы,10,"100,5",90,RUB\n'
        self.assertEqual(import_csv(csv)[0]["price"], 100.5)
        for value in ("NaN", "Infinity", "-1"):
            with self.assertRaises(ValidationError):
                import_csv(header + f"TEST,Пример,stocks,Финансы,10,{value},90,RUB\n")

    def test_cash_is_not_treated_as_security_concentration(self):
        positions = validate_positions([{**POSITION, "ticker": "RUB", "asset_class": "cash", "quantity": 2000, "price": 1, "average_price": 1}])
        result = analytics(positions, None)
        self.assertEqual(result["largest_position"]["percent"], 100)
        self.assertEqual(result["risk"]["concentration_percent"], 0)
        self.assertEqual(result["risk"]["label"], "Низкий")

    def test_paper_capacity_caps_reaction_and_sector_exclusion(self):
        aggressive = {**DEMO_PROFILE, "risk_tolerance": "high", "experience": "experienced"}
        plan = paper_plan(aggressive, 100000)
        stock = next(item for item in plan["orders"] if item["asset_class"] == "stocks")
        self.assertEqual(stock["quantity"], 649)
        plan = paper_plan({**aggressive, "loss_reaction": "sell"}, 100000)
        self.assertLessEqual(next(item for item in plan["orders"] if item["asset_class"] == "stocks")["value"], 15000)
        with self.assertRaises(ValidationError):
            paper_plan({**aggressive, "excluded_sectors": ["Нефтегаз"]}, 100000)

    def test_duplicate_tickers_rejected_and_tickers_normalized(self):
        positions = validate_positions([{**POSITION, "ticker": " test "}])
        self.assertEqual(positions[0]["ticker"], "TEST")
        with self.assertRaises(ValidationError) as error:
            validate_positions([POSITION, {**POSITION, "ticker": "test"}])
        self.assertIn("positions.1.ticker", error.exception.fields)

    def test_cash_requires_rubles_and_one_ruble_prices(self):
        cash = {**POSITION, "ticker": "rub", "asset_class": "cash", "quantity": 15000, "price": 1, "average_price": 1}
        self.assertEqual(analytics(validate_positions([cash]), None)["total_value"], 15000)
        for change in ({"ticker": "USD"}, {"price": 10}, {"average_price": 2}):
            with self.assertRaises(ValidationError):
                validate_positions([{**cash, **change}])

    def test_profile_range_matches_form(self):
        self.assertEqual(validate_profile(DEMO_PROFILE)["horizon_years"], 5)
        for change in ({"goal_amount": 999}, {"horizon_years": 0}, {"horizon_years": .5}, {"horizon_years": 41}, {"horizon_years": 5.5}):
            with self.assertRaises(ValidationError):
                validate_profile({**DEMO_PROFILE, **change})

    def test_insights_explain_profile_mismatch_goal_and_cash(self):
        positions = validate_positions([POSITION, {**POSITION, "ticker": "RUB", "name": "Деньги", "asset_class": "cash", "sector": "Резерв", "quantity": 800, "price": 1, "average_price": 1}])
        result = analytics(positions, {**DEMO_PROFILE, "risk_tolerance": "low"})
        titles = [insight["title"] for insight in result["insights"]]
        self.assertIn("Сверьте состав с готовностью к риску", titles)
        self.assertIn("Взносов пока не хватает до цели", titles)
        self.assertIn("Деньги внутри портфеля", titles)


if __name__ == "__main__":
    unittest.main()
