import json
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from engine import ValidationError, validate_positions


BASE_URL = "https://invest-public-api.tbank.ru/rest/tinkoff.public.invest.api.contract.v1."
METHODS = frozenset(("UsersService/GetAccounts", "OperationsService/GetPortfolio", "OperationsService/GetPositions", "InstrumentsService/GetInstrumentBy", "InstrumentsService/ShareBy", "InstrumentsService/BondBy", "InstrumentsService/EtfBy"))
RESPONSE_LIMIT = 2 * 1024 * 1024
MESSAGES = {
    "INVALID_TOKEN": "Введите корректный read-only токен без пробелов и переносов строк.",
    "READ_ONLY_REQUIRED": "Разрешен только токен с правами READ_ONLY. Торговый токен или неизвестный уровень прав не принимается.",
    "NO_ACCOUNTS": "У токена нет открытых брокерских счетов или ИИС с правами только для чтения. Другие типы счетов пока не поддерживаются.",
    "INVALID_ACCOUNT": "Выберите открытый счет из списка этого read-only подключения.",
    "INVALID_RESPONSE": "Брокер вернул неполные или некорректные данные. Предыдущий снимок не изменен.",
    "UNSUPPORTED_PORTFOLIO": "Состав счета пока не поддерживается. Предыдущий снимок не изменен.",
    "RECONCILIATION": "Стоимость позиций не совпала с итогом брокера. Предыдущий снимок не изменен.",
    "METADATA_LIMIT": "В счете больше 60 инструментов для загрузки справочника. Снимок не изменен.",
    "UNAUTHORIZED": "Токен не принят брокером. Проверьте срок действия и права read-only.",
    "FORBIDDEN": "Брокер запретил доступ к данным. Проверьте права токена и выбранного счета.",
    "RATE_LIMIT": "Достигнут лимит запросов брокера. Попробуйте обновить позже.",
    "TIMEOUT": "Брокер не ответил вовремя. Предыдущий снимок не изменен; попробуйте позже.",
    "NETWORK": "Не удалось установить защищенное соединение с брокером. Проверьте сеть и доверенные сертификаты.",
    "REMOTE_ERROR": "Брокер временно не смог обработать запрос. Попробуйте позже.",
    "TOO_LARGE": "Ответ брокера превышает допустимый размер. Предыдущий снимок не изменен.",
}


class TInvestError(Exception):
    def __init__(self, code, detail=""):
        self.code = code if code in MESSAGES else "REMOTE_ERROR"
        super().__init__(MESSAGES[self.code] + (" " + detail if detail else ""))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def integer(value, bound):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"-?\d{1,19}", str(value)):
        raise TInvestError("INVALID_RESPONSE")
    result = int(value)
    if abs(result) > bound:
        raise TInvestError("INVALID_RESPONSE")
    return result


def quotation(value):
    if not isinstance(value, dict):
        raise TInvestError("INVALID_RESPONSE")
    units = integer(value.get("units", 0), 9_223_372_036_854_775_807)
    nano = integer(value.get("nano", 0), 999_999_999)
    if units * nano < 0:
        raise TInvestError("INVALID_RESPONSE")
    return Decimal(units) + Decimal(nano) / 1_000_000_000


def rubles(value):
    amount = quotation(value)
    if not isinstance(value.get("currency"), str) or value["currency"].upper() != "RUB":
        raise TInvestError("UNSUPPORTED_PORTFOLIO", "Поддерживаются только рублевые цены и остатки.")
    if amount < 0:
        raise TInvestError("UNSUPPORTED_PORTFOLIO", "Отрицательные денежные значения не поддерживаются.")
    return amount


def label(value, limit=100):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or any(ord(char) < 32 for char in value):
        raise TInvestError("INVALID_RESPONSE")
    return value.strip()


def items(data, field, limit=100):
    value = data.get(field, [])
    if not isinstance(value, list) or len(value) > limit or any(not isinstance(item, dict) for item in value):
        raise TInvestError("INVALID_RESPONSE")
    return value


def identifiers(position):
    result = []
    for field in ("instrumentUid", "figi"):
        if position.get(field):
            result.append((field, label(position[field], 64)))
    if not result:
        raise TInvestError("RECONCILIATION", "Не хватает идентификатора для сверки позиций брокера.")
    return result


