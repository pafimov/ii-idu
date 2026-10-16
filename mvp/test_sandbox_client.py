import copy
import io
import json
import ssl
import unittest
import urllib.error
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from sandbox import BASE_URL, METHODS, RESPONSE_LIMIT, SandboxClient, SandboxError, quote


TOKEN = "sandbox-test-token"
ACCOUNT = "sandbox-account"


def amount(value):
    return {"currency": "rub", **quote(Decimal(str(value)))}


def timestamp(age=0):
    return (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()


class Transport:
    def __init__(self):
        self.calls = []
        self.cash = Decimal(1_000_000)
        self.blocked_cash = 0
        self.blocked_securities = 0
        self.position_quantity = 100
        self.active_orders = []
        self.quote_age = 0
        self.buy_available = True
        self.trading_available = True
        self.missing_commission = False
        self.post_error = None
        self.order = None
        self.metadata = {}
        for ticker, kind, class_code, lot, price, tick in (("SBER", "share", "TQBR", 10, 100, .01), ("LKOH", "share", "TQBR", 1, 7000, .5), ("TMOS", "etf", "TQTF", 1, 12.34, .001), ("TGLD", "etf", "TQTF", 1, 9.99, .01)):
            uid = ticker.lower() + "-uid"
            self.metadata[uid] = {"uid": uid, "ticker": ticker, "name": "Инструмент " + ticker, "instrumentType": kind, "classCode": class_code, "lot": lot, "currency": "rub", "sector": "financial", "minPriceIncrement": quote(Decimal(str(tick))), "apiTradeAvailableFlag": True, "buyAvailableFlag": True, "sellAvailableFlag": True, "price": price}

    def order_row(self, order_id=None, status="NEW", lots=1, direction="BUY"):
        return {"orderId": "broker-order", "orderRequestId": order_id or str(uuid.uuid4()), "instrumentUid": "sber-uid", "ticker": "SBER", "direction": "ORDER_DIRECTION_" + direction, "executionReportStatus": "EXECUTION_REPORT_STATUS_" + status, "lotsRequested": str(lots), "lotsExecuted": str(lots if status == "FILL" else 0), "executedOrderPrice": amount(0), "executedCommission": amount(0), "orderDate": timestamp()}

    def __call__(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if method == "SandboxService/GetSandboxAccounts":
            return {"accounts": [{"id": ACCOUNT, "name": "Учебный", "type": "ACCOUNT_TYPE_TINKOFF", "status": "ACCOUNT_STATUS_OPEN", "accessLevel": "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"}]}
        if method == "SandboxService/OpenSandboxAccount":
            return {"accountId": ACCOUNT}
        if method == "SandboxService/SandboxPayIn":
            self.cash += Decimal(payload["amount"]["units"]) + Decimal(payload["amount"]["nano"]) / 1_000_000_000
            return {"balance": amount(self.cash)}
        if method == "InstrumentsService/FindInstrument":
            return {"instruments": [copy.deepcopy(row) for row in self.metadata.values() if row["ticker"].startswith(payload["query"])]}
        if method in ("InstrumentsService/ShareBy", "InstrumentsService/EtfBy"):
            row = copy.deepcopy(self.metadata[payload["id"]])
            row["buyAvailableFlag"] = self.buy_available
            return {"instrument": row}
        if method == "MarketDataService/GetLastPrices":
            return {"lastPrices": [{"instrumentUid": uid, "price": quote(Decimal(str(self.metadata[uid]["price"]))), "time": timestamp(self.quote_age)} for uid in payload["instrumentId"]]}
        if method == "MarketDataService/GetTradingStatus":
            return {"instrumentUid": payload["instrumentId"], "apiTradeAvailableFlag": self.trading_available, "limitOrderAvailableFlag": self.trading_available}
        if method == "MarketDataService/GetOrderBook":
            return {"instrumentUid": payload["instrumentId"], "priceCurrency": "rub", "limitDown": quote(Decimal(1)), "limitUp": quote(Decimal(10000))}
        if method == "SandboxService/GetSandboxPortfolio":
            return {"accountId": ACCOUNT, "positions": [{"instrumentUid": "sber-uid", "instrumentType": "share", "quantity": quote(Decimal(self.position_quantity)), "currentPrice": amount(100)}], "totalAmountPortfolio": amount(self.cash + self.position_quantity * 100), "totalAmountFutures": amount(0)}
        if method == "SandboxService/GetSandboxPositions":
            return {"accountId": ACCOUNT, "money": [amount(self.cash)], "blocked": [amount(self.blocked_cash)], "securities": [{"instrumentUid": "sber-uid", "instrumentType": "share", "balance": str(self.position_quantity - self.blocked_securities), "blocked": str(self.blocked_securities)}]}
        if method == "SandboxService/GetSandboxOrders":
            return {"orders": copy.deepcopy(self.active_orders)}
        if method == "SandboxService/GetSandboxOrderPrice":
            price = Decimal(payload["price"]["units"]) + Decimal(payload["price"]["nano"]) / 1_000_000_000
            notional = price * int(payload["quantity"]) * self.metadata[payload["instrumentId"]]["lot"]
            commission = notional * Decimal("0.0005")
            result = {"initialOrderAmount": amount(notional), "totalOrderAmount": amount(notional + commission), "dealCommission": amount(commission), "serviceCommission": amount(0)}
            if self.missing_commission:
                del result["dealCommission"]
            return result
        if method == "SandboxService/PostSandboxOrder":
            if self.post_error:
                raise self.post_error
            self.order = self.order_row(payload["orderId"], lots=int(payload["quantity"]), direction=payload["direction"].removeprefix("ORDER_DIRECTION_"))
            return copy.deepcopy(self.order)
        if method == "SandboxService/GetSandboxOrderState":
            return copy.deepcopy(self.order or self.order_row(payload["orderId"]))
        if method == "SandboxService/CancelSandboxOrder":
            return {"time": timestamp()}
        raise AssertionError(method)


class Response:
    status = 200

    def __init__(self, content):
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.content[:limit]


class SandboxTests(unittest.TestCase):
    def setUp(self):
        self.transport = Transport()
        self.client = SandboxClient(TOKEN, self.transport)

    def assert_error(self, code, action):
        with self.assertRaises(SandboxError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(TOKEN, str(caught.exception))
        return caught.exception

    def test_accounts_allow_sandbox_full_access_open_account_and_payin(self):
        self.assertEqual(self.client.accounts()[0]["id"], ACCOUNT)
        self.assertEqual(self.client.open_account(), ACCOUNT)
        self.assertEqual(self.client.pay_in(ACCOUNT, 10_000_000)["balance"], 11_000_000)
        payload = next(payload for method, payload in self.transport.calls if method.endswith("SandboxPayIn"))
        self.assertEqual(payload["amount"], {"currency": "rub", "units": "10000000", "nano": 0})
        self.assert_error("INVALID_ORDER", lambda: self.client.pay_in(ACCOUNT, 30_000_001))
        self.assert_error("INVALID_ORDER", lambda: self.client.pay_in(ACCOUNT, -1))
        self.assert_error("INVALID_ACCOUNT", lambda: self.client.snapshot("production-account"))

    def test_snapshot_has_real_units_and_no_fabricated_average_or_pnl(self):
        snapshot = self.client.snapshot(ACCOUNT)
        self.assertEqual(snapshot["cash"], 1_000_000)
        self.assertEqual(snapshot["total_value"], 1_010_000)
        self.assertEqual(snapshot["positions"][0]["quantity"], 100)
        self.assertNotIn("average_price", snapshot["positions"][0])
        self.assertNotIn("pnl", snapshot)
        self.assertNotIn(TOKEN, json.dumps(snapshot))

    def test_catalog_uses_lots_steps_and_caches_only_metadata(self):
        first = self.client.catalog()
        self.assertEqual(len(first), 4)
        self.assertEqual(first[0]["lot"], 10)
        self.assertEqual(first[0]["tick_size"], .01)
        self.assertEqual(first[-1]["asset_class"], "funds")
        self.assertEqual(first[-1]["sector"], "Фонды: состав не раскрыт")
        self.client.catalog()
        methods = [method for method, _ in self.transport.calls]
        self.assertEqual(methods.count("InstrumentsService/FindInstrument"), 4)
        self.assertEqual(methods.count("InstrumentsService/ShareBy"), 2)
        self.assertEqual(methods.count("MarketDataService/GetLastPrices"), 2)
        self.assertTrue(all(method in METHODS for method in methods))

    def test_approved_spbru_aliases_share_canonical_catalog_and_snapshot_keys(self):
        self.transport.metadata["tmos-uid"].update(ticker="TMOS@", classCode="SPBRU")
        self.transport.metadata["tgld-uid"].update(ticker="TGLD@", classCode="SPBRU")
        original = self.transport.__call__
        def transport(method, payload):
            result = original(method, payload)
            if method == "SandboxService/GetSandboxPortfolio":
                result["positions"] = [{"instrumentUid": "tmos-uid", "instrumentType": "etf", "quantity": {"units": "100"}, "currentPrice": amount(12.34)}]
                result["totalAmountPortfolio"] = amount(self.transport.cash + Decimal("1234"))
            if method == "SandboxService/GetSandboxPositions":
                result["securities"] = [{"instrumentUid": "tmos-uid", "instrumentType": "etf", "balance": "100", "blocked": "0"}]
            return result
        client = SandboxClient(TOKEN, transport)
        catalog = {row["ticker"]: row for row in client.catalog()}
        self.assertTrue(catalog["TMOS"]["available"])
        self.assertTrue(catalog["TGLD"]["available"])
        self.assertEqual(catalog["TMOS"]["broker_ticker"], "TMOS@")
        self.assertEqual(catalog["TMOS"]["broker_class_code"], "SPBRU")
        position = client.snapshot(ACCOUNT)["positions"][0]
        self.assertEqual((position["ticker"], position["broker_ticker"]), ("TMOS", "TMOS@"))
        plan = client.preview_order(ACCOUNT, "tmos-uid", 1, 12.34)
        self.assertEqual(plan["ticker"], "TMOS")

    def test_unknown_suffix_or_two_approved_candidates_are_not_guessed(self):
        self.transport.metadata["tmos-uid"].update(ticker="TMOS_UNKNOWN", classCode="SPBRU")
        catalog = {row["ticker"]: row for row in self.client.catalog()}
        self.assertFalse(catalog["TMOS"]["available"])
        self.assert_error("UNAVAILABLE", lambda: self.client.preview_order(ACCOUNT, "tmos-uid", 1, 12.34))
        self.transport.metadata["tmos-uid"].update(ticker="TMOS", classCode="TQTF")
        self.transport.metadata["alias-uid"] = {**self.transport.metadata["tmos-uid"], "uid": "alias-uid", "ticker": "TMOS@", "classCode": "SPBRU"}
        catalog = {row["ticker"]: row for row in SandboxClient(TOKEN, self.transport).catalog()}
        self.assertFalse(catalog["TMOS"]["available"])

    def test_preview_known_fees_buffer_and_lot_quantity(self):
        plan = self.client.preview_order(ACCOUNT, "sber-uid", 2, 100)
        self.assertEqual(plan["quantity"], 20)
        self.assertEqual(plan["notional"], 2000)
        self.assertEqual(plan["commission"], 1)
        self.assertEqual(plan["broker_total"], 2001)
        self.assertEqual(plan["buffer"], 10)
        self.assertEqual(plan["estimated_total"], 2011)
        self.assert_error("INVALID_ORDER", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100.001))
        self.assert_error("INVALID_ORDER", lambda: self.client.preview_order(ACCOUNT, "sber-uid", True, 100))
        self.assert_error("INVALID_ORDER", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, float("nan")))

    def test_no_margin_cash_blocked_orders_and_unknown_fees_fail_closed(self):
        self.transport.cash = Decimal(1000)
        self.assert_error("INSUFFICIENT_CASH", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100))
        self.transport.cash = Decimal(1_000_000)
        self.transport.blocked_cash = 1
        self.assert_error("ACTIVE_ORDERS", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100))
        self.transport.blocked_cash = 0
        self.transport.active_orders = [self.transport.order_row()]
        self.assert_error("ACTIVE_ORDERS", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100))
        self.transport.active_orders = []
        self.transport.missing_commission = True
        self.assert_error("INVALID_RESPONSE", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100))

    def test_sell_only_long_free_quantity(self):
        plan = self.client.preview_order(ACCOUNT, "sber-uid", 2, 100, "sell")
        self.assertEqual(plan["direction"], "sell")
        self.assert_error("INSUFFICIENT_POSITION", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 11, 100, "sell"))
        self.transport.blocked_securities = 1
        self.assert_error("INSUFFICIENT_POSITION", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100, "sell"))

    def test_stale_quotes_strict_and_explicit_sandbox_policy(self):
        self.transport.quote_age = 3600
        self.assert_error("UNAVAILABLE", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100))
        plan = self.client.preview_order(ACCOUNT, "sber-uid", 1, 100, quote_policy="sandbox_last_close")
        self.assertEqual(plan["quote_policy"], "sandbox_last_close")
        self.assertTrue(plan["warnings"])
        self.transport.quote_age = 8 * 24 * 60 * 60
        self.assert_error("UNAVAILABLE", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100, quote_policy="sandbox_last_close"))

    def test_api_trading_status_and_bounds_are_checked(self):
        self.transport.trading_available = False
        error = self.assert_error("TRADING_CLOSED", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100))
        self.assertIn("Торги по инструменту сейчас закрыты", str(error))
        self.transport.trading_available = True
        self.assert_error("INVALID_ORDER", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 10001))
        self.client = SandboxClient(TOKEN, self.transport)
        self.transport.buy_available = False
        self.assert_error("UNAVAILABLE", lambda: self.client.preview_order(ACCOUNT, "sber-uid", 1, 100))

    def test_post_order_is_limit_with_saved_uuid_and_never_retries(self):
        plan = self.client.preview_order(ACCOUNT, "sber-uid", 2, 100)
        request_id = str(uuid.uuid4())
        order = self.client.place_order(ACCOUNT, plan, request_id)
        self.assertEqual(order["request_id"], request_id)
        payloads = [payload for method, payload in self.transport.calls if method == "SandboxService/PostSandboxOrder"]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["quantity"], "2")
        self.assertEqual(payloads[0]["orderId"], request_id)
        self.assertEqual(payloads[0]["orderType"], "ORDER_TYPE_LIMIT")
        self.assertIs(payloads[0]["confirmMarginTrade"], False)
        self.assertEqual(payloads[0]["priceType"], "PRICE_TYPE_CURRENCY")
        self.assert_error("INVALID_ORDER", lambda: self.client.place_order(ACCOUNT, {**plan, "quantity": 999}, str(uuid.uuid4())))
        self.transport.post_error = TimeoutError(TOKEN)
        self.assert_error("UNKNOWN_EXECUTION", lambda: self.client.place_order(ACCOUNT, plan, str(uuid.uuid4())))
        self.assertEqual(len([method for method, _ in self.transport.calls if method == "SandboxService/PostSandboxOrder"]), 2)

    def test_lookup_by_request_uuid_and_cancel_by_broker_id(self):
        request_id = str(uuid.uuid4())
        order = self.client.order_state(ACCOUNT, request_id)
        self.assertEqual(order["request_id"], request_id)
        request = self.transport.calls[-1][1]
        self.assertEqual(request["orderIdType"], "ORDER_ID_TYPE_REQUEST")
        result = self.client.cancel_order(ACCOUNT, order["order_id"])
        self.assertTrue(result["cancel_requested"])
        self.assertEqual(self.transport.calls[-1][1]["orderIdType"], "ORDER_ID_TYPE_EXCHANGE")

    def test_filled_and_partial_status_must_match_executed_lots(self):
        row = self.transport.order_row(status="FILL", lots=2)
        self.assertEqual(self.client._order(row)["status"], "filled")
        row["lotsExecuted"] = "1"
        self.assert_error("INVALID_RESPONSE", lambda: self.client._order(row))
        row = self.transport.order_row(status="PARTIALLYFILL", lots=2)
        for executed in ("0", "2", "3"):
            row["lotsExecuted"] = executed
            self.assert_error("INVALID_RESPONSE", lambda: self.client._order(row))
        row["lotsExecuted"] = "1"
        self.assertEqual(self.client._order(row)["status"], "partially_filled")
        row["executionReportStatus"] = "EXECUTION_REPORT_STATUS_NEW"
        self.assert_error("INVALID_RESPONSE", lambda: self.client._order(row))

    def test_final_authorization_hook_runs_after_prechecks_and_can_block_post(self):
        plan = self.client.preview_order(ACCOUNT, "sber-uid", 1, 100)
        checked = []
        @contextmanager
        def deny():
            checked.append(self.transport.calls[-1][0])
            raise SandboxError("FORBIDDEN")
            yield
        self.assert_error("FORBIDDEN", lambda: self.client.place_order(ACCOUNT, plan, str(uuid.uuid4()), before_send=deny))
        self.assertEqual(checked, ["SandboxService/GetSandboxOrderPrice"])
        self.assertFalse(any(method == "SandboxService/PostSandboxOrder" for method, _ in self.transport.calls))

    def test_unsupported_snapshot_and_mismatch_have_no_partial_result(self):
        original = self.transport.__call__
        def transport(method, payload):
            result = original(method, payload)
            if method == "SandboxService/GetSandboxPortfolio":
                result["positions"][0]["instrumentType"] = "bond"
            return result
        self.assert_error("UNSUPPORTED", lambda: SandboxClient(TOKEN, transport).snapshot(ACCOUNT))
        def mismatch(method, payload):
            result = original(method, payload)
            if method == "SandboxService/GetSandboxPositions":
                result["securities"][0]["balance"] = "101"
            return result
        self.assert_error("RECONCILIATION", lambda: SandboxClient(TOKEN, mismatch).snapshot(ACCOUNT))

    def test_class_totals_variation_margin_and_metadata_kind_are_not_ignored(self):
        original = self.transport.__call__
        def wrong_class(method, payload):
            result = original(method, payload)
            if method == "SandboxService/GetSandboxPortfolio":
                result["totalAmountShares"] = amount(99)
            return result
        self.assert_error("RECONCILIATION", lambda: SandboxClient(TOKEN, wrong_class).snapshot(ACCOUNT))
        def variation_margin(method, payload):
            result = original(method, payload)
            if method == "SandboxService/GetSandboxPortfolio":
                result["totalVarMargin"] = amount(1)
            return result
        self.assert_error("UNSUPPORTED", lambda: SandboxClient(TOKEN, variation_margin).snapshot(ACCOUNT))
        self.transport.metadata["sber-uid"]["instrumentKind"] = "INSTRUMENT_TYPE_FUTURES"
        self.assert_error("UNSUPPORTED", lambda: self.client.snapshot(ACCOUNT))

    def test_http_errors_and_native_exception_text_never_leak_token(self):
        for status, code in ((401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (404, "NOT_FOUND"), (429, "RATE_LIMIT")):
            def transport(method, payload, status=status):
                raise urllib.error.HTTPError(BASE_URL + TOKEN, status, TOKEN, {}, io.BytesIO(TOKEN.encode()))
            self.assert_error(code, lambda: SandboxClient(TOKEN, transport).accounts())
        for error, code in ((TimeoutError(TOKEN), "TIMEOUT"), (urllib.error.URLError(TOKEN), "NETWORK")):
            def transport(method, payload, error=error):
                raise error
            self.assert_error(code, lambda: SandboxClient(TOKEN, transport).accounts())

    def test_fixed_sandbox_host_tls_no_proxy_redirect_and_write_allowlist(self):
        body = json.dumps({"accounts": []}).encode()
        with patch("sandbox.urllib.request.build_opener") as build:
            build.return_value.open.return_value = Response(body)
            self.assertEqual(SandboxClient(TOKEN).accounts(), [])
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, BASE_URL + "SandboxService/GetSandboxAccounts")
            self.assertEqual(request.host, "sandbox-invest-public-api.tbank.ru")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 10)
            proxy, https, redirect = build.call_args.args
            self.assertEqual(proxy.proxies, {})
            self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(https._context.check_hostname)
            self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://invest-public-api.tbank.ru"))
        self.assert_error("FORBIDDEN", lambda: self.client._call("OrdersService/PostOrder", {}))
        self.assert_error("FORBIDDEN", lambda: self.client._request("UsersService/GetAccounts", {}))

    def test_invalid_json_large_response_and_overall_deadline(self):
        for body, code in ((b"x" * (RESPONSE_LIMIT + 1), "TOO_LARGE"), (b'{"accounts":NaN}', "INVALID_RESPONSE"), (b'{"x":1,"x":2}', "INVALID_RESPONSE")):
            with patch("sandbox.urllib.request.build_opener") as build:
                build.return_value.open.return_value = Response(body)
                self.assert_error(code, SandboxClient(TOKEN).accounts)
        clock = [0]
        def transport(method, payload):
            clock[0] += 16
            return self.transport(method, payload)
        with patch("sandbox.time.monotonic", side_effect=lambda: clock[0]):
            client = SandboxClient(TOKEN, transport)
            self.assert_error("TIMEOUT", lambda: client.snapshot(ACCOUNT))
            self.assertIsNone(client._deadline)


if __name__ == "__main__":
    unittest.main()
