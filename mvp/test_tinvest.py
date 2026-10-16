import copy
import io
import json
import ssl
import unittest
import urllib.error
from unittest.mock import patch

from tinvest import BASE_URL, METHODS, RESPONSE_LIMIT, NoRedirect, TInvestClient, TInvestError, quotation


TOKEN = "test-readonly-token"


def amount(units, nano=0, currency="rub"):
    return {"units": str(units), "nano": nano, "currency": currency}


def account(access="READ_ONLY", status="OPEN", account_id="123"):
    return {"id": account_id, "name": "Основной", "type": "ACCOUNT_TYPE_TINKOFF", "accessLevel": "ACCOUNT_ACCESS_LEVEL_" + access, "status": "ACCOUNT_STATUS_" + status}


def fixture():
    share = {"instrumentUid": "share-id", "instrumentType": "share", "quantity": {"units": "20", "nano": 0}, "quantityLots": {"units": "2"}, "currentPrice": amount(100), "averagePositionPrice": amount(90)}
    bond = {"instrumentUid": "bond-id", "instrumentType": "bond", "quantity": {"units": "3"}, "currentPrice": amount(995, 500_000_000), "averagePositionPrice": amount(980), "currentNkd": amount(12, 500_000_000)}
    cash = {"instrumentType": "currency", "figi": "RUB000UTSTOM", "ticker": "RUB", "quantity": {"units": "500"}, "currentPrice": amount(1), "averagePositionPrice": amount(1)}
    return {
        "UsersService/GetAccounts": {"accounts": [account()]},
        "OperationsService/GetPortfolio": {"accountId": "123", "positions": [share, bond, cash], "totalAmountPortfolio": amount(5524), "totalAmountFutures": amount(0)},
        "OperationsService/GetPositions": {"accountId": "123", "money": [amount(500)], "blocked": [amount(0)], "securities": [{"instrumentUid": "share-id", "instrumentType": "share", "balance": "20", "blocked": "0"}, {"instrumentUid": "bond-id", "instrumentType": "bond", "balance": "3", "blocked": "0"}], "futures": [], "options": [], "limitsLoadingInProgress": False},
        "metadata": {"share-id": {"uid": "share-id", "ticker": "TEST", "name": "Акция", "instrumentType": "share", "currency": "rub", "lot": 10}, "bond-id": {"uid": "bond-id", "ticker": "BOND", "name": "Облигация", "instrumentType": "bond", "currency": "rub", "lot": 1}},
    }


class MockTransport:
    def __init__(self, data=None):
        self.data = data or fixture()
        self.calls = []

    def __call__(self, method, payload):
        self.calls.append((method, copy.deepcopy(payload)))
        if method.startswith("InstrumentsService/"):
            return {"instrument": copy.deepcopy(self.data["metadata"][payload["id"]])}
        return copy.deepcopy(self.data[method])


class Response:
    status = 200

    def __init__(self, content):
        self.content = content
        self.read_limit = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        self.read_limit = limit
        return self.content[:limit]


