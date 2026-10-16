import copy
import hashlib
import json
import secrets
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app import DEMO_PROFILE, connect, empty_paper, empty_portfolio, encoded, init_database, now, password_hash, session_hash
from broker_sessions import BrokerSessionError, BrokerSessions
from engine import ValidationError
from portfolio_policy import demo_strategies, make_demo_policy, make_policy, rebalance_orders, strategies, validate_demo_config
from sandbox import SandboxError
from sandbox_service import SandboxService
from sandbox_store import SandboxConflict, encode


class FakeSandbox:
    def __init__(self, secret):
        self._secret = secret
        self.account_rows = []
        self.archived_accounts = {}
        self.cash = 0.0
        self.positions = []
        self.remote_orders = {}
        self.open_calls = 0
        self.payments = []
        self.posts = []
        self.state_requests = []
        self.open_unknown = False
        self.pay_unknown = False
        self.post_unknown = False
        self.fee_rate = Decimal("0.0005")
        self.precheck_hook = None
        self.client_precheck_hook = None
        self.before_post = None
        self.catalog_rows = [{"instrument_uid": ticker.lower() + "-uid", "ticker": ticker, "name": ticker,
                              "asset_class": "stocks" if ticker in ("SBER", "LKOH") else "funds",
                              "sector": "Финансы" if ticker == "SBER" else "Фонды: состав не раскрыт",
                              "lot": lot, "tick_size": .01, "price": price, "available": True}
                             for ticker, lot, price in (("SBER", 10, 100), ("LKOH", 1, 1000), ("TMOS", 1, 10), ("TGLD", 1, 100))]

    def accounts(self):
        return copy.deepcopy(self.account_rows)

    def open_account(self, name="ИИ-ИДУ"):
        self.open_calls += 1
        if self.account_rows:
            self.archived_accounts[self.account_rows[-1]["id"]] = {"cash": self.cash, "positions": copy.deepcopy(self.positions)}
        self.cash, self.positions = 0.0, []
        account_id = str(uuid.uuid4())
        self.account_rows.append({"id": account_id, "name": name, "type": "ACCOUNT_TYPE_TINKOFF"})
        if self.open_unknown:
            raise SandboxError("UNKNOWN_EXECUTION")
        return account_id

    def pay_in(self, account_id, amount):
        self.payments.append(amount)
        self.cash += amount
        if self.pay_unknown:
            raise SandboxError("UNKNOWN_EXECUTION")
        return {"balance": self.cash}

    def snapshot(self, account_id):
        if not any(row["id"] == account_id for row in self.account_rows):
            raise SandboxError("INVALID_ACCOUNT")
        active = [copy.deepcopy(row) for row in self.remote_orders.values() if row["status"] in ("new", "partially_filled")]
        total = self.cash + sum(row["quantity"] * row["price"] for row in self.positions)
        return {"account_id": account_id, "positions": copy.deepcopy(self.positions), "cash": self.cash,
                "cash_blocked": 0, "total_value": total, "orders": active, "fetched_at": now(), "warnings": []}

    def catalog(self):
        return copy.deepcopy(self.catalog_rows)

    def preview_order(self, account_id, instrument_uid, lots, limit_price, direction="buy", quote_policy="strict_30s"):
        if self.precheck_hook:
            self.precheck_hook()
        item = next((row for row in self.catalog_rows if row["instrument_uid"] == instrument_uid), None)
        if not item:
            raise SandboxError("UNAVAILABLE")
        if isinstance(lots, bool) or not isinstance(lots, int) or lots <= 0 or direction not in ("buy", "sell"):
            raise SandboxError("INVALID_ORDER")
        price = Decimal(str(limit_price))
        if not price.is_finite() or price <= 0 or price % Decimal(str(item["tick_size"])):
            raise SandboxError("INVALID_ORDER")
        notional = Decimal(lots * item["lot"]) * price
        commission = (notional * self.fee_rate).quantize(Decimal("0.01"))
        buffer = (notional * Decimal("0.005")).quantize(Decimal("0.01"))
        total = notional + commission + buffer if direction == "buy" else commission + buffer
        if direction == "buy" and total > Decimal(str(self.cash)):
            raise SandboxError("INSUFFICIENT_CASH")
        if direction == "sell":
            position = next((row for row in self.positions if row["instrument_uid"] == instrument_uid), None)
            if not position or lots * item["lot"] > position["quantity"]:
                raise SandboxError("INSUFFICIENT_POSITION")
        return {"instrument_uid": instrument_uid, "ticker": item["ticker"], "name": item["name"], "lot": item["lot"],
                "lots": lots, "quantity": lots * item["lot"], "limit_price": float(price), "direction": direction,
                "notional": float(notional), "commission": float(commission), "broker_total": float(notional + commission),
                "cash": self.cash, "buffer": float(buffer), "estimated_total": float(total), "quote_policy": quote_policy,
                "quote_age_seconds": 1, "price_time": now(), "warnings": []}

    def place_order(self, account_id, plan, order_id, before_send=None):
        if self.client_precheck_hook:
            self.client_precheck_hook()
        with before_send() if before_send else nullcontext():
            if self.before_post:
                self.before_post(order_id)
            self.posts.append(order_id)
            row = {"order_id": "remote-" + order_id, "request_id": order_id, "instrument_uid": plan["instrument_uid"],
                   "ticker": plan["ticker"], "direction": plan["direction"], "status": "new", "broker_status": "EXECUTION_REPORT_STATUS_NEW",
                   "lots_requested": plan["lots"], "lots_executed": 0, "executed_value": 0, "commission": 0, "created_at": now()}
            self.remote_orders[order_id] = {**row, "plan": copy.deepcopy(plan)}
            if self.post_unknown:
                raise SandboxError("UNKNOWN_EXECUTION")
            return row

    def fill(self, request_id):
        row = self.remote_orders[request_id]
        if row["status"] == "filled":
            return
        plan = row["plan"]
        position = next((p for p in self.positions if p["instrument_uid"] == plan["instrument_uid"]), None)
        if plan["direction"] == "buy":
            self.cash -= plan["notional"] + plan["commission"]
            if not position:
                item = next(p for p in self.catalog_rows if p["instrument_uid"] == plan["instrument_uid"])
                position = {**item, "quantity": 0, "currency": "RUB", "blocked": False, "available_quantity": 0}
                self.positions.append(position)
            position["quantity"] += plan["quantity"]
        else:
            self.cash += plan["notional"] - plan["commission"]
            position["quantity"] -= plan["quantity"]
        position["available_quantity"] = position["quantity"]
        self.positions = [p for p in self.positions if p["quantity"]]
        row.update(status="filled", broker_status="EXECUTION_REPORT_STATUS_FILL", lots_executed=plan["lots"],
                   executed_value=plan["notional"], commission=plan["commission"])

    def order_state(self, account_id, order_id, request=True):
        self.state_requests.append((order_id, request))
        if request:
            result = self.remote_orders.get(order_id)
        else:
            result = next((row for row in self.remote_orders.values() if row["order_id"] == order_id), None)
        if not result:
            raise SandboxError("NOT_FOUND")
        return {key: copy.deepcopy(value) for key, value in result.items() if key != "plan"}

    def cancel_order(self, account_id, order_id, request=False):
        row = next((row for key, row in self.remote_orders.items() if (key if request else row["order_id"]) == order_id), None)
        if not row:
            raise SandboxError("NOT_FOUND")
        row.update(status="cancelled", broker_status="EXECUTION_REPORT_STATUS_CANCELLED")
        return {"cancel_requested": True, "order_id": order_id}


class SandboxServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "app.sqlite3"
        init_database(self.database)
        self.vault = BrokerSessions()
        self.fakes, self.actor_secrets = {}, {}
        self.factory = lambda token: self.fakes[token]
        self.service = SandboxService(self.database, self.vault, self.factory, start_worker=False)
        self.assertFalse(self.service.worker.is_alive())
        self.network = patch("urllib.request.build_opener", side_effect=AssertionError("Network is forbidden in service tests"))
        self.production = patch("tinvest.TInvestClient.__init__", side_effect=AssertionError("Production broker is forbidden"))
        self.network.start()
        self.production_mock = self.production.start()
        self.actor = self.add_actor()

    def tearDown(self):
        self.service.close()
        self.vault.close()
        self.production_mock.assert_not_called()
        self.production.stop()
        self.network.stop()
        self.temp.cleanup()

    def add_actor(self):
        owner = str(uuid.uuid4())
        raw_session = secrets.token_urlsafe(32)
        key = session_hash(raw_session)
        secret = "fixture-sandbox-secret-" + str(uuid.uuid4())
        fake = FakeSandbox(secret)
        self.fakes[secret] = fake
        self.actor_secrets[owner] = (secret, raw_session)
        with connect(self.database) as conn:
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?,?,?,?,?)", (owner, "Участник", owner + "@example.invalid", password_hash("test-password"), 0, encoded(DEMO_PROFILE), 1, encoded(empty_portfolio()), encoded(empty_paper()), now()))
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (key, owner, time.time() + 3600))
        return {"owner": owner, "key": key, "fake": fake, "secret": secret}

    def action(self, command, payload=None, actor=None):
        actor = actor or self.actor
        return self.service.action(actor["owner"], actor["key"], command, payload or {})

    def public(self, actor=None):
        actor = actor or self.actor
        with connect(self.database) as conn:
            user = conn.execute("SELECT * FROM users WHERE id=?", (actor["owner"],)).fetchone()
            return self.service.public_state(conn, user, actor["key"])

    def ready(self, strategy="balanced", actor=None):
        actor = actor or self.actor
        self.action("connect", {"token": actor["secret"]}, actor)
        self.action("create", {"key": str(uuid.uuid4())}, actor)
        self.action("use-profile", actor=actor)
        self.action("policy-preview", {"strategy_id": strategy, "quote_policy": "sandbox_last_close"}, actor)
        policy = self.public(actor)["policy_draft"]
        self.action("policy-confirm", {"policy_id": policy["id"]}, actor)
        self.action("resume", actor=actor)
        return self.public(actor)

    def demo_ready(self, strategy="balanced"):
        self.action("connect", {"token": self.actor["secret"]})
        self.action("create", {"key": str(uuid.uuid4())})
        return self.demo_preview(strategy)

    def demo_preview(self, strategy="balanced", **changes):
        config = {"strategy_id": strategy, "goal_name": "Новый дом", "target_amount": 20_000_000,
                  "horizon_years": 5, "monthly_contribution": 0, **changes}
        self.action("demo-preview", config)
        return self.public()["proposal"]

    def propose(self, rebalance=False):
        if rebalance:
            self.action("preview-demo")
        else:
            self.action("preview", {"instrument_uid": "sber-uid", "lots": 10, "limit_price": 100, "direction": "buy"})
        return self.public()["proposal"]

    def confirm(self, proposal):
        self.action("execute", {"proposal_id": proposal["id"]})

    def dispatch(self):
        return self.service.dispatch(self.actor["owner"], self.actor["key"])

    def update_state(self, **changes):
        with self.service.store.transaction() as conn:
            data = self.service.store.load(conn, self.actor["owner"])
            data.update(changes)
            self.service.store.save(conn, self.actor["owner"], data)

    def test_create_funds_exactly_ten_million_and_is_idempotent(self):
        self.action("connect", {"token": self.actor["secret"]})
        key = str(uuid.uuid4())
        self.action("create", {"key": key, "amount": 1})
        self.action("create", {"key": key, "amount": 20_000_000})
        self.assertEqual(self.actor["fake"].open_calls, 1)
        self.assertEqual(self.actor["fake"].payments, [10_000_000])
        self.assertEqual(self.public()["snapshot"]["cash"], 10_000_000)
        with self.assertRaises(SandboxConflict):
            self.action("create", {"key": str(uuid.uuid4())})

    def test_unknown_creation_blocks_same_and_fresh_keys_without_retry(self):
        self.action("connect", {"token": self.actor["secret"]})
        self.actor["fake"].open_unknown = True
        key = str(uuid.uuid4())
        with self.assertRaises(SandboxError):
            self.action("create", {"key": key})
        for repeated in (key, str(uuid.uuid4())):
            with self.assertRaises(SandboxConflict):
                self.action("create", {"key": repeated})
        self.assertEqual(self.actor["fake"].open_calls, 1)
        self.assertEqual(self.actor["fake"].payments, [])

    def test_unknown_funding_is_not_repeated(self):
        self.action("connect", {"token": self.actor["secret"]})
        self.actor["fake"].pay_unknown = True
        key = str(uuid.uuid4())
        with self.assertRaises(SandboxError):
            self.action("create", {"key": key})
        for repeated in (key, str(uuid.uuid4())):
            with self.assertRaises(SandboxConflict):
                self.action("create", {"key": repeated})
        self.assertEqual(self.actor["fake"].payments, [10_000_000])

    def test_underfunded_snapshot_is_unknown_and_never_refunded_automatically(self):
        self.action("connect", {"token": self.actor["secret"]})
        fake = self.actor["fake"]
        original_snapshot = fake.snapshot
        def underfunded(account_id):
            result = original_snapshot(account_id)
            result.update(cash=9_000_000, total_value=9_000_000)
            return result
        key = str(uuid.uuid4())
        with patch.object(fake, "snapshot", side_effect=underfunded):
            with self.assertRaises((SandboxError, ValidationError)):
                self.action("create", {"key": key})
        with connect(self.database) as conn:
            status = conn.execute("SELECT status FROM sandbox_actions WHERE user_id=? AND key=?", (self.actor["owner"], key)).fetchone()["status"]
        self.assertEqual(status, "unknown")
        self.assertIsNone(self.public()["snapshot"])
        for repeated in (key, str(uuid.uuid4())):
            with self.assertRaises(SandboxConflict):
                self.action("create", {"key": repeated})
        self.assertEqual(fake.payments, [10_000_000])

    def test_funding_reply_must_agree_with_ten_million_snapshot(self):
        self.action("connect", {"token": self.actor["secret"]})
        fake = self.actor["fake"]
        original = fake.pay_in
        def wrong_balance(account_id, amount):
            original(account_id, amount)
            return {"balance": 9_000_000}
        with patch.object(fake, "pay_in", side_effect=wrong_balance):
            with self.assertRaises((SandboxError, ValidationError)):
                self.action("create", {"key": str(uuid.uuid4())})
        self.assertIsNone(self.public()["snapshot"])
        self.assertEqual(fake.payments, [10_000_000])

    def test_demo_path_needs_only_risk_and_goal_and_keeps_profiles_separate(self):
        with connect(self.database) as conn:
            conn.execute("UPDATE users SET profile=NULL WHERE id=?", (self.actor["owner"],))
        proposal = self.demo_ready("moderate")
        state = self.public()
        self.assertIsNone(state["profile"])
        self.assertIsNone(state["policy_draft"])
        self.assertFalse(state["stopped"])
        self.assertEqual(proposal["policy"]["mode"], "demo")
        self.assertEqual(proposal["demo_config"]["strategy_id"], "moderate")
        self.assertEqual(proposal["quote_policy"], "sandbox_last_close")
        self.assertEqual(proposal["config_version"], state["demo_config_version"])
        self.assertIsNone(proposal["profile"])
        self.assertIsNone(proposal["profile_version"])
        for field in ("experience", "reserve_months", "expensive_debt", "loss_reaction", "risk_tolerance"):
            self.assertNotIn(field, json.dumps(proposal))
        self.assertEqual(self.actor["fake"].posts, [])
        self.confirm(proposal)
        self.assertEqual(self.actor["fake"].posts, [])
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [proposal["orders"][0]["id"]])
        with connect(self.database) as conn:
            self.assertIsNone(conn.execute("SELECT profile FROM users WHERE id=?", (self.actor["owner"],)).fetchone()["profile"])

    def test_demo_card_contract_has_four_increasing_risk_choices(self):
        cards = self.public()["demo_strategies"]
        self.assertEqual([(card["id"], card["name"], card["risk_level"]) for card in cards],
                         [("reserve", "Осторожный", 1), ("moderate", "Умеренный", 2), ("balanced", "Сбалансированный", 3), ("growth", "Динамичный", 4)])
        self.assertEqual([card["weights"]["RUB"] for card in cards], [85, 60, 40, 10])
        self.assertTrue(all(sum(card["weights"].values()) == 100 for card in cards))
        self.assertTrue(all(set(card["weights"]) <= {"RUB", "TGLD", "TMOS", "SBER", "LKOH"} for card in cards))

    def gold_preview(self, code):
        original = self.actor["fake"].preview_order
        def preview(account_id, instrument_uid, *args, **kwargs):
            if instrument_uid == "tgld-uid":
                raise SandboxError(code)
            return original(account_id, instrument_uid, *args, **kwargs)
        return patch.object(self.actor["fake"], "preview_order", side_effect=preview)

    def test_closed_gold_fund_does_not_block_the_rest_of_the_basket(self):
        self.action("connect", {"token": self.actor["secret"]})
        self.action("create", {"key": str(uuid.uuid4())})
        with self.gold_preview("TRADING_CLOSED"):
            proposal = self.demo_preview("balanced")
        self.assertEqual({row["ticker"] for row in proposal["orders"]}, {"TMOS", "SBER", "LKOH"})
        self.assertEqual([item["ticker"] for item in proposal["skipped"]], ["TGLD"])
        self.assertGreater(proposal["skipped"][0]["amount"], 0)
        self.assertIn("TGLD", proposal["reason"])
        self.assertIn("остаются в рублях", proposal["reason"])
        self.confirm(proposal)
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [proposal["orders"][0]["id"]])

    def test_open_trading_has_no_skipped_instruments(self):
        proposal = self.demo_ready("balanced")
        self.assertEqual(proposal["skipped"], [])
        self.assertEqual({row["ticker"] for row in proposal["orders"]}, {"TGLD", "TMOS", "SBER", "LKOH"})

    def test_basket_with_only_closed_instruments_explains_why_nothing_is_bought(self):
        self.action("connect", {"token": self.actor["secret"]})
        self.action("create", {"key": str(uuid.uuid4())})
        with self.gold_preview("TRADING_CLOSED"):
            with self.assertRaises(ValidationError) as caught:
                self.demo_preview("reserve")
        self.assertIn("TGLD", str(caught.exception))
        self.assertIsNone(self.public()["proposal"])

    def test_other_unavailable_reasons_still_block_the_basket(self):
        self.action("connect", {"token": self.actor["secret"]})
        self.action("create", {"key": str(uuid.uuid4())})
        with self.gold_preview("UNAVAILABLE"):
            with self.assertRaises(SandboxError):
                self.demo_preview("balanced")
        self.assertIsNone(self.public()["proposal"])

    def test_demo_preview_failure_preserves_confirmed_series_and_both_profiles(self):
        self.ready()
        previous = self.propose()
        self.confirm(previous)
        before = self.public()
        with patch.object(self.actor["fake"], "preview_order", side_effect=SandboxError("UNAVAILABLE")):
            with self.assertRaises(SandboxError):
                self.demo_preview("growth")
        after = self.public()
        for field in ("policy", "profile", "profile_version", "demo_config", "demo_config_version", "proposal", "orders", "stopped"):
            self.assertEqual(after[field], before[field], field)
        self.assertEqual(after["proposal"]["fingerprint"], previous["fingerprint"])
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [previous["orders"][0]["id"]])
        self.assertEqual(self.actor["fake"].open_calls, 1)
        self.assertEqual(self.actor["fake"].payments, [10_000_000])

    def test_invalid_demo_goal_does_not_change_current_configuration(self):
        self.demo_ready()
        before = self.public()
        for changes in ({"strategy_id": []}, {"strategy_id": "invented"}, {"target_amount": float("nan")},
                        {"target_amount": True}, {"horizon_years": 1.5}, {"horizon_years": 41},
                        {"monthly_contribution": -1}, {"goal_name": ""}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.demo_preview(**changes)
        after = self.public()
        for field in ("policy", "demo_config", "demo_config_version", "proposal", "account"):
            self.assertEqual(after[field], before[field], field)

    def test_successful_demo_change_versions_and_revokes_previous_consent(self):
        previous = self.demo_ready("growth")
        self.confirm(previous)
        current = self.demo_preview("reserve", goal_name="Резерв", horizon_years=1)
        self.assertGreater(current["config_version"], previous["config_version"])
        self.assertGreater(current["policy"]["version"], previous["policy"]["version"])
        self.assertNotEqual(current["fingerprint"], previous["fingerprint"])
        self.assertEqual(current["policy"]["weights"]["RUB"], 85)
        self.assertEqual({row["ticker"] for row in current["orders"]}, {"TGLD"})
        with self.assertRaises(ValidationError):
            self.confirm(previous)
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])
        self.confirm(current)
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [current["orders"][0]["id"]])

    def test_demo_preview_does_not_override_stop_during_network_precheck(self):
        self.demo_ready()
        self.actor["fake"].precheck_hook = lambda: self.action("stop")
        with self.assertRaises(SandboxConflict):
            self.demo_preview("moderate")
        self.assertTrue(self.public()["stopped"])
        self.assertEqual(self.public()["policy"]["strategy_id"], "balanced")
        self.assertEqual(self.actor["fake"].posts, [])

    def test_demo_preview_preserves_prior_series_if_order_changes_during_calculation(self):
        previous = self.demo_ready()
        self.confirm(previous)
        request_id = previous["orders"][0]["id"]
        def admitted_elsewhere():
            with connect(self.database) as conn:
                conn.execute("UPDATE sandbox_orders SET status='new' WHERE id=?", (request_id,))
        self.actor["fake"].precheck_hook = admitted_elsewhere
        with self.assertRaises(SandboxConflict):
            self.demo_preview("moderate")
        current = self.public()
        self.assertEqual(current["proposal"]["id"], previous["id"])
        self.assertEqual(current["policy"]["id"], previous["policy_id"])
        self.assertEqual(next(row for row in current["orders"] if row["id"] == request_id)["status"], "new")

    def test_demo_configuration_change_cannot_reuse_confirmed_consent(self):
        proposal = self.demo_ready()
        self.confirm(proposal)
        state = self.public()
        changed = {**state["demo_config"], "strategy_id": "growth"}
        self.update_state(demo_config=changed)
        with self.assertRaises(ValidationError):
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual(self.stored_proposal(proposal["id"])["fingerprint"], proposal["fingerprint"])

    def test_policy_payload_is_bound_after_confirmation_even_if_id_is_unchanged(self):
        proposal = self.demo_ready()
        self.confirm(proposal)
        policy = self.public()["policy"]
        policy["weights"]["RUB"] = 10
        policy["cash_reserve_percent"] = 10
        policy["version"] += 1
        self.update_state(policy=policy)
        with self.assertRaises(ValidationError):
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])

    def test_reset_starts_new_ten_million_account_once_and_retains_history(self):
        proposal = self.demo_ready()
        self.confirm(proposal)
        old = self.public()
        key = str(uuid.uuid4())
        self.action("reset-demo", {"key": key})
        current = self.public()
        self.assertNotEqual(current["account"]["id"], old["account"]["id"])
        self.assertEqual(current["snapshot"]["cash"], 10_000_000)
        self.assertEqual(current["snapshot"]["positions"], [])
        self.assertIsNone(current["policy"])
        self.assertIsNone(current["demo_config"])
        self.assertIsNone(current["proposal"])
        self.assertTrue(current["stopped"])
        self.assertEqual(current["orders"], [])
        self.assertEqual(current["connection"]["account_id"], current["account"]["id"])
        self.assertEqual(self.actor["fake"].open_calls, 2)
        self.assertEqual(self.actor["fake"].payments, [10_000_000, 10_000_000])
        self.action("reset-demo", {"key": key.upper()})
        self.assertEqual(self.public()["account"], current["account"])
        self.assertEqual(self.actor["fake"].open_calls, 2)
        self.assertEqual(self.actor["fake"].payments, [10_000_000, 10_000_000])
        with connect(self.database) as conn:
            orders = self.service.store.orders(conn, self.actor["owner"], old["account"]["id"])
            self.assertEqual({row["status"] for row in orders}, {"cancelled"})
            self.assertEqual({row["id"] for row in orders}, {row["id"] for row in proposal["orders"]})
            events = [row["kind"] for row in conn.execute("SELECT kind FROM sandbox_audit WHERE user_id=?", (self.actor["owner"],))]
        self.assertIn("demo_reset_started", events)
        self.assertIn("demo_reset_completed", events)
        self.assertEqual(self.actor["fake"].posts, [])

    def test_reset_refuses_active_or_unknown_orders(self):
        proposal = self.demo_ready()
        self.confirm(proposal)
        self.dispatch()
        for status in ("new", "partially_filled", "submitting", "unknown"):
            with self.subTest(status=status):
                with connect(self.database) as conn:
                    conn.execute("UPDATE sandbox_orders SET status=? WHERE id=?", (status, proposal["orders"][0]["id"]))
                with self.assertRaises(SandboxConflict):
                    self.action("reset-demo", {"key": str(uuid.uuid4())})
                self.assertEqual(self.actor["fake"].open_calls, 1)
                self.assertEqual(self.public()["account"]["id"], proposal["account_id"])

    def test_reset_unknown_funding_blocks_same_and_new_reset_keys(self):
        self.demo_ready()
        self.actor["fake"].pay_unknown = True
        key = str(uuid.uuid4())
        with self.assertRaises(SandboxError):
            self.action("reset-demo", {"key": key})
        for repeated in (key, str(uuid.uuid4())):
            with self.assertRaises(SandboxConflict):
                self.action("reset-demo", {"key": repeated})
        self.assertEqual(self.actor["fake"].open_calls, 2)
        self.assertEqual(self.actor["fake"].payments, [10_000_000, 10_000_000])
        with connect(self.database) as conn:
            actions = conn.execute("SELECT kind,status FROM sandbox_actions WHERE user_id=?", (self.actor["owner"],)).fetchall()
        self.assertIn(("reset", "unknown"), [(row["kind"], row["status"]) for row in actions])

    def pending_action(self, status="unknown", action_key=None):
        action_key = action_key or str(uuid.uuid4())
        with connect(self.database) as conn:
            conn.execute("INSERT OR REPLACE INTO sandbox_actions VALUES(?,?,?,?,?)", (self.actor["owner"], action_key, "create", status, self.public()["account"]["id"]))
        return action_key

    def test_unresolved_actions_block_preview_confirmation_and_dispatch(self):
        proposal = self.demo_ready()
        for status in ("opening", "funding", "unknown", "resetting"):
            with self.subTest(status=status):
                action_key = self.pending_action(status)
                with patch.object(self.actor["fake"], "preview_order", wraps=self.actor["fake"].preview_order) as preview:
                    with self.assertRaises(SandboxConflict):
                        self.demo_preview("moderate")
                    preview.assert_not_called()
                with self.assertRaises(SandboxConflict):
                    self.confirm(proposal)
                with self.assertRaises(SandboxConflict):
                    self.dispatch()
                with connect(self.database) as conn:
                    conn.execute("DELETE FROM sandbox_actions WHERE user_id=? AND key=?", (self.actor["owner"], action_key))
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual(self.public()["proposal"]["id"], proposal["id"])

    def test_funding_timeout_remains_blocked_after_reconnect_and_read_refresh(self):
        fake = self.actor["fake"]
        self.action("connect", {"token": self.actor["secret"]})
        fake.pay_unknown = True
        with self.assertRaises(SandboxError):
            self.action("create", {"key": str(uuid.uuid4())})
        fake.pay_unknown = False
        self.action("connect", {"token": self.actor["secret"]})
        self.action("refresh")
        self.assertEqual(self.public()["snapshot"]["cash"], 10_000_000)
        with self.assertRaises(SandboxConflict):
            self.demo_preview()
        self.assertIsNone(self.public()["policy"])
        self.assertEqual(fake.posts, [])
        self.assertEqual(fake.payments, [10_000_000])

    def test_pending_action_during_preview_prevents_commit_and_preserves_old_plan(self):
        previous = self.demo_ready()
        action_key = str(uuid.uuid4())
        self.actor["fake"].precheck_hook = lambda: self.pending_action("unknown", action_key)
        with self.assertRaises(SandboxConflict):
            self.demo_preview("moderate")
        self.assertEqual(self.public()["proposal"]["id"], previous["id"])
        self.assertEqual(self.public()["policy"]["id"], previous["policy_id"])
        self.assertEqual(self.actor["fake"].posts, [])

    def test_pending_action_at_final_client_gate_prevents_actual_post(self):
        proposal = self.demo_ready()
        self.confirm(proposal)
        self.actor["fake"].client_precheck_hook = lambda: self.pending_action("unknown")
        with self.assertRaises(SandboxConflict):
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertFalse(any(order["status"] == "submitting" for order in self.public()["orders"]))

    def test_paused_refresh_cannot_restore_old_account_during_reset(self):
        self._refresh_during_reset(False)

    def test_paused_refresh_error_cannot_pollute_new_account_during_reset(self):
        self._refresh_during_reset(True)

    def _refresh_during_reset(self, fail_refresh):
        self.demo_ready()
        old_account = self.public()["account"]
        fake = self.actor["fake"]
        second_key = session_hash(secrets.token_urlsafe(32))
        with connect(self.database) as conn:
            conn.execute("INSERT INTO sessions VALUES(?,?,?)", (second_key, self.actor["owner"], time.time() + 3600))
        second = {**self.actor, "key": second_key}
        self.action("connect", {"token": self.actor["secret"]}, second)
        self.action("select", {"account_id": old_account["id"]}, second)
        refreshing, resetting = threading.Event(), threading.Event()
        release_refresh, release_reset, refresh_done = threading.Event(), threading.Event(), threading.Event()
        refresh_errors, reset_errors = [], []
        original_snapshot, original_open = fake.snapshot, fake.open_account
        def snapshot(account_id):
            result = original_snapshot(account_id)
            if threading.current_thread() is refresh_thread:
                refreshing.set()
                if not release_refresh.wait(5):
                    raise AssertionError("Refresh was not released")
                if fail_refresh:
                    raise SandboxError("UNAVAILABLE")
            return result
        def open_account(name):
            resetting.set()
            if not release_reset.wait(5):
                raise AssertionError("Reset was not released")
            return original_open(name)
        def refresh():
            try:
                self.action("refresh", actor=second)
            except Exception as exc:
                refresh_errors.append(exc)
            finally:
                refresh_done.set()
        def reset():
            try:
                self.action("reset-demo", {"key": str(uuid.uuid4())})
            except Exception as exc:
                reset_errors.append(exc)
        refresh_thread = threading.Thread(target=refresh)
        reset_thread = threading.Thread(target=reset)
        with patch.object(fake, "snapshot", side_effect=snapshot), patch.object(fake, "open_account", side_effect=open_account):
            refresh_thread.start()
            try:
                self.assertTrue(refreshing.wait(2))
                reset_thread.start()
                self.assertTrue(resetting.wait(2))
                self.assertIsNone(self.public()["account"])
                release_refresh.set()
                self.assertTrue(refresh_done.wait(2))
                intermediate = self.public()
                self.assertIsNone(intermediate["account"])
                self.assertIsNone(intermediate["snapshot"])
                self.assertIsNone(intermediate["reconcile_error"])
            finally:
                release_refresh.set()
                release_reset.set()
                refresh_thread.join(4)
                if reset_thread.ident is not None:
                    reset_thread.join(4)
        self.assertFalse(refresh_thread.is_alive())
        self.assertFalse(reset_thread.is_alive())
        self.assertEqual(reset_errors, [])
        self.assertEqual(len(refresh_errors), 1)
        self.assertIsInstance(refresh_errors[0], SandboxError if fail_refresh else SandboxConflict)
        self.assertNotEqual(self.public()["account"]["id"], old_account["id"])
        self.assertEqual(self.public()["snapshot"]["cash"], 10_000_000)
        self.assertEqual(fake.open_calls, 2)
        self.assertEqual(fake.posts, [])

    def test_reset_requires_owner_connection_and_valid_unique_key(self):
        self.demo_ready()
        other = self.add_actor()
        with self.assertRaises((SandboxConflict, BrokerSessionError)):
            self.service.action(other["owner"], self.actor["key"], "reset-demo", {"key": str(uuid.uuid4())})
        with self.assertRaises(ValidationError):
            self.action("reset-demo", {"key": "invalid"})
        self.action("disconnect")
        with self.assertRaises(BrokerSessionError):
            self.action("reset-demo", {"key": str(uuid.uuid4())})
        self.assertEqual(self.actor["fake"].open_calls, 1)

    def test_reset_success_is_idempotent_after_server_restart_without_connection(self):
        self.demo_ready()
        key = str(uuid.uuid4())
        self.action("reset-demo", {"key": key})
        account = self.public()["account"]
        self.service.close()
        self.vault.close()
        self.vault = BrokerSessions()
        self.service = SandboxService(self.database, self.vault, self.factory, start_worker=False)
        self.action("reset-demo", {"key": key})
        self.assertEqual(self.public()["account"], account)
        self.assertEqual(self.actor["fake"].open_calls, 2)
        self.assertEqual(self.actor["fake"].payments, [10_000_000, 10_000_000])

    def test_final_fill_refreshes_snapshot_with_short_bounded_retries(self):
        proposal = self.demo_ready("reserve")
        self.confirm(proposal)
        self.dispatch()
        fake = self.actor["fake"]
        old_snapshot = fake.snapshot(proposal["account_id"])
        fake.fill(fake.posts[0])
        identity = (self.actor["owner"], self.actor["key"])
        self.service.last_refresh[identity] = time.monotonic() - 11
        with patch.object(fake, "snapshot", return_value=old_snapshot) as snapshot:
            self.service.tick(*identity)
            self.assertEqual(self.public()["orders"][0]["status"], "filled")
            self.assertEqual(self.service.refresh_pending[identity], 2)
            self.service.last_refresh[identity] = time.monotonic() - 3
            self.service.tick(*identity)
            self.assertEqual(snapshot.call_count, 2)
        self.service.last_refresh[identity] = time.monotonic() - 3
        self.service.tick(*identity)
        current = self.public()
        self.assertIsNone(current["reconcile_error"])
        self.assertEqual(current["snapshot"]["positions"], fake.positions)
        self.assertEqual(current["snapshot"]["cash"], fake.cash)
        self.assertEqual(current["proposal"]["id"], proposal["id"])
        self.assertNotIn(identity, self.service.refresh_pending)
        self.assertEqual(fake.posts, [proposal["orders"][0]["id"]])

    def test_final_fill_does_not_poll_forever_if_snapshot_keeps_lagging(self):
        proposal = self.demo_ready("reserve")
        self.confirm(proposal)
        self.dispatch()
        fake = self.actor["fake"]
        lagging = fake.snapshot(proposal["account_id"])
        fake.fill(fake.posts[0])
        identity = (self.actor["owner"], self.actor["key"])
        self.service.last_refresh[identity] = time.monotonic() - 11
        with patch.object(fake, "snapshot", return_value=lagging) as snapshot:
            for _ in range(3):
                self.service.tick(*identity)
                self.service.last_refresh[identity] = time.monotonic() - 3
            self.service.tick(*identity)
            self.assertEqual(snapshot.call_count, 3)
        self.assertNotIn(identity, self.service.refresh_pending)
        self.assertTrue(self.public()["reconcile_error"])
        self.assertEqual(len(fake.posts), 1)

    def test_same_second_snapshot_with_three_positions_is_not_reconciled_with_four_fills(self):
        proposal = self.demo_ready()
        self.confirm(proposal)
        fake = self.actor["fake"]
        for _ in range(3):
            self.dispatch()
            fake.fill(fake.posts[-1])
            self.action("refresh")
        before = self.public()["snapshot"]
        self.assertEqual(len(before["positions"]), 3)
        self.dispatch()
        last = fake.posts[-1]
        fake.fill(last)
        with self.service.store.transaction() as conn:
            self.service.store.order_result(conn, self.actor["owner"], last, fake.order_state(proposal["account_id"], last))
            row = conn.execute("SELECT payload FROM sandbox_orders WHERE id=?", (last,)).fetchone()
            order = json.loads(row["payload"])
            order["updated_at"] = before["fetched_at"]
            conn.execute("UPDATE sandbox_orders SET payload=? WHERE id=?", (encode(order), last))
        current = self.public()
        self.assertEqual(len(current["snapshot"]["positions"]), 3)
        self.assertTrue(all(row["status"] == "filled" for row in current["orders"]))
        self.assertFalse(current["snapshot_reconciled"])
        self.action("refresh")
        self.assertTrue(self.public()["snapshot_reconciled"])
        self.assertEqual(len(self.public()["snapshot"]["positions"]), 4)
        self.assertEqual(len(fake.posts), 4)

    def test_expired_confirmed_proposal_still_serves_reconciled_positions(self):
        proposal = self.demo_ready("reserve")
        self.confirm(proposal)
        self.dispatch()
        self.actor["fake"].fill(self.actor["fake"].posts[0])
        self.action("refresh")
        with patch("sandbox_service.time.time", return_value=time.time() + 601):
            state = self.public()
            self.assertTrue(state["proposal"]["expired"])
            self.assertTrue(state["snapshot_reconciled"])

    def test_separate_policy_confirmation_and_dispatch_are_required(self):
        self.action("connect", {"token": self.actor["secret"]})
        self.action("create", {"key": str(uuid.uuid4())})
        self.action("use-profile")
        self.action("policy-preview", {"strategy_id": "balanced"})
        self.assertIsNone(self.public()["policy"])
        with self.assertRaises(ValidationError):
            self.propose()
        draft = self.public()["policy_draft"]
        self.action("policy-confirm", {"policy_id": draft["id"]})
        self.action("resume")
        proposal = self.propose()
        self.assertEqual(self.actor["fake"].posts, [])
        self.confirm(proposal)
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual({row["status"] for row in self.public()["orders"]}, {"draft"})
        self.dispatch()
        self.assertEqual(len(self.actor["fake"].posts), 1)

    def test_request_uuid_and_submitting_record_exist_before_post(self):
        self.ready()
        proposal = self.propose()
        request_id = proposal["orders"][0]["id"]
        uuid.UUID(request_id)
        self.confirm(proposal)
        seen = []
        def inspect(request_id):
            with connect(self.database) as conn:
                row = conn.execute("SELECT status FROM sandbox_orders WHERE id=?", (request_id,)).fetchone()
                seen.append(row["status"])
        self.actor["fake"].before_post = inspect
        self.dispatch()
        self.confirm(proposal)
        self.dispatch()
        self.assertEqual(seen, ["submitting"])
        self.assertEqual(self.actor["fake"].posts, [request_id])
        self.assertEqual(self.public()["orders"][0]["id"], request_id)

    def test_accepted_order_is_not_filled_and_blocks_next_order(self):
        self.ready()
        proposal = self.propose(rebalance=True)
        self.assertGreater(len(proposal["orders"]), 1)
        self.confirm(proposal)
        self.dispatch()
        first = self.actor["fake"].posts[0]
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [first])
        orders = self.public()["orders"]
        self.assertEqual(next(row for row in orders if row["id"] == first)["status"], "new")
        self.assertTrue(any(row["status"] == "draft" for row in orders))

    def test_series_continues_in_reviewed_order_only_after_fill(self):
        self.ready()
        proposal = self.propose(rebalance=True)
        expected = [row["id"] for row in proposal["orders"]]
        self.confirm(proposal)
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, expected[:1])
        self.actor["fake"].fill(expected[0])
        self.action("refresh")
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, expected[:2])
        self.assertTrue(all(request for _, request in self.actor["fake"].state_requests))

    def test_unknown_post_preserves_uuid_blocks_series_and_is_reconciled(self):
        self.ready()
        proposal = self.propose(rebalance=True)
        self.confirm(proposal)
        self.actor["fake"].post_unknown = True
        with self.assertRaises(SandboxError):
            self.dispatch()
        first = self.actor["fake"].posts[0]
        self.assertEqual(next(row for row in self.public()["orders"] if row["id"] == first)["status"], "unknown")
        self.confirm(proposal)
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [first])
        self.action("refresh")
        self.assertIn((first, True), self.actor["fake"].state_requests)
        self.assertEqual(next(row for row in self.public()["orders"] if row["id"] == first)["status"], "new")

    def test_rejected_order_revokes_remaining_series(self):
        self.ready()
        self.confirm(self.propose(rebalance=True))
        self.dispatch()
        first = self.actor["fake"].posts[0]
        self.actor["fake"].remote_orders[first].update(status="rejected", broker_status="EXECUTION_REPORT_STATUS_REJECTED")
        self.action("refresh")
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [first])
        self.assertFalse(any(row["status"] == "draft" for row in self.public()["orders"]))

    def test_explicit_cancel_aborts_remaining_series(self):
        self.ready()
        self.confirm(self.propose(rebalance=True))
        self.dispatch()
        first = self.actor["fake"].posts[0]
        self.action("cancel", {"order_id": first})
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [first])
        self.assertFalse(any(row["status"] == "draft" for row in self.public()["orders"]))

    def test_higher_commission_requires_new_consent_before_post(self):
        self.ready()
        self.confirm(self.propose())
        self.actor["fake"].fee_rate = Decimal("0.01")
        with self.assertRaises(ValidationError):
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual({row["status"] for row in self.public()["orders"]}, {"blocked"})

    def test_vault_and_owner_sessions_are_isolated(self):
        self.ready()
        other = self.add_actor()
        self.assertFalse(self.public(other)["connection"]["connected"])
        with self.assertRaises((BrokerSessionError, SandboxConflict)):
            self.service.action(other["owner"], self.actor["key"], "refresh", {})
        with self.assertRaises(SandboxConflict):
            self.service.action(other["owner"], self.actor["key"], "stop", {})
        self.assertIsNone(self.public(other)["account"])
        self.assertEqual(other["fake"].posts, [])

    def test_expired_auth_cannot_send_queued_order(self):
        self.ready()
        self.confirm(self.propose())
        with connect(self.database) as conn:
            conn.execute("UPDATE sessions SET expires_at=? WHERE token_hash=?", (time.time() - 1, self.actor["key"]))
        with self.assertRaises(SandboxConflict):
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])

    def test_manual_intent_cannot_bypass_weights_or_cash_reserve(self):
        self.ready()
        with self.assertRaises(ValidationError):
            self.action("preview", {"instrument_uid": "sber-uid", "lots": 2000, "limit_price": 100, "direction": "buy", "ignore_policy": True})
        fake = self.actor["fake"]
        fake.cash = 4_000_000
        fake.positions = [{**fake.catalog_rows[2], "quantity": 600_000, "price": 10, "currency": "RUB", "blocked": False, "available_quantity": 600_000}]
        with self.assertRaises(ValidationError):
            self.action("preview", {"instrument_uid": "sber-uid", "lots": 1000, "limit_price": 100, "direction": "buy"})
        self.assertEqual(fake.posts, [])

    def test_policy_change_revokes_old_proposal_and_versions_rules(self):
        self.ready()
        old_policy = self.public()["policy"]
        proposal = self.propose()
        self.action("policy-preview", {"strategy_id": "reserve"})
        draft = self.public()["policy_draft"]
        self.assertEqual(draft["version"], old_policy["version"] + 1)
        self.action("policy-confirm", {"policy_id": draft["id"]})
        with self.assertRaises(ValidationError):
            self.confirm(proposal)
        self.assertEqual(self.actor["fake"].posts, [])

    def test_sandbox_profile_is_separate_and_change_revokes_rules(self):
        self.ready()
        old = self.public()
        proposal = self.propose()
        self.action("profile", {**DEMO_PROFILE, "goal_name": "Учебные 10 млн", "goal_amount": 10_000_000, "confirmed": True})
        current = self.public()
        self.assertGreater(current["profile_version"], old["profile_version"])
        self.assertTrue(current["stopped"])
        self.assertIsNone(current["policy"])
        with connect(self.database) as conn:
            real = json.loads(conn.execute("SELECT profile FROM users WHERE id=?", (self.actor["owner"],)).fetchone()["profile"])
        self.assertEqual(real, DEMO_PROFILE)
        with self.assertRaises(ValidationError):
            self.confirm(proposal)

    def test_proposal_and_policy_expiry_block_sending(self):
        self.ready()
        proposal = self.propose()
        with connect(self.database) as conn:
            conn.execute("UPDATE sandbox_proposals SET expires_at=? WHERE id=?", (time.time() - 1, proposal["id"]))
        with self.assertRaises(ValidationError):
            self.confirm(proposal)
        policy = self.public()["policy"]
        policy["expires_at"] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.update_state(policy=policy)
        with self.assertRaises(ValidationError):
            self.propose()
        self.assertEqual(self.actor["fake"].posts, [])

    def stored_proposal(self, proposal_id):
        with connect(self.database) as conn:
            return json.loads(conn.execute("SELECT payload FROM sandbox_proposals WHERE id=?", (proposal_id,)).fetchone()["payload"])

    def test_tampered_proposal_quantity_with_same_id_cannot_be_confirmed(self):
        self.ready()
        proposal = self.propose()
        original = self.stored_proposal(proposal["id"])
        fingerprint = original["fingerprint"]
        content = {key: value for key, value in original.items() if key != "fingerprint"}
        self.assertEqual(fingerprint, hashlib.sha256(encode(content).encode()).hexdigest())
        tampered = copy.deepcopy(original)
        tampered["orders"][0]["quantity"] += tampered["orders"][0]["lot"]
        with connect(self.database) as conn:
            conn.execute("UPDATE sandbox_proposals SET payload=? WHERE id=?", (encode(tampered), proposal["id"]))
        with self.assertRaises(ValidationError):
            self.confirm(proposal)
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual(self.public()["orders"], [])
        self.assertEqual(self.stored_proposal(proposal["id"])["fingerprint"], fingerprint)

    def test_tampered_proposal_price_cannot_reuse_existing_confirmation(self):
        self.ready()
        proposal = self.propose()
        self.confirm(proposal)
        original = self.stored_proposal(proposal["id"])
        fingerprint = original["fingerprint"]
        changed = copy.deepcopy(original)
        changed["orders"][0]["limit_price"] -= .01
        with connect(self.database) as conn:
            conn.execute("UPDATE sandbox_proposals SET payload=? WHERE id=?", (encode(changed), proposal["id"]))
        with self.assertRaises(ValidationError):
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])
        self.assertEqual(self.public()["orders"][0]["limit_price"], original["orders"][0]["limit_price"])
        self.assertEqual(self.stored_proposal(proposal["id"])["fingerprint"], fingerprint)

    def test_ledger_quantity_or_price_cannot_change_approved_proposal(self):
        for field in ("quantity", "limit_price"):
            with self.subTest(field=field):
                self.actor = self.add_actor()
                self.ready()
                proposal = self.propose()
                self.confirm(proposal)
                fingerprint = self.stored_proposal(proposal["id"])["fingerprint"]
                request_id = proposal["orders"][0]["id"]
                with connect(self.database) as conn:
                    row = conn.execute("SELECT payload FROM sandbox_orders WHERE id=?", (request_id,)).fetchone()
                    changed = json.loads(row["payload"])
                    changed[field] = changed[field] + changed["lot"] if field == "quantity" else changed[field] - .01
                    conn.execute("UPDATE sandbox_orders SET payload=? WHERE id=?", (encode(changed), request_id))
                with self.assertRaises((ValidationError, SandboxConflict, SandboxError)):
                    self.dispatch()
                self.assertEqual(self.actor["fake"].posts, [])
                self.assertEqual(self.stored_proposal(proposal["id"])["fingerprint"], fingerprint)

    def test_lagging_filled_positions_block_publication_and_new_sends(self):
        self.ready()
        proposal = self.propose(rebalance=True)
        self.confirm(proposal)
        self.dispatch()
        fake = self.actor["fake"]
        first = fake.posts[0]
        lagging = fake.snapshot(proposal["account_id"])
        fake.fill(first)
        published = self.public()["snapshot"]
        with patch.object(fake, "snapshot", return_value=lagging):
            with self.assertRaises(SandboxError) as caught:
                self.action("refresh")
            self.assertEqual(caught.exception.code, "RECONCILIATION")
            self.assertEqual(self.public()["snapshot"], published)
            self.assertTrue(self.public()["reconcile_error"])
            with self.assertRaises(SandboxError):
                self.dispatch()
            self.assertEqual(fake.posts, [first])
        self.action("refresh")
        actual = self.public()
        self.assertIsNone(actual["reconcile_error"])
        self.assertEqual(actual["snapshot"]["positions"], fake.positions)
        replacement = self.propose(rebalance=True)
        self.confirm(replacement)
        self.dispatch()
        self.assertEqual(len(fake.posts), 2)

    def test_stop_during_service_precheck_prevents_post(self):
        self._stop_during_precheck("precheck_hook")

    def test_stop_during_client_precheck_prevents_post_at_final_gate(self):
        self._stop_during_precheck("client_precheck_hook")

    def test_stop_returns_during_admitted_post_and_prevents_next_post(self):
        self.ready()
        proposal = self.propose(rebalance=True)
        self.confirm(proposal)
        entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
        failures = []
        admitted = []
        def blocked_post(request_id):
            admitted.append(request_id)
            entered.set()
            if not release.wait(5):
                raise AssertionError("POST was not released")
        self.actor["fake"].before_post = blocked_post
        def dispatch():
            try:
                self.dispatch()
            except Exception as exc:
                failures.append(exc)
        def stop():
            try:
                self.action("stop")
            except Exception as exc:
                failures.append(exc)
            finally:
                stopped.set()
        sender = threading.Thread(target=dispatch)
        stopper = threading.Thread(target=stop)
        sender.start()
        try:
            self.assertTrue(entered.wait(2))
            stopper.start()
            self.assertTrue(stopped.wait(2), "Stop waited for an already admitted POST")
            self.assertFalse(release.is_set())
            with connect(self.database) as conn:
                persisted = json.loads(conn.execute("SELECT payload FROM sandbox_states WHERE user_id=?", (self.actor["owner"],)).fetchone()["payload"])
            self.assertTrue(persisted["stopped"])
        finally:
            release.set()
            sender.join(4)
            if stopper.ident is not None:
                stopper.join(4)
        self.assertFalse(sender.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(self.actor["fake"].posts, admitted)
        self.assertEqual(len(admitted), 1)
        self.dispatch()
        self.assertEqual(self.actor["fake"].posts, admitted)
        self.assertFalse(any(row["status"] == "draft" for row in self.public()["orders"]))

    def test_public_states_remain_available_while_other_owner_post_waits(self):
        self.ready()
        other = self.add_actor()
        self.ready(actor=other)
        self.confirm(self.propose())
        entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
        failures, states = [], {}
        def blocked_post(request_id):
            entered.set()
            if not release.wait(5):
                raise AssertionError("POST was not released")
        self.actor["fake"].before_post = blocked_post
        def dispatch():
            try:
                self.dispatch()
            except Exception as exc:
                failures.append(exc)
        def read():
            try:
                states["other"] = self.public(other)
                states["sending"] = self.public()
            except Exception as exc:
                failures.append(exc)
            finally:
                read_done.set()
        sender = threading.Thread(target=dispatch)
        reader = threading.Thread(target=read)
        sender.start()
        try:
            self.assertTrue(entered.wait(2))
            reader.start()
            self.assertTrue(read_done.wait(2), "Public state waited for another owner's network request")
            self.assertFalse(release.is_set())
            self.assertTrue(states["other"]["connection"]["connected"])
            self.assertFalse(states["other"]["connection"]["busy"])
            self.assertTrue(states["sending"]["connection"]["busy"])
            self.assertNotEqual(states["other"]["account"]["id"], states["sending"]["account"]["id"])
        finally:
            release.set()
            sender.join(4)
            if reader.ident is not None:
                reader.join(4)
        self.assertFalse(sender.is_alive())
        self.assertFalse(reader.is_alive())
        self.assertEqual(failures, [])

    def _stop_during_precheck(self, field):
        self.ready()
        self.confirm(self.propose())
        started, release = threading.Event(), threading.Event()
        failures, stop_done = [], threading.Event()
        def block():
            started.set()
            if not release.wait(3):
                raise AssertionError("precheck did not resume")
        setattr(self.actor["fake"], field, block)
        def dispatch():
            try:
                self.dispatch()
            except Exception as exc:
                failures.append(exc)
        worker = threading.Thread(target=dispatch)
        worker.start()
        self.assertTrue(started.wait(3))
        def stop():
            try:
                self.action("stop")
            except Exception as exc:
                failures.append(exc)
            finally:
                stop_done.set()
        stopper = threading.Thread(target=stop)
        stopper.start()
        stopped_before_release = stop_done.wait(2)
        try:
            self.assertTrue(stopped_before_release, "Stop waited for a broker precheck instead of closing admission")
        finally:
            release.set()
        worker.join(4)
        stopper.join(4)
        self.assertFalse(worker.is_alive())
        self.assertFalse(stopper.is_alive())
        self.assertTrue(all(isinstance(exc, (ValidationError, SandboxConflict, BrokerSessionError, SandboxError)) for exc in failures), failures)
        self.assertTrue(self.public()["stopped"])
        self.assertEqual(self.actor["fake"].posts, [])

    def test_restart_preserves_stop_and_historical_request_ids(self):
        self.ready()
        proposal = self.propose()
        self.confirm(proposal)
        request_id = proposal["orders"][0]["id"]
        self.action("stop")
        self.service.close()
        self.vault.close()
        self.vault = BrokerSessions()
        self.service = SandboxService(self.database, self.vault, self.factory, start_worker=False)
        result = self.public()
        self.assertTrue(result["stopped"])
        self.assertFalse(result["connection"]["connected"])
        self.assertIn(request_id, [row["id"] for row in result["orders"]])
        with self.assertRaises(BrokerSessionError):
            self.dispatch()
        self.assertEqual(self.actor["fake"].posts, [])

    def test_no_broker_secret_or_raw_session_is_in_database_or_public_state(self):
        self.ready()
        self.confirm(self.propose())
        self.dispatch()
        output = json.dumps(self.public(), ensure_ascii=False)
        with connect(self.database) as conn:
            output += "\n".join(conn.iterdump())
        secret, raw_session = self.actor_secrets[self.actor["owner"]]
        self.assertNotIn(secret, output)
        self.assertNotIn(raw_session, output)

    def ledger_record(self, proposal, status="new"):
        request_id = str(uuid.uuid4())
        plan = proposal["orders"][0]
        record = {**plan, "id": request_id, "request_id": request_id, "order_id": "remote-" + request_id,
                  "status": status, "lots_requested": plan["lots"], "lots_executed": plan["lots"] if status == "filled" else 0}
        with self.service.store.transaction() as conn:
            conn.execute("INSERT INTO sandbox_orders VALUES(?,?,?,?,?,?,?)", (request_id, self.actor["owner"], proposal["id"], proposal["account_id"], encode(record), status, now()))
        return record

    def test_ledger_does_not_reverse_terminal_statuses(self):
        self.ready()
        proposal = self.propose()
        self.confirm(proposal)
        for previous, incoming in (("rejected", "filled"), ("cancelled", "filled"), ("filled", "new"), ("filled", "rejected"), ("blocked", "filled")):
            with self.subTest(previous=previous, incoming=incoming):
                record = self.ledger_record(proposal, previous)
                result = {**record, "status": incoming, "lots_executed": record["lots"] if incoming == "filled" else 0}
                with self.service.store.transaction() as conn:
                    self.service.store.order_result(conn, self.actor["owner"], record["id"], result)
                actual = next(row for row in self.public()["orders"] if row["id"] == record["id"])
                self.assertEqual(actual["status"], previous)
                self.assertEqual(actual["lots_executed"], record["lots_executed"])

    def test_wrong_instrument_lots_or_direction_becomes_unknown_with_audit(self):
        self.ready()
        proposal = self.propose()
        self.confirm(proposal)
        for changed in ({"instrument_uid": "other-instrument"}, {"lots_requested": proposal["orders"][0]["lots"] + 1}, {"direction": "sell"}):
            with self.subTest(changed=changed):
                record = self.ledger_record(proposal)
                result = {**record, "status": "filled", "lots_executed": record["lots"], **changed}
                with self.service.store.transaction() as conn:
                    self.service.store.order_result(conn, self.actor["owner"], record["id"], result)
                actual = next(row for row in self.public()["orders"] if row["id"] == record["id"])
                self.assertEqual(actual["status"], "unknown")
                self.assertEqual(actual["instrument_uid"], record["instrument_uid"])
                self.assertEqual(actual["lots_requested"], record["lots_requested"])
                self.assertEqual(actual["direction"], record["direction"])
                with connect(self.database) as conn:
                    rows = conn.execute("SELECT kind,payload FROM sandbox_audit WHERE user_id=?", (self.actor["owner"],)).fetchall()
                self.assertTrue(any("mismatch" in row["kind"] and record["id"] in row["payload"] for row in rows))


class PortfolioPolicyTests(unittest.TestCase):
    def profile(self, **changes):
        return {**DEMO_PROFILE, "version": 1, **changes}

    def test_rules_expire_after_thirty_days_and_keep_explicit_reserve(self):
        policy = make_policy(self.profile(), "balanced", "sandbox-account", 1)
        self.assertEqual(datetime.fromisoformat(policy["expires_at"]) - datetime.fromisoformat(policy["created_at"]), timedelta(days=30))
        self.assertEqual(policy["cash_reserve_percent"], 40)
        self.assertEqual(sum(policy["weights"].values()), 100)
        self.assertEqual(policy["environment"], "sandbox")

    def test_risk_gates_reject_growth_and_global_capacity_failures(self):
        for changes in ({"expensive_debt": True}, {"reserve_months": 2}, {"excluded_sectors": ["Нефтегаз"]}):
            self.assertTrue(all(not row["available"] for row in strategies(self.profile(**changes))))
        for changes in ({"risk_tolerance": "medium"}, {"experience": "none", "risk_tolerance": "high"}, {"loss_reaction": "sell", "risk_tolerance": "high"}, {"horizon_years": 4, "risk_tolerance": "high"}):
            with self.assertRaises(ValidationError):
                make_policy(self.profile(**changes), "growth", "sandbox-account", 1)
        self.assertEqual(make_policy(self.profile(risk_tolerance="high"), "growth", "sandbox-account", 1)["cash_reserve_percent"], 10)

    def test_rebalancing_uses_whole_lots_and_preserves_target_cash(self):
        fake = FakeSandbox("unused-fixture-secret")
        policy = make_policy(self.profile(), "balanced", "sandbox-account", 1)
        snapshot = {"positions": [], "cash": 10_000_000, "total_value": 10_000_000}
        requests = rebalance_orders(policy, snapshot, fake.catalog())
        cost = sum(Decimal(str(row["limit_price"])) * row["quantity"] * Decimal("1.0055") for row in requests)
        self.assertLessEqual(cost, Decimal(6_000_000))
        self.assertTrue(all(row["quantity"] == row["lots"] * row["lot"] and isinstance(row["lots"], int) for row in requests))
        self.assertEqual({row["ticker"] for row in requests}, {"SBER", "LKOH", "TMOS", "TGLD"})

    def test_demo_policy_liquidates_only_known_positions_without_fabricated_profile(self):
        config = validate_demo_config({"strategy_id": "reserve", "goal_name": "Цель", "target_amount": 10_000_000,
                                       "horizon_years": 1, "monthly_contribution": 0})
        policy = make_demo_policy(config, "sandbox-account", 1, 1)
        fake = FakeSandbox("unused-fixture-secret")
        sber = next(row for row in fake.catalog() if row["ticker"] == "SBER")
        snapshot = {"positions": [{**sber, "quantity": 20_000}], "cash": 8_000_000, "total_value": 10_000_000}
        requests = rebalance_orders(policy, snapshot, fake.catalog())
        sale = next(row for row in requests if row["ticker"] == "SBER")
        self.assertEqual(sale["direction"], "sell")
        self.assertLessEqual(sale["quantity"], 20_000)
        self.assertEqual(policy["max_weights"]["SBER"], 0)
        self.assertNotIn("profile", policy)
        snapshot["positions"][0]["ticker"] = "UNKNOWN"
        with self.assertRaises(ValidationError):
            rebalance_orders(policy, snapshot, fake.catalog())


if __name__ == "__main__":
    unittest.main()