class TInvestClient:
    def __init__(self, token, transport=None):
        if not isinstance(token, str) or not re.fullmatch(r"[!-~]{1,2000}", token):
            raise TInvestError("INVALID_TOKEN")
        self._token = token
        self._transport = transport
        self._account_ids = None
        self._deadline = None

    def _timeout(self):
        remaining = self._deadline - time.monotonic() if self._deadline is not None else 10
        if remaining <= 0:
            raise TInvestError("TIMEOUT")
        return min(10, remaining)

    def _request(self, method, payload):
        if method not in METHODS:
            raise TInvestError("FORBIDDEN")
        request = urllib.request.Request(BASE_URL + method, json.dumps(payload).encode("utf-8"), {"Authorization": "Bearer " + self._token, "Content-Type": "application/json", "Accept": "application/json"}, method="POST")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        with opener.open(request, timeout=self._timeout()) as response:
            if response.status != 200:
                raise TInvestError("REMOTE_ERROR")
            body = response.read(RESPONSE_LIMIT + 1)
        self._timeout()
        if len(body) > RESPONSE_LIMIT:
            raise TInvestError("TOO_LARGE")
        def pairs(entries):
            result = {}
            for key, value in entries:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        def reject(_):
            raise ValueError()
        try:
            return json.loads(body.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise TInvestError("INVALID_RESPONSE") from None

    def _call(self, method, payload):
        if method not in METHODS:
            raise TInvestError("FORBIDDEN")
        try:
            self._timeout()
            data = self._transport(method, payload) if self._transport else self._request(method, payload)
            self._timeout()
        except TInvestError:
            raise
        except urllib.error.HTTPError as exc:
            code = {401: "UNAUTHORIZED", 403: "FORBIDDEN", 429: "RATE_LIMIT", 408: "TIMEOUT", 504: "TIMEOUT"}.get(exc.code, "REMOTE_ERROR")
            exc.close()
            raise TInvestError(code) from None
        except (TimeoutError, socket.timeout):
            raise TInvestError("TIMEOUT") from None
        except Exception:
            raise TInvestError("NETWORK") from None
        if not isinstance(data, dict):
            raise TInvestError("INVALID_RESPONSE")
        if "code" in data and data["code"] not in (0, "0"):
            code = {"16": "UNAUTHORIZED", "40003": "UNAUTHORIZED", "7": "FORBIDDEN", "8": "RATE_LIMIT", "4": "TIMEOUT"}.get(str(data["code"]), "REMOTE_ERROR")
            raise TInvestError(code)
        return data

    def accounts(self):
        self._account_ids = None
        data = self._call("UsersService/GetAccounts", {"status": "ACCOUNT_STATUS_ALL"})
        result = []
        for account in items(data, "accounts"):
            access = account.get("accessLevel")
            if access not in ("ACCOUNT_ACCESS_LEVEL_READ_ONLY", "ACCOUNT_ACCESS_LEVEL_NO_ACCESS"):
                raise TInvestError("READ_ONLY_REQUIRED")
            if access == "ACCOUNT_ACCESS_LEVEL_NO_ACCESS" or account.get("status") != "ACCOUNT_STATUS_OPEN":
                continue
            if account.get("type") not in ("ACCOUNT_TYPE_TINKOFF", "ACCOUNT_TYPE_TINKOFF_IIS"):
                continue
            account_id = label(account.get("id"), 64)
            if not re.fullmatch(r"[A-Za-z0-9_-]+", account_id) or any(item["id"] == account_id for item in result):
                raise TInvestError("INVALID_RESPONSE")
            result.append({"id": account_id, "name": label(account.get("name")), "type": label(account.get("type"), 60), "access_level": "READ_ONLY"})
        if not result:
            raise TInvestError("NO_ACCOUNTS")
        self._account_ids = {account["id"] for account in result}
        return result

    def _instrument(self, position, cache):
        uid, figi = position.get("instrumentUid"), position.get("figi")
        identifier = label(uid or figi, 64)
        id_type = "INSTRUMENT_ID_TYPE_UID" if uid else "INSTRUMENT_ID_TYPE_FIGI"
        kind = position["instrumentType"]
        method = {"share": "ShareBy", "bond": "BondBy", "etf": "EtfBy"}[kind]
        key = (kind, id_type, identifier)
        if key not in cache:
            if len(cache) >= 60:
                raise TInvestError("METADATA_LIMIT")
            data = self._call("InstrumentsService/" + method, {"idType": id_type, "id": identifier})
            if not isinstance(data.get("instrument"), dict):
                raise TInvestError("INVALID_RESPONSE")
            instrument = data["instrument"]
            returned = instrument.get("uid") if uid else instrument.get("figi")
            if returned != identifier:
                raise TInvestError("INVALID_RESPONSE")
            cache[key] = instrument
        return cache[key]

    def portfolio(self, account_id):
        self._deadline = time.monotonic() + 45
        try:
            return self._portfolio(account_id)
        finally:
            self._deadline = None

    def _portfolio(self, account_id):
        self.accounts()
        if not isinstance(account_id, str) or account_id not in self._account_ids:
            raise TInvestError("INVALID_ACCOUNT")
        data = self._call("OperationsService/GetPortfolio", {"accountId": account_id, "currency": "RUB"})
        balances = self._call("OperationsService/GetPositions", {"accountId": account_id})
        if data.get("accountId") != account_id or balances.get("accountId") != account_id or balances.get("limitsLoadingInProgress", False) is not False:
            raise TInvestError("INVALID_RESPONSE")
        if items(data, "virtualPositions") or items(balances, "futures") or items(balances, "options"):
            raise TInvestError("UNSUPPORTED_PORTFOLIO", "Виртуальные позиции, фьючерсы и опционы не поддерживаются.")
        allowed_totals = {"totalAmountPortfolio", "totalAmountShares", "totalAmountBonds", "totalAmountEtf", "totalAmountCurrencies"}
        for field, value in data.items():
            if (field.startswith("totalAmount") and field not in allowed_totals or field.startswith("totalVarMargin")) and quotation(value) != 0:
                raise TInvestError("UNSUPPORTED_PORTFOLIO", "На счете есть неподдерживаемые инструменты или вариационная маржа.")
        security_rows = items(balances, "securities")
        security_index, matched = {}, set()
        for index, row in enumerate(security_rows):
            if row.get("instrumentType") not in ("share", "bond", "etf") or integer(row.get("balance", 0), 9_223_372_036_854_775_807) < 0 or integer(row.get("blocked", 0), 9_223_372_036_854_775_807) < 0:
                raise TInvestError("UNSUPPORTED_PORTFOLIO", "Короткие или неподдерживаемые позиции не загружаются.")
            for key in identifiers(row):
                if key in security_index:
                    raise TInvestError("RECONCILIATION", "Брокер вернул повторяющиеся идентификаторы позиций.")
                security_index[key] = index
        for row in items(balances, "blocked"):
            if rubles(row):
                raise TInvestError("UNSUPPORTED_PORTFOLIO", "На счете заблокированы деньги. Дождитесь завершения заявок или расчетов и повторите загрузку.")
        cash_rows = items(balances, "money")
        if len(cash_rows) > 1:
            raise TInvestError("UNSUPPORTED_PORTFOLIO", "Поддерживается один рублевый денежный остаток.")
        cash = rubles(cash_rows[0]) if cash_rows else Decimal(0)
        total, accrued, normalized, cache, warnings = cash, Decimal(0), [], {}, []
        class_totals = {"share": Decimal(0), "bond": Decimal(0), "etf": Decimal(0)}
        classes = {"share": "stocks", "bond": "bonds", "etf": "funds"}
        currency_seen = False
        for row in items(data, "positions"):
            kind = row.get("instrumentType")
            quantity = quotation(row.get("quantity"))
            if quantity < 0 or kind not in (*classes, "currency"):
                raise TInvestError("UNSUPPORTED_PORTFOLIO", "Поддерживаются только длинные рублевые акции, облигации, фонды и рублевый остаток.")
            if kind == "currency":
                is_ruble = row.get("ticker") == "RUB" or row.get("figi") == "RUB000UTSTOM"
                if currency_seen or not is_ruble or abs(quantity - cash) > 1:
                    raise TInvestError("RECONCILIATION", "Рублевые остатки в ответах брокера различаются или валюта не поддерживается.")
                currency_seen = True
                continue
            if quantity == 0:
                raise TInvestError("UNSUPPORTED_PORTFOLIO", "Нулевые позиции не загружаются: обновите счет после завершения расчетов.")
            keys = identifiers(row)
            match = next((security_index[key] for key in keys if key in security_index), None)
            if match is None or match in matched:
                raise TInvestError("RECONCILIATION", "Набор бумаг между ответами брокера не совпал или содержит дубли.")
            balance = security_rows[match]
            if any(row.get(field) and balance.get(field) and row[field] != balance[field] for field in ("instrumentUid", "figi")) or balance["instrumentType"] != kind:
                raise TInvestError("RECONCILIATION", "Идентификаторы или тип бумаги между ответами брокера различаются.")
            if quantity != integer(balance.get("balance", 0), 9_223_372_036_854_775_807) + integer(balance.get("blocked", 0), 9_223_372_036_854_775_807):
                raise TInvestError("RECONCILIATION", "Количество бумаг между ответами брокера различается. Дождитесь завершения расчетов.")
            matched.add(match)
            blocked_lots = quotation(row["blockedLots"]) if "blockedLots" in row else Decimal(0)
            if not isinstance(row.get("blocked", False), bool) or blocked_lots < 0:
                raise TInvestError("INVALID_RESPONSE")
            blocked = row.get("blocked", False) or blocked_lots > 0 or int(balance.get("blocked", 0)) > 0
            if blocked and "Часть бумаг заблокирована брокером или выставленными заявками; их доступность для продажи не подтверждена." not in warnings:
                warnings.append("Часть бумаг заблокирована брокером или выставленными заявками; их доступность для продажи не подтверждена.")
            if not isinstance(row.get("currentPrice"), dict) or not isinstance(row.get("averagePositionPrice"), dict):
                raise TInvestError("INVALID_RESPONSE", "Нет текущей или средней цены позиции: дождитесь завершения расчетов у брокера.")
            current, average = rubles(row.get("currentPrice")), rubles(row.get("averagePositionPrice"))
            nkd = rubles(row.get("currentNkd")) if kind == "bond" else Decimal(0)
            instrument = self._instrument(row, cache)
            expected_kind = {"share": "INSTRUMENT_TYPE_SHARE", "bond": "INSTRUMENT_TYPE_BOND", "etf": "INSTRUMENT_TYPE_ETF"}[kind]
            if "instrumentType" in instrument and instrument["instrumentType"] != kind or "instrumentKind" in instrument and instrument["instrumentKind"] != expected_kind or str(instrument.get("currency", "")).upper() != "RUB":
                raise TInvestError("UNSUPPORTED_PORTFOLIO", "Тип или валюта справочника не совпали с поддерживаемой позицией.")
            sector = "Фонды: состав не раскрыт" if kind == "etf" else label(instrument["sector"], 60) if instrument.get("sector") else "Не указан брокером"
            if sector == "Не указан брокером" and "Справочник брокера не предоставил отрасли части инструментов. Отраслевой анализ неполный." not in warnings:
                warnings.append("Справочник брокера не предоставил отрасли части инструментов. Отраслевой анализ неполный.")
            total += quantity * (current + nkd)
            class_totals[kind] += quantity * (current + nkd)
            accrued += quantity * nkd
            normalized.append({"id": str(uuid.uuid4()), "ticker": label(instrument.get("ticker"), 24).upper(), "name": label(instrument.get("name")), "asset_class": classes[kind], "sector": sector, "currency": "RUB", "quantity": float(quantity), "price": float(current + nkd), "average_price": float(average + nkd), "source_price": float(current), "principal_average_price": float(average), "accrued_interest": float(nkd), "blocked": blocked})
        if len(matched) != len(security_rows):
            raise TInvestError("RECONCILIATION", "Набор бумаг между ответами брокера не совпал. Дождитесь завершения расчетов.")
        if cash:
            normalized.append({"id": str(uuid.uuid4()), "ticker": "RUB", "name": "Рубли", "asset_class": "cash", "sector": "Денежный остаток", "currency": "RUB", "quantity": float(cash), "price": 1, "average_price": 1})
        broker_total = rubles(data.get("totalAmountPortfolio"))
        if abs(total - broker_total) > 1:
            raise TInvestError("RECONCILIATION", f"По позициям {total:.2f} ₽, по итогу брокера {broker_total:.2f} ₽; допуск 1 ₽.")
        for field, computed in (("totalAmountShares", class_totals["share"]), ("totalAmountBonds", class_totals["bond"]), ("totalAmountEtf", class_totals["etf"]), ("totalAmountCurrencies", cash)):
            if field in data and abs(rubles(data[field]) - computed) > 1:
                raise TInvestError("RECONCILIATION", "Стоимость одного из классов активов не совпала с итогом брокера; допуск 1 ₽.")
        try:
            validate_positions(normalized)
        except ValidationError:
            raise TInvestError("UNSUPPORTED_PORTFOLIO", "Тикеры должны быть однозначными и уникальными; поддерживается до 100 позиций в пределах лимитов MVP.") from None
        warnings.append("Время оценки брокером не передано. Время загрузки не является временем обновления котировок.")
        if accrued:
            warnings.append("Стоимость облигаций включает текущий НКД. В сравнительную базу добавлен тот же текущий НКД, поэтому расчет изменения цены исключает его; купоны, налоги и комиссии не учтены.")
        if any(position["asset_class"] == "funds" for position in normalized):
            warnings.append("Состав фондов не раскрывается: отрасли и активы внутри фондов не учитываются в отраслевом анализе.")
        self._timeout()
        return {"positions": normalized, "broker_total": float(broker_total), "broker_timestamp": None, "warnings": warnings, "accrued_interest_total": float(accrued), "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "source": "tinvest", "account_id": account_id}
