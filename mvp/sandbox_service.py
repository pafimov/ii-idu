import json
import hashlib
import hmac
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable
from broker_gateway import SandboxGateway
from datetime import datetime, timezone
from decimal import Decimal

from broker_sessions import BrokerSessionError
from engine import ValidationError, validate_profile
from portfolio_policy import demo_strategies, insights, make_demo_policy, make_policy, rebalance_orders, strategies, validate_demo_config
from sandbox import SandboxError
from sandbox_store import SandboxConflict, SandboxStore, encode, now


TERMINAL = {"filled", "cancelled", "rejected", "blocked"}
ACTIVE = {"submitting", "new", "partially_filled", "unknown"}


class SandboxService:
    def __init__(self, database, vault, factory: Callable[[str], SandboxGateway], start_worker=True):
        self.store = SandboxStore(database)
        self.vault = vault
        self.factory = factory
        self.locks = {}
        self.locks_guard = threading.Lock()
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.last_refresh = {}
        self.refresh_pending = {}
        self.worker = threading.Thread(target=self.run, name="sandbox-execution", daemon=True)
        if start_worker:
            self.worker.start()

    def lock(self, owner):
        with self.locks_guard:
            return self.locks.setdefault(owner, threading.RLock())

    def close(self):
        self.closed.set()
        self.wake.set()
        if self.worker.is_alive():
            self.worker.join(timeout=2)

    def public_state(self, conn, user, key):
        owner = user["id"]
        data = self.store.load(conn, owner)
        connection = self.vault.status(owner, key)
        connection.update(read_only=False, environment="sandbox")
        profile = self.profile(user, data)
        result = {**data, "connection": connection, "strategies": strategies(profile), "demo_strategies": demo_strategies(),
                  "orders": self.store.orders(conn, owner, data["account"]["id"] if data["account"] else None), "proposal": None}
        if data["proposal_id"]:
            row = conn.execute("SELECT * FROM sandbox_proposals WHERE id=? AND user_id=?", (data["proposal_id"], owner)).fetchone()
            if row and not row["revoked"]:
                proposal = json.loads(row["payload"])
                proposal["confirmed_at"] = row["confirmed_at"]
                proposal["expired"] = row["expires_at"] <= time.time()
                result["proposal"] = proposal
        if data["snapshot"]:
            result["insights"] = insights(data["snapshot"], data["policy"])
        else:
            result["insights"] = []
        try:
            result["snapshot_reconciled"] = bool(data["account"] and data["snapshot"] and not data.get("reconcile_error") and self.positions_match(conn, owner, data["account"]["id"], data["snapshot"]))
        except ValidationError:
            result["snapshot_reconciled"] = False
        return result

    def report(self, conn, owner):
        rows = conn.execute("SELECT id,kind,payload,created_at FROM sandbox_audit WHERE user_id=? ORDER BY id", (owner,)).fetchall()
        return {"generated_at": now(), "environment": "sandbox", "events": [{"id": row["id"], "kind": row["kind"], "data": json.loads(row["payload"]), "created_at": row["created_at"]} for row in rows]}

    @staticmethod
    def profile(user, data):
        return {**data["profile"], "version": data["profile_version"]} if data.get("profile") else None

    def read(self, owner, key):
        with self.store.transaction() as conn:
            user = self.store.authenticate(conn, owner, key)
            return self.store.load(conn, owner), user

    def current_account(self, data, entry):
        account = data.get("account")
        if not account or not any(a["id"] == account["id"] for a in entry["accounts"]):
            raise ValidationError("Выберите доступный счёт песочницы.")
        return account

    @staticmethod
    def require_settled_actions(conn, owner):
        if conn.execute("SELECT 1 FROM sandbox_actions WHERE user_id=? AND status IN ('opening','funding','unknown','resetting')", (owner,)).fetchone():
            raise SandboxConflict("Результат создания или пополнения ещё уточняется. Заявки пока недоступны; повторное пополнение не отправлено.")

    def invalidate(self, conn, owner, data, reason):
        conn.execute("UPDATE sandbox_proposals SET revoked=1 WHERE user_id=?", (owner,))
        conn.execute("UPDATE sandbox_orders SET status='cancelled' WHERE user_id=? AND status='draft'", (owner,))
        data.update(proposal_id=None, version=data["version"] + 1)
        self.store.audit(conn, owner, reason, {"account": data["account"], "version": data["version"]})

    def profile_review(self, conn, owner):
        data = self.store.load(conn, owner)
        self.invalidate(conn, owner, data, "profile_review")
        data.update(stopped=True, policy=None, policy_draft=None)
        self.store.save(conn, owner, data)
        self.wake.set()

    def action(self, owner, key, command, payload):
        if command != "connect":
            try:
                self.vault.touch(owner, key)
            except BrokerSessionError:
                pass
        if command == "connect":
            return self.connect(owner, key, payload)
        if command in ("stop", "profile-review", "disconnect"):
            with self.store.transaction() as conn:
                self.store.authenticate(conn, owner, key)
                data = self.store.load(conn, owner)
                self.invalidate(conn, owner, data, command)
                data["stopped"] = True
                if command == "profile-review":
                    data.update(policy=None, policy_draft=None)
                self.store.save(conn, owner, data)
            if command == "disconnect":
                self.vault.disconnect(owner, key)
            self.wake.set()
            return
        if command == "create":
            return self.create(owner, key, payload)
        if command == "reset-demo":
            return self.reset_demo(owner, key, payload)
        if command == "demo-preview":
            return self.preview(owner, key, {}, True, demo_config=validate_demo_config(payload))
        if command in ("select", "refresh"):
            return self.refresh(owner, key, payload.get("account_id") if command == "select" else None)
        if command in ("profile", "use-profile", "policy-preview", "policy-confirm", "resume", "execute"):
            return self.policy_action(owner, key, command, payload)
        if command in ("preview-demo", "preview"):
            return self.preview(owner, key, payload, command == "preview-demo")
        if command == "cancel":
            return self.cancel(owner, key, payload)
        raise ValidationError("Действие не найдено.")

    def connect(self, owner, key, payload):
        entry = self.vault.prepare(owner, key, self.factory(payload.get("token")))
        try:
            accounts = entry["client"].accounts()
            with self.store.transaction() as conn, self.vault.accept(entry):
                self.store.authenticate(conn, owner, key)
                entry.update(accounts=accounts, ready=True, busy=False)
                self.store.audit(conn, owner, "connected", {"account_count": len(accounts)})
                conn.commit()
            self.wake.set()
        except Exception:
            self.vault.discard(entry)
            raise

    def create(self, owner, key, payload, entry=None):
        action_key = payload.get("key")
        try:
            uuid.UUID(action_key)
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("Не хватает идентификатора создания счёта.") from None
        with self.lock(owner), self.store.transaction() as conn:
            self.store.authenticate(conn, owner, key)
            data = self.store.load(conn, owner)
            previous = conn.execute("SELECT * FROM sandbox_actions WHERE user_id=? AND key=?", (owner, action_key)).fetchone()
            if previous:
                if previous["kind"] != "create":
                    raise SandboxConflict("Этот идентификатор уже использован для другого действия.")
                if previous["status"] == "complete":
                    return
                raise SandboxConflict("Создание уже запрашивалось. Обновите список счетов, повтор не отправлен.")
            if conn.execute("SELECT 1 FROM sandbox_actions WHERE user_id=? AND kind='create' AND status IN ('opening','funding','unknown')", (owner,)).fetchone():
                raise SandboxConflict("Результат создания ещё уточняется. Обновите подключение и выберите уже созданный счёт.")
            if data["account"]:
                raise SandboxConflict("Счёт уже выбран. В этой версии используется один счёт.")
            conn.execute("INSERT INTO sandbox_actions VALUES(?,?,?,'opening',NULL)", (owner, action_key, "create"))
        own_entry = entry is None
        entry = entry or self.vault.begin(owner, key)
        try:
            with self.lock(owner), self.vault.send_guard(entry):
                account_id = entry["client"].open_account("ИИ-ИДУ")
            with self.store.transaction() as conn, self.vault.accept(entry):
                self.store.authenticate(conn, owner, key)
                data = self.store.load(conn, owner)
                data["account"] = {"id": account_id, "name": "ИИ-ИДУ"}
                self.store.save(conn, owner, data)
                conn.execute("UPDATE sandbox_actions SET account_id=?,status='funding' WHERE user_id=? AND key=?", (account_id, owner, action_key))
                self.store.audit(conn, owner, "account_created", {"account_id": account_id})
                conn.commit()
            with self.lock(owner), self.vault.send_guard(entry):
                result = entry["client"].pay_in(account_id, 10_000_000)
            accounts = entry["client"].accounts()
            snapshot = entry["client"].snapshot(account_id)
            catalog = entry["client"].catalog()
            if snapshot.get("positions") or snapshot.get("cash_blocked", 0) or abs(Decimal(str(snapshot.get("cash", 0))) - Decimal(10_000_000)) > Decimal("0.01") or abs(Decimal(str(snapshot.get("total_value", 0))) - Decimal(10_000_000)) > Decimal("0.01") or abs(Decimal(str(result.get("balance", 0))) - Decimal(10_000_000)) > Decimal("0.01"):
                raise SandboxError("RECONCILIATION", "Баланс нового счёта не подтверждён. Повторное пополнение не отправлено.")
            with self.store.transaction() as conn, self.vault.accept(entry):
                self.store.authenticate(conn, owner, key)
                data = self.store.load(conn, owner)
                data.update(snapshot=snapshot, catalog=catalog, checked_at=now(), reconcile_error=None)
                self.store.save(conn, owner, data)
                conn.execute("UPDATE sandbox_actions SET status='complete' WHERE user_id=? AND key=?", (owner, action_key))
                self.store.audit(conn, owner, "funded", {"account_id": account_id, "amount": 10_000_000, "response": result})
                entry.update(accounts=accounts, account_id=account_id, account_name="ИИ-ИДУ")
                conn.commit()
            self.last_refresh[(owner, key)] = time.monotonic()
            self.refresh_pending.pop((owner, key), None)
        except Exception:
            with self.store.transaction() as conn:
                conn.execute("UPDATE sandbox_actions SET status='unknown' WHERE user_id=? AND key=? AND status!='complete'", (owner, action_key))
            raise
        finally:
            if own_entry:
                self.vault.finish(entry)

    def reset_demo(self, owner, key, payload):
        reset_key = payload.get("key")
        try:
            parsed_key = uuid.UUID(reset_key)
        except (ValueError, TypeError, AttributeError):
            raise ValidationError("Не хватает идентификатора нового старта.") from None
        reset_key = str(parsed_key)
        with self.lock(owner):
            with self.store.transaction() as conn:
                self.store.authenticate(conn, owner, key)
                previous = conn.execute("SELECT * FROM sandbox_actions WHERE user_id=? AND key=?", (owner, reset_key)).fetchone()
                if previous:
                    if previous["kind"] == "reset" and previous["status"] == "complete":
                        return
                    raise SandboxConflict("Новый старт уже запрашивался. Повторное создание не отправлено.")
            entry = self.vault.begin(owner, key)
            try:
                with self.store.transaction() as conn, self.vault.accept(entry):
                    self.store.authenticate(conn, owner, key)
                    data = self.store.load(conn, owner)
                    if any(order["status"] in ACTIVE for order in self.store.orders(conn, owner)):
                        raise SandboxConflict("Сначала нужно уточнить результат активных заявок.")
                    if conn.execute("SELECT 1 FROM sandbox_actions WHERE user_id=? AND status IN ('opening','funding','unknown','resetting')", (owner,)).fetchone():
                        raise SandboxConflict("Результат создания или пополнения ещё уточняется. Новый счёт не создан.")
                    old_account = data["account"]
                    conn.execute("INSERT INTO sandbox_actions VALUES(?,?,?,'resetting',NULL)", (owner, reset_key, "reset"))
                    self.invalidate(conn, owner, data, "demo_reset")
                    data.update(account=None, snapshot=None, catalog=[], policy=None, policy_draft=None,
                                demo_config=None, demo_config_version=data["demo_config_version"] + 1,
                                checked_at=None, reconcile_error=None, stopped=True)
                    self.store.save(conn, owner, data)
                    self.store.audit(conn, owner, "demo_reset_started", {"key": reset_key, "old_account": old_account})
                    entry.update(account_id=None, account_name=None)
                    conn.commit()
                create_key = str(uuid.uuid5(parsed_key, "sandbox-demo-create"))
                self.create(owner, key, {"key": create_key}, entry=entry)
                with self.store.transaction() as conn, self.vault.accept(entry):
                    self.store.authenticate(conn, owner, key)
                    data = self.store.load(conn, owner)
                    conn.execute("UPDATE sandbox_actions SET status='complete',account_id=? WHERE user_id=? AND key=?", (data["account"]["id"], owner, reset_key))
                    self.store.audit(conn, owner, "demo_reset_completed", {"key": reset_key, "account": data["account"]})
                    conn.commit()
            except Exception:
                with self.store.transaction() as conn:
                    conn.execute("UPDATE sandbox_actions SET status='unknown' WHERE user_id=? AND key=? AND status!='complete'", (owner, reset_key))
                raise
            finally:
                self.vault.finish(entry)
        self.wake.set()

    def refresh(self, owner, key, requested_account=None, background=False):
        entry = self.vault.begin(owner, key, touch=not background)
        try:
            data, user = self.read(owner, key)
            origin_account, origin_version = data["account"], data["version"]
            if requested_account:
                account = next((a for a in entry["accounts"] if a["id"] == requested_account), None)
                if not account:
                    raise ValidationError("Счёт не найден в этом подключении.")
                if data["account"] and data["account"]["id"] != requested_account:
                    raise SandboxConflict("Счёт уже выбран. Второй счёт не подключается.")
            else:
                account = self.current_account(data, entry)
            self.reconcile(owner, key, entry, account["id"])
            snapshot = entry["client"].snapshot(account["id"])
            catalog = entry["client"].catalog()
            self.reconcile_positions(owner, account["id"], snapshot)
            with self.store.transaction() as conn, self.vault.accept(entry):
                self.store.authenticate(conn, owner, key)
                data = self.store.load(conn, owner)
                if data["account"] != origin_account or data["version"] != origin_version:
                    raise SandboxConflict("Выбранный счёт изменился во время обновления. Прежний снимок не сохранён.")
                if not data["account"] and conn.execute("SELECT 1 FROM sandbox_actions WHERE user_id=? AND kind='reset' AND status IN ('resetting','unknown')", (owner,)).fetchone():
                    raise SandboxConflict("Новый старт ещё не завершён. Прежний счёт не подключён.")
                data.update(account={"id": account["id"], "name": account["name"]}, snapshot=snapshot,
                            catalog=catalog, checked_at=now(), reconcile_error=None)
                self.store.save(conn, owner, data)
                entry.update(account_id=account["id"], account_name=account["name"])
                if requested_account:
                    conn.execute("UPDATE sandbox_actions SET status='resolved' WHERE user_id=? AND kind='create' AND account_id IS NULL AND status='unknown'", (owner,))
                conn.commit()
            self.last_refresh[(owner, key)] = time.monotonic()
            self.refresh_pending.pop((owner, key), None)
        except (SandboxError, ValidationError) as exc:
            try:
                with self.store.transaction() as conn, self.vault.accept(entry):
                    self.store.authenticate(conn, owner, key)
                    data = self.store.load(conn, owner)
                    if data["account"] != origin_account or data["version"] != origin_version:
                        raise SandboxConflict("Счёт изменился; ошибка прежнего обновления не сохранена.")
                    data["reconcile_error"] = str(exc)
                    self.store.save(conn, owner, data)
                    conn.commit()
            except (SandboxConflict, BrokerSessionError):
                pass
            raise
        finally:
            self.vault.finish(entry)

    def reconcile(self, owner, key, entry, account_id):
        with self.store.transaction() as conn:
            orders = self.store.orders(conn, owner, account_id)
        for order in orders:
            if order["status"] not in ACTIVE:
                continue
            result = entry["client"].order_state(account_id, order["id"], request=True)
            with self.store.transaction() as conn:
                valid = self.store.order_result(conn, owner, order["id"], result)
                if valid is not False and result.get("status") == "filled":
                    self.refresh_pending.setdefault((owner, key), 3)
                if result.get("status") in ("cancelled", "rejected"):
                    conn.execute("UPDATE sandbox_proposals SET revoked=1 WHERE id=?", (order["proposal_id"],))
                    conn.execute("UPDATE sandbox_orders SET status='cancelled' WHERE proposal_id=? AND status='draft'", (order["proposal_id"],))
            if valid is False:
                raise SandboxError("RECONCILIATION", "Реквизиты результата заявки не совпали.")

    def positions_match(self, conn, owner, account_id, snapshot):
        if snapshot.get("account_id") != account_id:
            return False
        proposal = conn.execute("SELECT * FROM sandbox_proposals WHERE user_id=? AND account_id=? AND confirmed_at IS NOT NULL ORDER BY confirmed_at DESC,rowid DESC LIMIT 1", (owner, account_id)).fetchone()
        if not proposal:
            return True
        baseline = self.checked_proposal(proposal, require_live=False)["source_snapshot"]
        orders = [o for o in self.store.orders(conn, owner, account_id) if o["proposal_id"] == proposal["id"]]
        if any(o["status"] in ("unknown", "submitting") for o in orders):
            return False
        expected = {p["instrument_uid"]: Decimal(str(p["quantity"])) for p in baseline["positions"] if p.get("asset_class") != "cash"}
        for order in orders:
            quantity = Decimal(str(order.get("lots_executed", 0) or 0)) * Decimal(str(order["lot"]))
            expected[order["instrument_uid"]] = expected.get(order["instrument_uid"], Decimal(0)) + (quantity if order["direction"] == "buy" else -quantity)
        actual = {p["instrument_uid"]: Decimal(str(p["quantity"])) for p in snapshot["positions"] if p.get("asset_class") != "cash"}
        return {k:v for k,v in expected.items() if v} == {k:v for k,v in actual.items() if v}

    def reconcile_positions(self, owner, account_id, snapshot):
        with self.store.transaction() as conn:
            matches = self.positions_match(conn, owner, account_id, snapshot)
        if not matches:
            raise SandboxError("RECONCILIATION", "Позиции ещё не совпали с подтверждёнными исполнениями. Новые заявки заблокированы до сверки.")

    @staticmethod
    def checked_proposal(row, require_live=True):
        payload = json.loads(row["payload"])
        fingerprint = payload.get("fingerprint")
        content = {key: value for key, value in payload.items() if key != "fingerprint"}
        expected = hashlib.sha256(encode(content).encode()).hexdigest()
        if not isinstance(fingerprint, str) or not hmac.compare_digest(fingerprint, expected):
            raise ValidationError("Состав предложения изменился. Рассчитайте новое предложение.")
        if require_live and datetime.fromisoformat(payload["expires_at"]).timestamp() <= time.time():
            raise ValidationError("Срок предложения истёк. Рассчитайте новое предложение.")
        return payload

    def approved_order(self, proposal, order, policy=None):
        payload = self.checked_proposal(proposal)
        if policy is not None and payload["policy"] != policy:
            raise ValidationError("Правила изменились после подтверждения. Рассчитайте предложение заново.")
        approved = next((item for item in payload["orders"] if item["id"] == order["id"]), None)
        if not approved or any(order.get(field) != value for field, value in approved.items()):
            raise ValidationError("Параметры заявки не совпали с подтверждённым предложением.")
        return approved

    def require_policy(self, data, user):
        policy = data.get("policy")
        if not policy:
            raise ValidationError("Сначала выберите и подтвердите правила портфеля.")
        if policy.get("mode") == "demo":
            if not data.get("demo_config") or policy.get("config_version") != data["demo_config_version"] or policy.get("demo_config") != data["demo_config"]:
                raise ValidationError("Настройки демонстрации изменились. Рассчитайте предложение заново.")
        elif not data.get("profile") or policy["profile_version"] != data["profile_version"]:
            raise ValidationError("Сначала выберите и подтвердите правила портфеля.")
        if datetime.fromisoformat(policy["expires_at"]).timestamp() <= time.time():
            raise ValidationError("Срок правил истёк. Подтвердите новые правила.")
        if not data["account"] or policy["account_id"] != data["account"]["id"]:
            raise ValidationError("Правила относятся к другому счёту.")
        return policy

    def policy_action(self, owner, key, command, payload):
        with self.lock(owner), self.store.transaction() as conn:
            user = self.store.authenticate(conn, owner, key)
            data = self.store.load(conn, owner)
            if command in ("profile", "use-profile"):
                profile_input = payload if command == "profile" else ({**json.loads(user["profile"]), "confirmed": True} if user["profile"] else {})
                profile = validate_profile(profile_input)
                self.profile_review(conn, owner)
                data = self.store.load(conn, owner)
                data.update(profile=profile, profile_version=data["profile_version"] + 1)
                self.store.audit(conn, owner, "profile_confirmed", {"profile": profile, "version": data["profile_version"], "confirmed_at": now()})
            elif command == "policy-preview":
                if not data["account"]:
                    raise ValidationError("Сначала выберите счёт.")
                profile = self.profile(user, data)
                version = (data.get("policy") or data.get("policy_draft") or {}).get("version", 0) + 1
                policy = make_policy(profile, payload.get("strategy_id"), data["account"]["id"], version)
                quote_policy = payload.get("quote_policy", "sandbox_last_close")
                if quote_policy not in ("strict_30s", "sandbox_last_close"):
                    raise ValidationError("Выберите режим цены.")
                policy["quote_policy"] = quote_policy
                data["policy_draft"] = policy
                self.store.audit(conn, owner, "policy_proposed", policy)
            elif command == "policy-confirm":
                policy = data["policy_draft"]
                if not policy or policy["id"] != payload.get("policy_id") or policy["profile_version"] != data["profile_version"]:
                    raise ValidationError("Правила изменились. Рассчитайте их заново.")
                self.invalidate(conn, owner, data, "policy_changed")
                data.update(policy=policy, policy_draft=None)
                self.store.audit(conn, owner, "policy_confirmed", policy)
            elif command == "resume":
                self.require_policy(data, user)
                data["stopped"] = False
                self.store.audit(conn, owner, "resumed", {"policy_id": data["policy"]["id"]})
            else:
                self.require_settled_actions(conn, owner)
                policy = self.require_policy(data, user)
                if data["stopped"]:
                    raise SandboxConflict("Сначала возобновите управление.")
                proposal = conn.execute("SELECT * FROM sandbox_proposals WHERE id=? AND user_id=?", (payload.get("proposal_id"), owner)).fetchone()
                if not proposal or proposal["revoked"] or proposal["expires_at"] <= time.time():
                    raise ValidationError("Предложение недействительно. Рассчитайте новое.")
                plan = self.checked_proposal(proposal)
                demo_changed = policy.get("mode") == "demo" and (plan.get("config_version") != data["demo_config_version"] or plan.get("demo_config") != data["demo_config"] or plan["policy"]["weights"] != policy["weights"])
                profile_changed = policy.get("mode") != "demo" and plan["profile_version"] != data["profile_version"]
                if plan["policy_id"] != policy["id"] or plan["policy"] != policy or demo_changed or profile_changed or proposal["account_id"] != data["account"]["id"]:
                    raise ValidationError("Профиль, счёт или правила изменились.")
                if plan.get("commission_status") != "known":
                    raise ValidationError("Комиссия не подтверждена. Отправка недоступна.")
                if proposal["confirmed_at"]:
                    return
                conn.execute("UPDATE sandbox_proposals SET confirmed_at=? WHERE id=?", (now(), proposal["id"]))
                for sequence, original in enumerate(plan["orders"]):
                    order = {**original, "sequence": sequence}
                    conn.execute("INSERT INTO sandbox_orders VALUES(?,?,?,?,?,'draft',?)", (order["id"], owner, proposal["id"], data["account"]["id"], encode(order), now()))
                self.store.audit(conn, owner, "series_confirmed", {"proposal": plan, "profile": self.profile(user, data), "policy": policy})
            self.store.save(conn, owner, data)
        self.wake.set()

    def check_allocation(self, policy, snapshot, plan):
        total = Decimal(str(snapshot["total_value"]))
        if total <= 0:
            raise ValidationError("В счёте нет денег для операций.")
        ticker = plan["ticker"]
        if ticker not in policy["weights"] or ticker == "RUB":
            raise ValidationError("Инструмент не входит в правила.")
        if plan["direction"] == "buy":
            remainder = Decimal(str(snapshot["cash"])) - Decimal(str(plan["estimated_total"]))
            reserve = total * Decimal(policy["cash_reserve_percent"]) / 100
            if remainder < reserve:
                raise ValidationError("После покупки не останется согласованного резерва денег.")
            held = sum(Decimal(str(p["quantity"])) * Decimal(str(p["price"])) for p in snapshot["positions"] if p["ticker"] == ticker)
            if (held + Decimal(str(plan["notional"]))) / total * 100 > Decimal(policy["max_weights"][ticker]):
                raise ValidationError("Покупка превысит предел доли " + ticker + ".")

    def preview(self, owner, key, payload, rebalance, background=False, demo_config=None):
        entry = self.vault.begin(owner, key, touch=not background)
        try:
            original_data, user = self.read(owner, key)
            data = dict(original_data)
            if demo_config is not None:
                account = self.current_account(data, entry)
                version = (data.get("policy") or data.get("policy_draft") or {}).get("version", 0) + 1
                config_version = data["demo_config_version"] + 1
                policy = make_demo_policy(demo_config, account["id"], version, config_version)
                data.update(policy=policy, policy_draft=None, demo_config=demo_config,
                            demo_config_version=config_version, stopped=False)
            policy = self.require_policy(data, user)
            if data["stopped"]:
                raise SandboxConflict("Сначала возобновите управление.")
            account = self.current_account(data, entry)
            with self.store.transaction() as conn:
                self.require_settled_actions(conn, owner)
                existing = self.store.orders(conn, owner, account["id"])
                if any(o["status"] in ACTIVE or o["status"] == "draft" and demo_config is None for o in existing):
                    raise SandboxConflict("Сначала дождитесь результата текущей серии или остановите её.")
            snapshot = entry["client"].snapshot(account["id"])
            self.reconcile_positions(owner, account["id"], snapshot)
            catalog = entry["client"].catalog()
            if rebalance:
                requests = rebalance_orders(policy, snapshot, catalog)
                if any(p["direction"] == "sell" for p in requests):
                    requests = [p for p in requests if p["direction"] == "sell"]
            else:
                requests = [{"instrument_uid": payload.get("instrument_uid"), "lots": payload.get("lots"), "limit_price": payload.get("limit_price"), "direction": payload.get("direction", "buy")}]
            if not requests:
                raise ValidationError("Доли в пределах правил или суммы недостаточно для целого лота.")
            plans, skipped = [], []
            projected = dict(snapshot)
            projected["positions"] = [dict(p) for p in snapshot["positions"]]
            for request in requests:
                try:
                    plan = entry["client"].preview_order(account["id"], request["instrument_uid"], request["lots"], request["limit_price"], request["direction"], quote_policy=policy["quote_policy"])
                except SandboxError as exc:
                    if not rebalance or exc.code != "TRADING_CLOSED":
                        raise
                    # Закрытые торги по одной бумаге не должны блокировать всю корзину:
                    # сумма остаётся в рублях и показывается пользователю до подтверждения.
                    skipped.append({"ticker": request.get("ticker"), "name": request.get("name"), "direction": request["direction"],
                                    "amount": round(float(Decimal(str(request["limit_price"])) * Decimal(str(request.get("quantity", 0)))), 2),
                                    "reason": "Торги по инструменту сейчас закрыты. Сумма остаётся в рублях; пересчитайте покупки, когда торги откроются."})
                    continue
                self.check_allocation(policy, projected, plan)
                plan["id"] = str(uuid.uuid4())
                plans.append(plan)
                if plan["direction"] == "buy":
                    projected["cash"] -= plan["estimated_total"]
            if not plans:
                names = ", ".join(item["ticker"] or "?" for item in skipped)
                raise ValidationError("Торги по бумагам (" + names + ") сейчас закрыты, покупать пока нечего. Повторите расчёт, когда торги откроются.")
            total = round(sum(p["estimated_total"] for p in plans), 2)
            skip_note = (" Не куплено из-за закрытых торгов: " + "; ".join(f"{s['ticker']} (≈ {s['amount']:,.0f} ₽ остаются в рублях)".replace(",", " ") for s in skipped) + ". Когда торги откроются, пересчитайте покупки.") if skipped else ""
            is_demo = policy.get("mode") == "demo"
            proposal = {"id": str(uuid.uuid4()), "account_id": account["id"], "policy_id": policy["id"], "profile_version": None if is_demo else data["profile_version"],
                        "orders": plans, "total": total, "commission": round(sum(p["commission"] for p in plans), 2), "commission_status": "known",
                        "reserve": policy["cash_reserve_percent"], "kind": "rebalance" if rebalance else "order", "created_at": now(),
                        "expires_at": datetime.fromtimestamp(time.time() + 600, timezone.utc).isoformat(timespec="seconds"),
                        "quote_policy": policy["quote_policy"], "skipped": skipped,
                        "reason": "Приведение к долям стратегии «" + policy["name"] + "»." + skip_note if rebalance else "Операция в пределах подтверждённых правил.",
                        "source_snapshot": snapshot, "policy": policy, "profile": None if is_demo else self.profile(user, data),
                        "mode": "demo" if is_demo else "profile", "demo_config": data["demo_config"] if is_demo else None,
                        "config_version": data["demo_config_version"] if is_demo else None}
            proposal["fingerprint"] = hashlib.sha256(encode(proposal).encode()).hexdigest()
            with self.lock(owner), self.store.transaction() as conn, self.vault.accept(entry):
                current_user = self.store.authenticate(conn, owner, key)
                self.require_settled_actions(conn, owner)
                current = self.store.load(conn, owner)
                if self.store.orders(conn, owner, account["id"]) != existing:
                    raise SandboxConflict("Статусы заявок изменились во время расчёта. Обновите счёт и повторите действие.")
                if demo_config is not None:
                    if current["version"] != original_data["version"] or current["account"] != original_data["account"] or current.get("policy") != original_data.get("policy") or current.get("policy_draft") != original_data.get("policy_draft") or current["demo_config_version"] != original_data["demo_config_version"]:
                        raise SandboxConflict("Настройки изменились во время расчёта. Повторите действие.")
                    current.update(policy=policy, policy_draft=None, demo_config=demo_config,
                                   demo_config_version=data["demo_config_version"], stopped=False)
                    self.store.audit(conn, owner, "demo_configured", {"config": demo_config, "policy": policy})
                else:
                    current_policy = self.require_policy(current, current_user)
                    if current["stopped"] or current_policy["id"] != policy["id"]:
                        raise SandboxConflict("Правила изменились во время расчёта.")
                self.invalidate(conn, owner, current, "proposal_replaced")
                current.update(proposal_id=proposal["id"], snapshot=snapshot, catalog=catalog, checked_at=now(), reconcile_error=None)
                conn.execute("INSERT INTO sandbox_proposals VALUES(?,?,?,?,?,NULL,0)", (proposal["id"], owner, account["id"], encode(proposal), time.time() + 600))
                self.store.save(conn, owner, current)
                self.store.audit(conn, owner, "series_proposed", proposal)
                conn.commit()
        finally:
            self.vault.finish(entry)

    def cancel(self, owner, key, payload, background=False):
        entry = self.vault.begin(owner, key, touch=not background)
        try:
            data, user = self.read(owner, key)
            account = self.current_account(data, entry)
            with self.store.transaction() as conn:
                order = next((o for o in self.store.orders(conn, owner, account["id"]) if o["id"] == payload.get("order_id")), None)
                if not order or order["status"] not in ACTIVE:
                    raise ValidationError("Нет активной заявки для отмены.")
                self.store.audit(conn, owner, "cancel_requested", {"id": order["id"]})
            with self.lock(owner), self.vault.send_guard(entry):
                entry["client"].cancel_order(account["id"], order.get("order_id") or order["id"], request=not bool(order.get("order_id")))
                result = entry["client"].order_state(account["id"], order["id"], request=True)
            with self.store.transaction() as conn:
                self.store.order_result(conn, owner, order["id"], result)
                conn.execute("UPDATE sandbox_proposals SET revoked=1 WHERE id=?", (order["proposal_id"],))
                conn.execute("UPDATE sandbox_orders SET status='cancelled' WHERE proposal_id=? AND status='draft'", (order["proposal_id"],))
        finally:
            self.vault.finish(entry)
        self.wake.set()

    def dispatch(self, owner, key):
        entry = self.vault.begin(owner, key, touch=False)
        order = None
        sending = False
        try:
            data, user = self.read(owner, key)
            if data["stopped"]:
                return
            account = self.current_account(data, entry)
            policy = self.require_policy(data, user)
            with self.store.transaction() as conn:
                self.require_settled_actions(conn, owner)
                orders = self.store.orders(conn, owner, account["id"])
                if any(o["status"] in ACTIVE for o in orders):
                    return
                order = next((o for o in sorted(orders, key=lambda o: (o["created_at"], o.get("sequence", 0))) if o["status"] == "draft"), None)
                if not order:
                    return
                proposal = conn.execute("SELECT * FROM sandbox_proposals WHERE id=? AND user_id=?", (order["proposal_id"], owner)).fetchone()
                if not proposal or not proposal["confirmed_at"] or proposal["revoked"] or proposal["expires_at"] <= time.time():
                    raise ValidationError("Согласие на оставшиеся операции больше не действует.")
                self.approved_order(proposal, order, policy)
            snapshot = entry["client"].snapshot(account["id"])
            self.reconcile_positions(owner, account["id"], snapshot)
            checked = entry["client"].preview_order(account["id"], order["instrument_uid"], order["lots"], order["limit_price"], order["direction"], quote_policy=policy["quote_policy"])
            self.check_allocation(policy, snapshot, checked)
            if checked["estimated_total"] > order["estimated_total"] + 0.01 or checked["commission"] > order["commission"] + 0.01:
                raise ValidationError("Расходы выросли. Нужно новое предложение.")
            @contextmanager
            def admission():
                nonlocal sending
                with self.lock(owner), self.store.transaction() as conn, self.vault.send_guard(entry):
                    user = self.store.authenticate(conn, owner, key)
                    self.require_settled_actions(conn, owner)
                    current = self.store.load(conn, owner)
                    current_policy = self.require_policy(current, user)
                    proposal = conn.execute("SELECT * FROM sandbox_proposals WHERE id=? AND user_id=?", (order["proposal_id"], owner)).fetchone()
                    row = conn.execute("SELECT * FROM sandbox_orders WHERE id=? AND user_id=?", (order["id"], owner)).fetchone()
                    if current["stopped"] or current_policy["id"] != policy["id"] or not proposal or proposal["revoked"] or not proposal["confirmed_at"] or proposal["expires_at"] <= time.time() or not row or row["status"] != "draft":
                        raise SandboxConflict("Отправка остановлена: согласие или правила изменились.")
                    self.approved_order(proposal, {**json.loads(row["payload"]), "id": row["id"]}, current_policy)
                    if current.get("reconcile_error"):
                        raise SandboxConflict("Сначала выполните сверку счёта.")
                    self.store.audit(conn, owner, "order_checks", {"request_id": order["id"], "profile_version": None if policy.get("mode") == "demo" else current["profile_version"],
                                    "config_version": current["demo_config_version"] if policy.get("mode") == "demo" else None, "policy_id": policy["id"],
                                    "account_id": account["id"], "consent": proposal["confirmed_at"], "checks": checked, "snapshot": snapshot,
                                    "environment": "sandbox", "margin": False})
                    conn.execute("UPDATE sandbox_orders SET status='submitting' WHERE id=? AND status='draft'", (order["id"],))
                    conn.commit()
                    sending = True
                    yield
            result = entry["client"].place_order(account["id"], checked, order["id"], before_send=admission)
            with self.store.transaction() as conn:
                valid = self.store.order_result(conn, owner, order["id"], result)
                if valid is not False and result.get("status") == "filled":
                    self.refresh_pending.setdefault((owner, key), 3)
                if result.get("status") in ("cancelled", "rejected"):
                    conn.execute("UPDATE sandbox_proposals SET revoked=1 WHERE id=?", (order["proposal_id"],))
                    conn.execute("UPDATE sandbox_orders SET status='cancelled' WHERE proposal_id=? AND status='draft'", (order["proposal_id"],))
            self.last_refresh[(owner, key)] = 0
        except (SandboxError, SandboxConflict, ValidationError) as exc:
            if order:
                with self.store.transaction() as conn:
                    self.store.order_result(conn, owner, order["id"], {"status": "unknown" if sending else "blocked", "error": str(exc)})
                    if not sending:
                        conn.execute("UPDATE sandbox_proposals SET revoked=1 WHERE id=?", (order["proposal_id"],))
                        conn.execute("UPDATE sandbox_orders SET status='cancelled' WHERE proposal_id=? AND status='draft'", (order["proposal_id"],))
            raise
        finally:
            self.vault.finish(entry)

    def run(self):
        while not self.closed.is_set():
            self.wake.wait(2)
            self.wake.clear()
            with self.vault.lock:
                entries = [(e["owner"], e["key"]) for e in self.vault.connections.values() if e["ready"] and e["account_id"] and not e["busy"]]
            for owner, key in entries:
                if self.closed.is_set():
                    return
                self.tick(owner, key)

    def tick(self, owner, key):
        try:
            data, user = self.read(owner, key)
            with self.store.transaction() as conn:
                orders = self.store.orders(conn, owner, data["account"]["id"] if data["account"] else None)
            active = [o for o in orders if o["status"] in ACTIVE]
            if data["stopped"] and active:
                for order in active:
                    try:
                        self.cancel(owner, key, {"order_id": order["id"]}, background=True)
                    except (SandboxError, SandboxConflict, ValidationError, BrokerSessionError):
                        pass
            due = time.monotonic() - self.last_refresh.get((owner, key), 0)
            pending = (owner, key) in self.refresh_pending
            if (pending and due >= 2) or (active and due > 10) or due > 60:
                self.refresh(owner, key, background=True)
                data, user = self.read(owner, key)
                with self.store.transaction() as conn:
                    orders = self.store.orders(conn, owner, data["account"]["id"] if data["account"] else None)
                if not data["stopped"] and data.get("policy") and data["policy"].get("mode") != "demo" and not any(o["status"] in ACTIVE or o["status"] == "draft" for o in orders):
                    with self.store.transaction() as conn:
                        proposal = conn.execute("SELECT * FROM sandbox_proposals WHERE id=? AND user_id=?", (data.get("proposal_id"), owner)).fetchone()
                    needs_proposal = not proposal or proposal["revoked"] or proposal["expires_at"] <= time.time() or bool(proposal["confirmed_at"])
                    if needs_proposal and rebalance_orders(data["policy"], data["snapshot"], data["catalog"]):
                        self.preview(owner, key, {}, True, background=True)
            if not data["stopped"] and any(o["status"] == "draft" for o in orders):
                self.dispatch(owner, key)
        except Exception:
            identity = (owner, key)
            self.last_refresh[identity] = time.monotonic()
            if identity in self.refresh_pending:
                self.refresh_pending[identity] -= 1
                if self.refresh_pending[identity] <= 0:
                    self.refresh_pending.pop(identity, None)