class TInvestTests(unittest.TestCase):
    def client(self, data=None):
        return TInvestClient(TOKEN, MockTransport(data))

    def assert_error(self, code, action):
        with self.assertRaises(TInvestError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertNotIn(TOKEN, str(caught.exception))
        return str(caught.exception)

    def test_tokens_are_validated_and_never_in_repr(self):
        for token in ("", "a b", "x\r\nHeader:token", "токен", "x" * 2001, None):
            self.assert_error("INVALID_TOKEN", lambda: TInvestClient(token))
        self.assertNotIn(TOKEN, repr(self.client()))

    def test_accounts_reject_any_trading_or_unknown_access_even_closed(self):
        for access in ("FULL_ACCESS", "UNSPECIFIED", "FUTURE_ENUM"):
            data = fixture()
            data["UsersService/GetAccounts"]["accounts"].append(account(access, "CLOSED", "456"))
            self.assert_error("READ_ONLY_REQUIRED", self.client(data).accounts)
        data = fixture()
        data["UsersService/GetAccounts"]["accounts"] += [account("NO_ACCESS", "OPEN", "456"), account("READ_ONLY", "CLOSED", "789")]
        self.assertEqual(self.client(data).accounts(), [{"id": "123", "name": "Основной", "type": "ACCOUNT_TYPE_TINKOFF", "access_level": "READ_ONLY"}])
        data["UsersService/GetAccounts"]["accounts"] = [account("NO_ACCESS")]
        self.assert_error("NO_ACCOUNTS", self.client(data).accounts)

    def test_account_id_cannot_select_unlisted_account(self):
        self.assert_error("INVALID_ACCOUNT", lambda: self.client().portfolio("other"))

    def test_only_brokerage_and_iis_types_and_fresh_access_are_accepted(self):
        data = fixture()
        data["UsersService/GetAccounts"]["accounts"] += [{**account(account_id="456"), "type": "ACCOUNT_TYPE_INVEST_BOX"}, {**account(account_id="789"), "type": "ACCOUNT_TYPE_TINKOFF_IIS"}]
        self.assertEqual([row["id"] for row in self.client(data).accounts()], ["123", "789"])
        data["UsersService/GetAccounts"]["accounts"] = [{**account(), "type": "ACCOUNT_TYPE_INVEST_BOX"}]
        self.assertIn("Другие типы", self.assert_error("NO_ACCOUNTS", self.client(data).accounts))
        data = fixture()
        client = self.client(data)
        client.accounts()
        data["UsersService/GetAccounts"]["accounts"][0]["accessLevel"] = "ACCOUNT_ACCESS_LEVEL_FULL_ACCESS"
        self.assert_error("READ_ONLY_REQUIRED", lambda: client.portfolio("123"))

    def test_dirty_bond_valuation_units_and_canonical_cash(self):
        transport = MockTransport()
        result = TInvestClient(TOKEN, transport).portfolio("123")
        self.assertEqual(result["broker_total"], 5524)
        positions = {row["ticker"]: row for row in result["positions"]}
        self.assertEqual(positions["TEST"]["quantity"], 20)
        self.assertEqual(positions["BOND"]["price"], 1008)
        self.assertEqual(positions["BOND"]["average_price"], 992.5)
        self.assertEqual(positions["BOND"]["source_price"], 995.5)
        self.assertEqual(positions["BOND"]["principal_average_price"], 980)
        self.assertEqual(positions["RUB"]["quantity"], 500)
        self.assertEqual(result["accrued_interest_total"], 37.5)
        self.assertEqual(positions["TEST"]["sector"], "Не указан брокером")
        self.assertIsNone(result["broker_timestamp"])
        self.assertNotIn(TOKEN, json.dumps(result))
        self.assertTrue(all(method in METHODS for method, _ in transport.calls))

    def test_cash_works_when_portfolio_omits_currency_row(self):
        data = fixture()
        data["OperationsService/GetPortfolio"]["positions"].pop()
        result = self.client(data).portfolio("123")
        self.assertEqual(next(row for row in result["positions"] if row["ticker"] == "RUB")["quantity"], 500)

    def test_redundant_ruble_row_needs_no_security_prices(self):
        data = fixture()
        cash = data["OperationsService/GetPortfolio"]["positions"][-1]
        del cash["averagePositionPrice"]
        del cash["currentPrice"]
        result = self.client(data).portfolio("123")
        normalized = next(row for row in result["positions"] if row["ticker"] == "RUB")
        self.assertEqual((normalized["quantity"], normalized["price"], normalized["average_price"]), (500, 1, 1))

    def test_nonzero_blocked_money_rejects_snapshot(self):
        data = fixture()
        data["OperationsService/GetPositions"]["blocked"] = [amount(100)]
        self.assertIn("заблокированы деньги", self.assert_error("UNSUPPORTED_PORTFOLIO", lambda: self.client(data).portfolio("123")))

    def test_security_quantity_and_identifier_sets_are_reconciled_exactly(self):
        changes = [
            lambda data: data["OperationsService/GetPositions"].update(securities=[]),
            lambda data: data["OperationsService/GetPositions"]["securities"].append(copy.deepcopy(data["OperationsService/GetPositions"]["securities"][0])),
            lambda data: data["OperationsService/GetPositions"]["securities"][0].update(balance="21"),
            lambda data: data["OperationsService/GetPositions"]["securities"][0].update(instrumentUid="other-id"),
            lambda data: data["OperationsService/GetPortfolio"]["positions"].pop(0),
            lambda data: data["OperationsService/GetPositions"]["securities"][0].pop("instrumentUid"),
        ]
        for change in changes:
            data = fixture()
            change(data)
            self.assert_error("RECONCILIATION", lambda: self.client(data).portfolio("123"))

    def test_figi_fallback_and_blocked_security_metadata(self):
        data = fixture()
        share = data["OperationsService/GetPortfolio"]["positions"][0]
        share.update(figi="SHAREFIGI", blocked=True, blockedLots={"units": "1"})
        balance = data["OperationsService/GetPositions"]["securities"][0]
        balance.update(figi="SHAREFIGI", balance="15", blocked="5")
        del balance["instrumentUid"]
        result = self.client(data).portfolio("123")
        self.assertTrue(result["positions"][0]["blocked"])
        self.assertTrue(any("заблокирована" in warning for warning in result["warnings"]))
        share["blocked"] = "true"
        self.assert_error("INVALID_RESPONSE", lambda: self.client(data).portfolio("123"))
        share["blocked"] = True
        del share["instrumentUid"]
        data["metadata"]["SHAREFIGI"] = {**data["metadata"]["share-id"], "figi": "SHAREFIGI"}
        self.assertEqual(self.client(data).portfolio("123")["positions"][0]["ticker"], "TEST")

    def test_funds_keep_separate_unknown_composition_sector(self):
        data = fixture()
        data["OperationsService/GetPortfolio"]["positions"][0]["instrumentType"] = "etf"
        data["OperationsService/GetPositions"]["securities"][0]["instrumentType"] = "etf"
        data["metadata"]["share-id"].update(instrumentType="etf", sector="gold", name="Золотой фонд")
        position = self.client(data).portfolio("123")["positions"][0]
        self.assertEqual(position["asset_class"], "funds")
        self.assertEqual(position["sector"], "Фонды: состав не раскрыт")

    def test_typed_metadata_methods_supply_sector_without_generic_type_field(self):
        data = fixture()
        data["metadata"]["share-id"].update(sector="financial", instrumentKind="INSTRUMENT_TYPE_SHARE")
        data["metadata"]["bond-id"].update(sector="government", instrumentKind="INSTRUMENT_TYPE_BOND")
        del data["metadata"]["share-id"]["instrumentType"]
        del data["metadata"]["bond-id"]["instrumentType"]
        transport = MockTransport(data)
        result = TInvestClient(TOKEN, transport).portfolio("123")
        self.assertEqual([row["sector"] for row in result["positions"][:2]], ["financial", "government"])
        methods = [method for method, _ in transport.calls if method.startswith("InstrumentsService/")]
        self.assertEqual(methods, ["InstrumentsService/ShareBy", "InstrumentsService/BondBy"])
        data["metadata"]["share-id"]["instrumentKind"] = "INSTRUMENT_TYPE_FUTURES"
        self.assert_error("UNSUPPORTED_PORTFOLIO", lambda: self.client(data).portfolio("123"))

    def test_reconciliation_is_strict_and_no_adjustment(self):
        data = fixture()
        data["OperationsService/GetPortfolio"]["totalAmountPortfolio"] = amount(5526)
        message = self.assert_error("RECONCILIATION", lambda: self.client(data).portfolio("123"))
        self.assertIn("5524.00", message)
        self.assertIn("5526.00", message)

    def test_optional_class_totals_are_checked_when_present(self):
        data = fixture()
        data["OperationsService/GetPortfolio"].update(totalAmountShares=amount(2000), totalAmountBonds=amount(3024), totalAmountEtf=amount(0), totalAmountCurrencies=amount(500))
        self.assertEqual(self.client(data).portfolio("123")["broker_total"], 5524)
        data["OperationsService/GetPortfolio"]["totalAmountShares"] = amount(2002)
        self.assert_error("RECONCILIATION", lambda: self.client(data).portfolio("123"))

    def test_unsupported_and_incomplete_positions_fail_entire_sync(self):
        changes = [
            lambda data: data["OperationsService/GetPortfolio"]["positions"][0].update(instrumentType="futures"),
            lambda data: data["OperationsService/GetPortfolio"]["positions"][0].update(quantity={"units": "-1"}),
            lambda data: data["OperationsService/GetPositions"].update(money=[amount(-1)]),
            lambda data: data["OperationsService/GetPositions"].update(money=[amount(500), amount(1, currency="usd")]),
            lambda data: data["OperationsService/GetPortfolio"].update(totalAmountDfa=amount(1)),
            lambda data: data["OperationsService/GetPortfolio"].update(virtualPositions=[{"quantity": {"units": "1"}}]),
            lambda data: data["OperationsService/GetPortfolio"]["positions"][0].update(currentPrice=amount(100, currency="usd")),
            lambda data: data["metadata"]["share-id"].update(currency="usd"),
        ]
        for change in changes:
            data = fixture()
            change(data)
            self.assert_error("UNSUPPORTED_PORTFOLIO", lambda: self.client(data).portfolio("123"))
        data = fixture()
        del data["OperationsService/GetPortfolio"]["positions"][0]["averagePositionPrice"]
        self.assertIn("средней цены", self.assert_error("INVALID_RESPONSE", lambda: self.client(data).portfolio("123")))

    def test_duplicates_and_metadata_limit_fail(self):
        data = fixture()
        data["metadata"]["bond-id"]["ticker"] = "test"
        self.assert_error("UNSUPPORTED_PORTFOLIO", lambda: self.client(data).portfolio("123"))
        data = fixture()
        share = data["OperationsService/GetPortfolio"]["positions"][0]
        data["OperationsService/GetPortfolio"]["positions"] = []
        data["OperationsService/GetPositions"]["securities"] = []
        for index in range(61):
            uid = f"share-{index}"
            data["OperationsService/GetPortfolio"]["positions"].append({**share, "instrumentUid": uid})
            data["OperationsService/GetPositions"]["securities"].append({"instrumentUid": uid, "instrumentType": "share", "balance": "20", "blocked": "0"})
            data["metadata"][uid] = {**data["metadata"]["share-id"], "uid": uid, "ticker": f"T{index}"}
        self.assert_error("METADATA_LIMIT", lambda: self.client(data).portfolio("123"))

    def test_signed_quotation_is_strict(self):
        self.assertEqual(str(quotation({"units": "-1", "nano": -500_000_000})), "-1.5")
        for value in ({"units": "NaN"}, {"units": True}, {"nano": 1_000_000_000}, {"units": -1, "nano": 1}, {"units": "1.1"}):
            self.assert_error("INVALID_RESPONSE", lambda: quotation(value))

    def test_api_errors_timeouts_and_transport_details_are_sanitized(self):
        errors = [(401, "UNAUTHORIZED"), (403, "FORBIDDEN"), (429, "RATE_LIMIT"), (500, "REMOTE_ERROR")]
        for status, expected in errors:
            def transport(method, payload, status=status):
                raise urllib.error.HTTPError(BASE_URL + TOKEN, status, TOKEN, {}, io.BytesIO(TOKEN.encode()))
            self.assert_error(expected, TInvestClient(TOKEN, transport).accounts)
        for error, expected in ((TimeoutError(TOKEN), "TIMEOUT"), (urllib.error.URLError(TOKEN), "NETWORK"), (RuntimeError(TOKEN), "NETWORK")):
            def transport(method, payload, error=error):
                raise error
            self.assert_error(expected, TInvestClient(TOKEN, transport).accounts)
        self.assert_error("UNAUTHORIZED", TInvestClient(TOKEN, lambda *_: {"code": "40003", "message": TOKEN}).accounts)

    def test_network_transport_uses_fixed_https_tls_no_proxy_and_no_redirect(self):
        response = Response(json.dumps({"accounts": [account()]}).encode())
        with patch("tinvest.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(len(TInvestClient(TOKEN).accounts()), 1)
            request = build.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url, BASE_URL + "UsersService/GetAccounts")
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 10)
            self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)
            proxy, https, redirect = build.call_args.args
            self.assertEqual(proxy.proxies, {})
            self.assertTrue(https._context.check_hostname)
            self.assertEqual(https._context.verify_mode, ssl.CERT_REQUIRED)
            self.assertIsInstance(redirect, NoRedirect)
            self.assertIsNone(redirect.redirect_request(None, None, 302, "", {}, "https://other.example"))
            self.assertEqual(response.read_limit, RESPONSE_LIMIT + 1)

    def test_overall_deadline_stops_serial_work_and_is_reset(self):
        clock = [0]
        transport = MockTransport()
        def slow_transport(method, payload):
            clock[0] += 16
            return transport(method, payload)
        client = TInvestClient(TOKEN, slow_transport)
        with patch("tinvest.time.monotonic", side_effect=lambda: clock[0]):
            self.assert_error("TIMEOUT", lambda: client.portfolio("123"))
        self.assertIsNone(client._deadline)
        self.assertEqual(len(transport.calls), 3)
        response = Response(json.dumps({"accounts": [account()]}).encode())
        client = TInvestClient(TOKEN)
        client._deadline = 100
        with patch("tinvest.time.monotonic", return_value=97), patch("tinvest.urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            client.accounts()
            self.assertEqual(build.return_value.open.call_args.kwargs["timeout"], 3)

    def test_response_size_invalid_json_and_write_methods_are_rejected(self):
        for body, code in ((b"x" * (RESPONSE_LIMIT + 1), "TOO_LARGE"), (b"invalid " + TOKEN.encode(), "INVALID_RESPONSE"), (b'{"accounts":NaN}', "INVALID_RESPONSE"), (b'{"accounts":[],"accounts":[]}', "INVALID_RESPONSE")):
            with patch("tinvest.urllib.request.build_opener") as build:
                build.return_value.open.return_value = Response(body)
                self.assert_error(code, TInvestClient(TOKEN).accounts)
        self.assert_error("FORBIDDEN", lambda: self.client()._call("OrdersService/PostOrder", {}))
        self.assert_error("FORBIDDEN", lambda: self.client()._request("OrdersService/PostOrder", {}))


if __name__ == "__main__":
    unittest.main()
