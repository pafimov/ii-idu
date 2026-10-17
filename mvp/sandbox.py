import copy
import functools
import json
import math
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from contextlib import nullcontext
from datetime import datetime, timezone
from decimal import Decimal, ROUND_UP

from tinvest import NoRedirect, TInvestError, integer, label, quotation


BASE_URL = "https://sandbox-invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
METHODS = frozenset({"SandboxService/" + name for name in ("GetSandboxAccounts", "OpenSandboxAccount", "SandboxPayIn", "GetSandboxPortfolio", "GetSandboxPositions", "GetSandboxOrders", "PostSandboxOrder", "GetSandboxOrderState", "CancelSandboxOrder", "GetSandboxOrderPrice")} | {"InstrumentsService/" + name for name in ("FindInstrument", "ShareBy", "EtfBy")} | {"MarketDataService/" + name for name in ("GetLastPrices", "GetTradingStatus", "GetOrderBook")})
RESPONSE_LIMIT = 2 * 1024 * 1024
WATCHLIST = (("SBER", "share", "TQBR"), ("LKOH", "share", "TQBR"), ("TMOS", "etf", "TQTF"), ("TGLD", "etf", "TQTF"), ("TMON", "etf", "TQTF"))
INSTRUMENT_PAIRS = {"SBER": (("SBER", "TQBR"),), "LKOH": (("LKOH", "TQBR"),),
                    "TMOS": (("TMOS", "TQTF"), ("TMOS@", "SPBRU")),
                    "TGLD": (("TGLD", "TQTF"), ("TGLD@", "SPBRU")),
                    "TMON": (("TMON", "TQTF"), ("TMON@", "SPBRU"))}
MESSAGES = {
    "INVALID_TOKEN": "Введите токен песочницы без пробелов и переносов строк.",
    "INVALID_RESPONSE": "Песочница вернула неполные или некорректные данные.",
    "INVALID_ACCOUNT": "Выберите доступный открытый счет песочницы.",
    "INVALID_ORDER": "Проверьте параметры заявки.",
    "UNSUPPORTED": "В этой версии поддерживаются только рублевые акции, фонды и длинные позиции.",
    "RECONCILIATION": "Ответы песочницы не согласованы. Обновите данные после завершения расчетов.",
    "ACTIVE_ORDERS": "Сначала дождитесь исполнения или отмените активные заявки и освободите заблокированные средства.",
    "INSUFFICIENT_CASH": "Свободных рублей недостаточно с учетом запаса на расходы.",
    "INSUFFICIENT_POSITION": "Недостаточно свободных бумаг. Продажа в долг запрещена.",
    "UNAVAILABLE": "Инструмент сейчас недоступен для этого действия в песочнице.",
    "TRADING_CLOSED": "Торги по инструменту сейчас закрыты: лимитная заявка не принимается.",
    "UNAUTHORIZED": "Токен не принят песочницей. Проверьте токен и срок его действия.",
    "FORBIDDEN": "Песочница запретила запрос. Проверьте права токена.",
    "NOT_FOUND": "Счет или заявка в песочнице не найдены. Обновите данные.",
    "RATE_LIMIT": "Превышен лимит запросов песочницы. Попробуйте позже.",
    "TIMEOUT": "Песочница не ответила вовремя. Обновите данные перед следующим действием.",
    "NETWORK": "Не удалось установить защищенное соединение с песочницей. Проверьте сеть и доверенные сертификаты.",
    "REMOTE_ERROR": "Песочница не смогла обработать запрос. Попробуйте позже.",
    "TOO_LARGE": "Ответ песочницы превышает допустимый размер.",
    "UNKNOWN_EXECUTION": "Результат запроса песочницы неизвестен. Обновите статус по сохраненному идентификатору; автоматически повторять действие нельзя.",
}
STATUSES = {"EXECUTION_REPORT_STATUS_NEW": "new", "EXECUTION_REPORT_STATUS_PARTIALLYFILL": "partially_filled", "EXECUTION_REPORT_STATUS_FILL": "filled", "EXECUTION_REPORT_STATUS_CANCELLED": "cancelled", "EXECUTION_REPORT_STATUS_REJECTED": "rejected"}


class SandboxError(Exception):
    def __init__(self, code, message=""):
        self.code = code if code in MESSAGES else "REMOTE_ERROR"
        super().__init__(MESSAGES[self.code] + (" " + message if message else ""))


def bounded(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        outer = self._deadline is None
        if outer:
            self._deadline = time.monotonic() + 45
        try:
            return method(self, *args, **kwargs)
        except TInvestError:
            raise SandboxError("INVALID_RESPONSE") from None
        finally:
            if outer:
                self._deadline = None
    return call


def entries(data, field):
    result = data.get(field, [])
    if not isinstance(result, list) or len(result) > 100 or any(not isinstance(row, dict) for row in result):
        raise SandboxError("INVALID_RESPONSE")
    return result


def money(value):
    result = quotation(value)
    if str(value.get("currency", "")).upper() != "RUB" or result < 0:
        raise SandboxError("UNSUPPORTED")
    return result


def numeric(value, maximum=30_000_000):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise SandboxError("INVALID_ORDER")
    if isinstance(value, float) and not math.isfinite(value):
        raise SandboxError("INVALID_ORDER")
    if isinstance(value, int) and not 0 < value <= maximum:
        raise SandboxError("INVALID_ORDER")
    result = Decimal(str(value))
    if not result.is_finite() or not 0 < result <= maximum or result.as_tuple().exponent < -9:
        raise SandboxError("INVALID_ORDER")
    return result


def quote(value):
    units = int(value)
    return {"units": str(units), "nano": int((value - units) * 1_000_000_000)}


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise SandboxError("INVALID_RESPONSE")
    return value


class SandboxClient:
    def __init__(self, token, transport=None):
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,2000}", token):
            raise SandboxError("INVALID_TOKEN")
        self._token, self._transport = token, transport
        self._deadline = None
        self._metadata_cache, self._catalog_ids = {}, {}

    def _timeout(self):
        remaining = self._deadline - time.monotonic() if self._deadline is not None else 10
        if remaining <= 0:
            raise SandboxError("TIMEOUT")
        return min(10, remaining)

    def _request(self, method, payload):
        if method not in METHODS:
            raise SandboxError("FORBIDDEN")
        request = urllib.request.Request(BASE_URL + method, json.dumps(payload).encode(), {"Authorization": "Bearer " + self._token, "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        with opener.open(request, timeout=self._timeout()) as response:
            if response.status != 200:
                raise SandboxError("REMOTE_ERROR")
            body = response.read(RESPONSE_LIMIT + 1)
        self._timeout()
        if len(body) > RESPONSE_LIMIT:
            raise SandboxError("TOO_LARGE")
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        def reject(_):
            raise ValueError()
        try:
            return json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise SandboxError("INVALID_RESPONSE") from None

    def _call(self, method, payload):
        if method not in METHODS:
            raise SandboxError("FORBIDDEN")
        try:
            self._timeout()
            data = self._transport(method, payload) if self._transport else self._request(method, payload)
            self._timeout()
        except SandboxError:
            raise
        except urllib.error.HTTPError as exc:
            code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 404: "NOT_FOUND", 429: "RATE_LIMIT", 408: "TIMEOUT", 504: "TIMEOUT"}.get(exc.code, "REMOTE_ERROR")
            exc.close()
            raise SandboxError(code) from None
        except (TimeoutError, socket.timeout):
            raise SandboxError("TIMEOUT") from None
        except Exception:
            raise SandboxError("NETWORK") from None
        if not isinstance(data, dict):
            raise SandboxError("INVALID_RESPONSE")
        if "code" in data and data["code"] not in (0, "0"):
            code = {"16": "UNAUTHORIZED", "40003": "UNAUTHORIZED", "7": "FORBIDDEN", "8": "RATE_LIMIT", "4": "TIMEOUT", "5": "NOT_FOUND"}.get(str(data["code"]), "REMOTE_ERROR")
            raise SandboxError(code)
        return data

    def _write(self, method, payload):
        try:
            return self._call(method, payload)
        except SandboxError as exc:
            if exc.code in ("TIMEOUT", "NETWORK", "REMOTE_ERROR", "INVALID_RESPONSE", "TOO_LARGE"):
                raise SandboxError("UNKNOWN_EXECUTION") from None
            raise

    @bounded
    def accounts(self):
        data = self._call("SandboxService/GetSandboxAccounts", {"status": "ACCOUNT_STATUS_OPEN"})
        result, seen = [], set()
        for row in entries(data, "accounts"):
            if row.get("status") != "ACCOUNT_STATUS_OPEN":
                continue
            account_id = identifier(row.get("id"))
            if account_id in seen:
                raise SandboxError("INVALID_RESPONSE")
            seen.add(account_id)
            result.append({"id": account_id, "name": label(row.get("name")), "type": label(row.get("type"), 60)})
        return result

    def _account(self, account_id):
        if not isinstance(account_id, str) or not any(row["id"] == account_id for row in self.accounts()):
            raise SandboxError("INVALID_ACCOUNT")

    @bounded
    def open_account(self, name="ИИ-ИДУ"):
        if not isinstance(name, str) or not name.strip() or len(name) > 60 or any(ord(char) < 32 for char in name):
            raise SandboxError("INVALID_ORDER", "Название счета должно содержать от 1 до 60 символов.")
        result = self._write("SandboxService/OpenSandboxAccount", {"name": name.strip()})
        try:
            return identifier(result.get("accountId"))
        except SandboxError:
            raise SandboxError("UNKNOWN_EXECUTION") from None

    @bounded
    def pay_in(self, account_id, amount):
        value = numeric(amount)
        if value != value.quantize(Decimal("0.01")):
            raise SandboxError("INVALID_ORDER", "Пополнение указывается с точностью до копейки.")
        self._account(account_id)
        result = self._write("SandboxService/SandboxPayIn", {"accountId": account_id, "amount": {"currency": "rub", **quote(value)}})
        try:
            return {"balance": float(money(result.get("balance")))}
        except (SandboxError, TInvestError):
            raise SandboxError("UNKNOWN_EXECUTION") from None

    def _metadata(self, uid, kind):
        identifier(uid)
        if kind not in ("share", "etf"):
            raise SandboxError("UNSUPPORTED")
        key = (uid, kind)
        cached = self._metadata_cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            return copy.deepcopy(cached[1])
        method = "ShareBy" if kind == "share" else "EtfBy"
        data = self._call("InstrumentsService/" + method, {"idType": "INSTRUMENT_ID_TYPE_UID", "id": uid})
        raw = data.get("instrument")
        if not isinstance(raw, dict) or raw.get("uid") != uid:
            raise SandboxError("INVALID_RESPONSE")
        expected_kind = "INSTRUMENT_TYPE_SHARE" if kind == "share" else "INSTRUMENT_TYPE_ETF"
        if str(raw.get("currency", "")).upper() != "RUB" or "instrumentType" in raw and raw["instrumentType"] != kind or "instrumentKind" in raw and raw["instrumentKind"] != expected_kind:
            raise SandboxError("UNSUPPORTED")
        lot = integer(raw.get("lot"), 1_000_000_000)
        tick = quotation(raw.get("minPriceIncrement"))
        if lot <= 0 or tick <= 0:
            raise SandboxError("INVALID_RESPONSE")
        flags = {}
        for field in ("apiTradeAvailableFlag", "buyAvailableFlag", "sellAvailableFlag"):
            if not isinstance(raw.get(field, False), bool):
                raise SandboxError("INVALID_RESPONSE")
            flags[field] = raw.get(field, False)
        broker_ticker = label(raw.get("ticker"), 24)
        broker_class = label(raw["classCode"], 60) if raw.get("classCode") else None
        canonical = next((ticker for ticker, pairs in INSTRUMENT_PAIRS.items() if (broker_ticker, broker_class) in pairs), broker_ticker)
        result = {"instrument_uid": uid, "ticker": canonical, "broker_ticker": broker_ticker, "broker_class_code": broker_class, "name": label(raw.get("name")), "asset_class": "stocks" if kind == "share" else "funds", "sector": label(raw["sector"], 60) if kind == "share" and raw.get("sector") else "Не указан брокером" if kind == "share" else "Фонды: состав не раскрыт", "currency": "RUB", "lot": lot, "tick_size": float(tick), "api_available": flags["apiTradeAvailableFlag"], "can_buy": flags["buyAvailableFlag"], "can_sell": flags["sellAvailableFlag"]}
        self._metadata_cache[key] = (time.monotonic(), result)
        return copy.deepcopy(result)

    def _order(self, row, request_id=None):
        if not isinstance(row, dict) or row.get("executionReportStatus") not in STATUSES or row.get("direction") not in ("ORDER_DIRECTION_BUY", "ORDER_DIRECTION_SELL"):
            raise SandboxError("INVALID_RESPONSE")
        requested = integer(row.get("lotsRequested"), 1_000_000_000)
        executed = integer(row.get("lotsExecuted", 0), 1_000_000_000)
        if not 0 <= executed <= requested or requested <= 0:
            raise SandboxError("INVALID_RESPONSE")
        status = STATUSES[row["executionReportStatus"]]
        if status == "filled" and executed != requested or status == "partially_filled" and not 0 < executed < requested or status == "new" and executed:
            raise SandboxError("INVALID_RESPONSE")
        return {"order_id": identifier(row.get("orderId")), "request_id": identifier(row.get("orderRequestId") or request_id) if row.get("orderRequestId") or request_id else None, "instrument_uid": identifier(row.get("instrumentUid")), "ticker": label(row["ticker"], 24) if row.get("ticker") else None, "direction": "buy" if row["direction"] == "ORDER_DIRECTION_BUY" else "sell", "status": STATUSES[row["executionReportStatus"]], "broker_status": row["executionReportStatus"], "lots_requested": requested, "lots_executed": executed, "executed_value": float(money(row["executedOrderPrice"])) if row.get("executedOrderPrice") else None, "commission": float(money(row["executedCommission"])) if row.get("executedCommission") else None, "created_at": label(row["orderDate"], 64) if row.get("orderDate") else None}

    @bounded
    def snapshot(self, account_id):
        self._account(account_id)
        portfolio = self._call("SandboxService/GetSandboxPortfolio", {"accountId": account_id, "currency": "RUB"})
        balances = self._call("SandboxService/GetSandboxPositions", {"accountId": account_id})
        orders = self._call("SandboxService/GetSandboxOrders", {"accountId": account_id})
        if portfolio.get("accountId") != account_id or balances.get("accountId") != account_id or balances.get("limitsLoadingInProgress", False) is not False:
            raise SandboxError("RECONCILIATION")
        if entries(portfolio, "virtualPositions") or entries(balances, "futures") or entries(balances, "options"):
            raise SandboxError("UNSUPPORTED")
        for field, value in portfolio.items():
            if (field.startswith("totalAmount") and field not in ("totalAmountShares", "totalAmountEtf", "totalAmountCurrencies", "totalAmountPortfolio") or field.startswith("totalVarMargin")) and quotation(value):
                raise SandboxError("UNSUPPORTED")
        cash_rows, blocked_rows = entries(balances, "money"), entries(balances, "blocked")
        if len(cash_rows) > 1 or len(blocked_rows) > 1:
            raise SandboxError("UNSUPPORTED")
        cash = money(cash_rows[0]) if cash_rows else Decimal(0)
        cash_blocked = money(blocked_rows[0]) if blocked_rows else Decimal(0)
        index, seen = {}, set()
        for row in entries(balances, "securities"):
            uid = identifier(row.get("instrumentUid"))
            if uid in index or row.get("instrumentType") not in ("share", "etf"):
                raise SandboxError("UNSUPPORTED")
            free, blocked = integer(row.get("balance", 0), 1_000_000_000), integer(row.get("blocked", 0), 1_000_000_000)
            if min(free, blocked) < 0:
                raise SandboxError("UNSUPPORTED")
            index[uid] = (row["instrumentType"], free, blocked)
        result, total, currency_seen = [], cash, False
        class_totals = {"share": Decimal(0), "etf": Decimal(0)}
        for row in entries(portfolio, "positions"):
            kind, quantity = row.get("instrumentType"), quotation(row.get("quantity"))
            if quantity < 0:
                raise SandboxError("UNSUPPORTED")
            if kind == "currency":
                if currency_seen or not (row.get("ticker") == "RUB" or row.get("figi") == "RUB000UTSTOM") or abs(quantity - cash) > 1:
                    raise SandboxError("RECONCILIATION")
                currency_seen = True
                continue
            uid = identifier(row.get("instrumentUid"))
            if kind not in ("share", "etf") or quantity <= 0:
                raise SandboxError("UNSUPPORTED")
            if uid in seen or uid not in index or index[uid][0] != kind or quantity != index[uid][1] + index[uid][2]:
                raise SandboxError("RECONCILIATION")
            seen.add(uid)
            if len(seen) > 60 or not isinstance(row.get("blocked", False), bool):
                raise SandboxError("INVALID_RESPONSE")
            metadata = self._metadata(uid, kind)
            price = money(row.get("currentPrice"))
            if price <= 0:
                raise SandboxError("INVALID_RESPONSE")
            total += price * quantity
            class_totals[kind] += price * quantity
            result.append({**{key: metadata[key] for key in ("ticker", "broker_ticker", "broker_class_code", "name", "asset_class", "sector", "lot", "instrument_uid")}, "quantity": float(quantity), "price": float(price), "currency": "RUB", "blocked": row.get("blocked", False) or index[uid][2] > 0, "available_quantity": index[uid][1]})
        if seen != set(index) or abs(total - money(portfolio.get("totalAmountPortfolio"))) > 1:
            raise SandboxError("RECONCILIATION")
        for field, computed in (("totalAmountShares", class_totals["share"]), ("totalAmountEtf", class_totals["etf"]), ("totalAmountCurrencies", cash)):
            if field in portfolio and abs(money(portfolio[field]) - computed) > 1:
                raise SandboxError("RECONCILIATION")
        warnings = ["Это счет песочницы: реальные деньги не используются. Средняя цена покупки и доходность здесь не рассчитываются.", "Время загрузки не является временем обновления котировок."]
        if cash_blocked:
            warnings.append("Есть заблокированные деньги. Новые заявки недоступны до освобождения средств.")
        return {"account_id": account_id, "positions": result, "cash": float(cash), "cash_blocked": float(cash_blocked), "total_value": float(total), "orders": [self._order(row) for row in entries(orders, "orders")], "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "warnings": warnings}

    @bounded
    def catalog(self):
        result = []
        for ticker, kind, class_code in WATCHLIST:
            cached = self._catalog_ids.get(ticker)
            if not cached or time.monotonic() - cached[0] >= 300:
                found = self._call("InstrumentsService/FindInstrument", {"query": ticker, "apiTradeAvailableFlag": True})
                candidates = [row for row in entries(found, "instruments") if (row.get("ticker"), row.get("classCode")) in INSTRUMENT_PAIRS[ticker] and row.get("instrumentType") == kind]
                if len(candidates) != 1:
                    result.append({"instrument_uid": None, "ticker": ticker, "name": ticker + ": инструмент не найден однозначно", "asset_class": "stocks" if kind == "share" else "funds", "sector": "Не указан брокером", "lot": None, "tick_size": None, "price": None, "available": False})
                    continue
                cached = (time.monotonic(), identifier(candidates[0].get("uid")))
                self._catalog_ids[ticker] = cached
            metadata = self._metadata(cached[1], kind)
            if metadata["ticker"] != ticker:
                raise SandboxError("INVALID_RESPONSE")
            result.append(metadata)
        uids = [row["instrument_uid"] for row in result if row["instrument_uid"]]
        prices = self._call("MarketDataService/GetLastPrices", {"instrumentId": uids}) if uids else {"lastPrices": []}
        price_map = {}
        for row in entries(prices, "lastPrices"):
            uid = identifier(row.get("instrumentUid"))
            if uid in price_map or uid not in uids:
                raise SandboxError("INVALID_RESPONSE")
            price = quotation(row.get("price"))
            if price < 0:
                raise SandboxError("INVALID_RESPONSE")
            price_map[uid] = (price, label(row["time"], 64) if row.get("time") else None)
        for row in result:
            if row["instrument_uid"]:
                price, price_time = price_map.get(row["instrument_uid"], (Decimal(0), None))
                row.update(price=float(price) if price else None, price_time=price_time, available=bool(price and row["api_available"] and (row["can_buy"] or row["can_sell"])))
        return result

    @bounded
    def preview_order(self, account_id, instrument_uid, lots, limit_price, direction="buy", quote_policy="strict_30s", allow_waiting=False):
        if isinstance(lots, bool) or not isinstance(lots, int) or not 1 <= lots <= 1_000_000 or direction not in ("buy", "sell"):
            raise SandboxError("INVALID_ORDER")
        price = numeric(limit_price)
        instrument = next((row for row in self.catalog() if row["instrument_uid"] == instrument_uid), None)
        if not instrument or not instrument["available"] or not instrument["can_" + direction]:
            raise SandboxError("UNAVAILABLE")
        if quote_policy not in ("strict_30s", "sandbox_last_close") or not isinstance(allow_waiting, bool):
            raise SandboxError("INVALID_ORDER")
        try:
            source_time = datetime.fromisoformat(instrument["price_time"].replace("Z", "+00:00"))
            if source_time.tzinfo is None:
                raise ValueError()
            quote_age = (datetime.now(timezone.utc) - source_time).total_seconds()
        except (ValueError, TypeError, AttributeError, KeyError):
            raise SandboxError("UNAVAILABLE", "Неизвестно время исходной котировки.") from None
        if quote_age < -5 or quote_age > (30 if quote_policy == "strict_30s" else 7 * 24 * 60 * 60):
            raise SandboxError("UNAVAILABLE", "Исходная котировка устарела для выбранной политики.")
        if price % Decimal(str(instrument["tick_size"])):
            raise SandboxError("INVALID_ORDER", "Цена должна быть кратна шагу цены инструмента.")
        status = self._call("MarketDataService/GetTradingStatus", {"instrumentId": instrument_uid})
        if status.get("instrumentUid") != instrument_uid:
            raise SandboxError("UNAVAILABLE", "Выставление лимитных заявок сейчас недоступно.")
        waiting = status.get("apiTradeAvailableFlag") is True and status.get("limitOrderAvailableFlag") is False and status.get("tradingStatus") == "SECURITY_TRADING_STATUS_NOT_AVAILABLE_FOR_TRADING"
        if waiting and not (allow_waiting and direction == "buy" and quote_policy == "sandbox_last_close"):
            raise SandboxError("TRADING_CLOSED", instrument["ticker"] + ": пересчитайте покупки после открытия торгов.")
        if status.get("apiTradeAvailableFlag") is not True or (status.get("limitOrderAvailableFlag") is not True and not waiting) or status.get("onlyBestPrice") is True:
            raise SandboxError("UNAVAILABLE", instrument["ticker"] + ": лимитные заявки сейчас недоступны.")
        book = self._call("MarketDataService/GetOrderBook", {"instrumentId": instrument_uid, "depth": 1})
        if book.get("instrumentUid") != instrument_uid or str(book.get("priceCurrency", "")).upper() != "RUB":
            raise SandboxError("INVALID_RESPONSE")
        lower = quotation(book["limitDown"]) if "limitDown" in book else Decimal(0)
        upper = quotation(book["limitUp"]) if "limitUp" in book else Decimal(0)
        if lower < 0 or upper < 0 or lower and price < lower or upper and price > upper:
            raise SandboxError("INVALID_ORDER", "Лимитная цена выходит за границы, полученные от биржи.")
        snapshot = self.snapshot(account_id)
        if snapshot["orders"] or snapshot["cash_blocked"]:
            raise SandboxError("ACTIVE_ORDERS")
        quantity = lots * instrument["lot"]
        notional = price * quantity
        if notional > 10_000_000:
            raise SandboxError("INVALID_ORDER", "Лимит одной учебной заявки — 10 млн рублей.")
        priced = self._call("SandboxService/GetSandboxOrderPrice", {"accountId": account_id, "instrumentId": instrument_uid, "quantity": str(lots), "price": quote(price), "direction": "ORDER_DIRECTION_" + direction.upper()})
        if not all(isinstance(priced.get(field), dict) for field in ("initialOrderAmount", "totalOrderAmount", "dealCommission")):
            raise SandboxError("INVALID_RESPONSE", "Песочница не предоставила полную стоимость и комиссию; заявка заблокирована.")
        commission = money(priced["dealCommission"]) + (money(priced["serviceCommission"]) if "serviceCommission" in priced else Decimal(0))
        broker_total = money(priced["totalOrderAmount"])
        expected = (notional + commission,) if direction == "buy" else (notional + commission, notional - commission)
        if abs(money(priced["initialOrderAmount"]) - notional) > Decimal("0.01") or all(abs(broker_total - value) > Decimal("0.01") for value in expected):
            raise SandboxError("RECONCILIATION", "Предварительная стоимость заявки или комиссия не совпала с расчетом.")
        buffer = (notional * Decimal("0.005")).quantize(Decimal("0.01"), rounding=ROUND_UP)
        estimated = broker_total + buffer if direction == "buy" else commission + buffer
        if direction == "buy" and estimated > Decimal(str(snapshot["cash"])):
            raise SandboxError("INSUFFICIENT_CASH")
        if direction == "sell":
            position = next((row for row in snapshot["positions"] if row["instrument_uid"] == instrument_uid), None)
            if not position or position["blocked"] or quantity > position["available_quantity"]:
                raise SandboxError("INSUFFICIENT_POSITION")
        return {"instrument_uid": instrument_uid, "ticker": instrument["ticker"], "name": instrument["name"], "lot": instrument["lot"], "lots": lots, "quantity": quantity, "limit_price": float(price), "direction": direction, "notional": float(notional), "cash": snapshot["cash"], "buffer": float(buffer), "commission": float(commission), "broker_total": float(broker_total), "estimated_total": float(estimated), "quote_policy": quote_policy, "quote_age_seconds": round(quote_age, 1), "price_time": instrument["price_time"], "waiting_market": waiting, "waiting_reason": "Торги закрыты; заявка будет отправлена после открытия с указанными ценой и количеством." if waiting else "", "warnings": ["Песочница: допускается последняя цена закрытого рынка; требование свежести 30 секунд не выполнено."] if quote_policy == "sandbox_last_close" and quote_age > 30 else []}

    @bounded
    def place_order(self, account_id, plan, order_id, before_send=None):
        if not isinstance(plan, dict):
            raise SandboxError("INVALID_ORDER")
        try:
            if not isinstance(order_id, str) or str(uuid.UUID(order_id)) != order_id:
                raise ValueError()
        except (ValueError, AttributeError):
            raise SandboxError("INVALID_ORDER", "Нужен сохраненный UUID заявки.") from None
        fresh = self.preview_order(account_id, plan.get("instrument_uid"), plan.get("lots"), plan.get("limit_price"), plan.get("direction"), plan.get("quote_policy", "strict_30s"))
        if any(plan.get(key) != fresh[key] for key in ("instrument_uid", "lot", "lots", "quantity", "limit_price", "direction", "notional", "buffer", "commission", "broker_total", "estimated_total", "quote_policy")):
            raise SandboxError("INVALID_ORDER", "Параметры расчета изменились. Создайте новый расчет.")
        payload = {"accountId": account_id, "instrumentId": fresh["instrument_uid"], "quantity": str(fresh["lots"]), "price": quote(Decimal(str(fresh["limit_price"]))), "direction": "ORDER_DIRECTION_" + fresh["direction"].upper(), "orderType": "ORDER_TYPE_LIMIT", "orderId": order_id, "timeInForce": "TIME_IN_FORCE_DAY", "priceType": "PRICE_TYPE_CURRENCY", "confirmMarginTrade": False}
        with before_send() if before_send else nullcontext():
            result = self._write("SandboxService/PostSandboxOrder", payload)
        try:
            order = self._order(result, order_id)
            if order["instrument_uid"] != fresh["instrument_uid"] or order["direction"] != fresh["direction"] or order["lots_requested"] != fresh["lots"] or order["request_id"] != order_id:
                raise SandboxError("INVALID_RESPONSE")
            return order
        except (SandboxError, TInvestError):
            raise SandboxError("UNKNOWN_EXECUTION") from None

    @bounded
    def order_state(self, account_id, order_id, request=True):
        self._account(account_id)
        if not isinstance(request, bool):
            raise SandboxError("INVALID_ORDER")
        data = self._call("SandboxService/GetSandboxOrderState", {"accountId": account_id, "orderId": identifier(order_id), "orderIdType": "ORDER_ID_TYPE_REQUEST" if request else "ORDER_ID_TYPE_EXCHANGE", "priceType": "PRICE_TYPE_CURRENCY"})
        result = self._order(data, order_id if request else None)
        if request and result["request_id"] != order_id or not request and result["order_id"] != order_id:
            raise SandboxError("RECONCILIATION")
        return result

    @bounded
    def cancel_order(self, account_id, order_id, request=False):
        self._account(account_id)
        if not isinstance(request, bool):
            raise SandboxError("INVALID_ORDER")
        result = self._write("SandboxService/CancelSandboxOrder", {"accountId": account_id, "orderId": identifier(order_id), "orderIdType": "ORDER_ID_TYPE_REQUEST" if request else "ORDER_ID_TYPE_EXCHANGE"})
        return {"order_id": order_id, "request": request, "cancel_requested": True, "time": label(result["time"], 64) if result.get("time") else None}
