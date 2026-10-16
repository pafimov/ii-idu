import csv
import io
import math
import re
import uuid
from decimal import Decimal, ROUND_HALF_UP


ASSETS = {"stocks": "Акции", "bonds": "Облигации", "funds": "Фонды", "gold": "Золото", "cash": "Деньги"}
CSV_FIELDS = ["ticker", "name", "asset_class", "sector", "quantity", "price", "average_price", "currency"]


class ValidationError(Exception):
    def __init__(self, message, fields=None):
        super().__init__(message)
        self.fields = fields or {}


def money(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def number(value, field, low=0, high=1_000_000_000, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError("Проверьте числовые поля.", {field: "Нужно число."})
    if value < low or value > high or not math.isfinite(value) or (positive and value == 0):
        raise ValidationError("Проверьте числовые поля.", {field: "Число вне допустимого диапазона."})
    return float(value)


def text(value, field, limit=100, empty=False):
    if not isinstance(value, str) or len(value.strip()) > limit or (not empty and not value.strip()):
        raise ValidationError("Проверьте заполнение полей.", {field: f"Введите текст до {limit} символов."})
    value = value.strip()
    if any(ord(ch) < 32 for ch in value):
        raise ValidationError("Недопустимые символы.", {field: "Уберите управляющие символы."})
    return value


def validate_profile(data):
    result = {"goal_name": text(data.get("goal_name"), "goal_name", 80)}
    result["goal_amount"] = number(data.get("goal_amount"), "goal_amount", low=1_000, high=1_000_000_000)
    horizon = number(data.get("horizon_years"), "horizon_years", low=1, high=40)
    if not horizon.is_integer():
        raise ValidationError("Горизонт указывается в полных годах.", {"horizon_years": "Введите целое число от 1 до 40."})
    result["horizon_years"] = int(horizon)
    result["monthly_contribution"] = number(data.get("monthly_contribution"), "monthly_contribution", high=10_000_000)
    result["reserve_months"] = number(data.get("reserve_months"), "reserve_months", high=120)
    for field, choices in (("risk_tolerance", ("low", "medium", "high")), ("experience", ("none", "some", "experienced")), ("loss_reaction", ("sell", "wait", "buy"))):
        if data.get(field) not in choices:
            raise ValidationError("Выберите ответ в анкете.", {field: "Недопустимый вариант."})
        result[field] = data[field]
    if not isinstance(data.get("expensive_debt"), bool):
        raise ValidationError("Укажите наличие дорогих кредитов.", {"expensive_debt": "Выберите да или нет."})
    result["expensive_debt"] = data["expensive_debt"]
    excluded = data.get("excluded_sectors", [])
    if not isinstance(excluded, list) or len(excluded) > 20:
        raise ValidationError("Слишком много ограничений.", {"excluded_sectors": "Не более 20 секторов."})
    result["excluded_sectors"] = list(dict.fromkeys(text(item, "excluded_sectors", 60) for item in excluded))
    return result


def validate_positions(items):
    if not isinstance(items, list) or len(items) > 100:
        raise ValidationError("В портфеле может быть не более 100 позиций.", {"positions": "Нужен список до 100 позиций."})
    result, errors, ids, tickers = [], {}, set(), set()
    total = Decimal(0)
    for i, row in enumerate(items):
        prefix = f"positions.{i}"
        if not isinstance(row, dict):
            errors[prefix] = "Нужен объект позиции."
            continue
        clean = {}
        for field, limit in (("ticker", 24), ("name", 100), ("sector", 60)):
            try:
                clean[field] = text(row.get(field), field, limit)
            except ValidationError as exc:
                errors[f"{prefix}.{field}"] = exc.fields[field]
        if "ticker" in clean:
            clean["ticker"] = clean["ticker"].upper()
            if clean["ticker"] in tickers:
                errors[f"{prefix}.ticker"] = "Тикер уже есть в портфеле. Объедините количество в одной строке и укажите среднюю цену покупки."
            tickers.add(clean["ticker"])
        if not isinstance(row.get("asset_class"), str) or row.get("asset_class") not in ASSETS:
            errors[f"{prefix}.asset_class"] = "Выберите акции, облигации, фонд, золото или деньги."
        else:
            clean["asset_class"] = row["asset_class"]
        if row.get("currency") != "RUB":
            errors[f"{prefix}.currency"] = "Поддерживается только RUB."
        clean["currency"] = "RUB"
        for field in ("quantity", "price", "average_price"):
            try:
                clean[field] = number(row.get(field), field, positive=field == "quantity")
            except ValidationError as exc:
                errors[f"{prefix}.{field}"] = exc.fields[field]
        if clean.get("asset_class") == "cash":
            if clean.get("ticker") != "RUB":
                errors[f"{prefix}.ticker"] = "Для денежного остатка используйте RUB; количество — сумма рублей."
            for field in ("price", "average_price"):
                if clean.get(field) != 1:
                    errors[f"{prefix}.{field}"] = "Для рублей цена и средняя цена должны равняться 1."
        elif clean.get("ticker") == "RUB":
            errors[f"{prefix}.asset_class"] = "Тикер RUB используется только для денежного остатка."
        position_id = row["id"] if "id" in row else str(uuid.uuid4())
        if not isinstance(position_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", position_id) or position_id in ids:
            errors[f"{prefix}.id"] = "Идентификатор должен быть уникальным."
        else:
            ids.add(position_id)
            clean["id"] = position_id
        if all(field in clean for field in ("quantity", "price", "average_price")):
            cost = Decimal(str(clean["quantity"])) * Decimal(str(clean["price"]))
            invested = Decimal(str(clean["quantity"])) * Decimal(str(clean["average_price"]))
            if max(cost, invested) > 1_000_000_000_000:
                errors[f"{prefix}.quantity"] = "Стоимость позиции должна быть до 1 трлн рублей."
            total += max(cost, invested)
        result.append(clean)
    if total > 10_000_000_000_000:
        errors["positions"] = "Стоимость портфеля должна быть до 10 трлн рублей."
    if errors:
        raise ValidationError("Портфель не сохранен. Исправьте отмеченные поля.", errors)
    return result


def import_csv(content):
    if not isinstance(content, str) or not content.strip() or len(content.encode("utf-8")) > 200_000:
        raise ValidationError("Выберите непустой CSV размером до 200 КБ.", {"csv": "Некорректный файл."})
    content = content.lstrip("\ufeff")
    first = content.splitlines()[0]
    separator = ";" if first.count(";") > first.count(",") else ","
    try:
        reader = csv.DictReader(io.StringIO(content, newline=""), delimiter=separator, strict=True)
        if not reader.fieldnames or len(set(reader.fieldnames)) != len(reader.fieldnames) or set(reader.fieldnames) != set(CSV_FIELDS):
            raise ValidationError("Заголовки CSV не совпадают с образцом.", {"csv": ", ".join(CSV_FIELDS)})
        rows, errors = [], {}
        for index, row in enumerate(reader):
            if index >= 100:
                raise ValidationError("В CSV не должно быть больше 100 позиций.")
            if None in row or any(value is None for value in row.values()):
                errors[f"row.{index + 2}"] = "Количество колонок не совпадает с заголовком."
                continue
            for field in ("quantity", "price", "average_price"):
                try:
                    row[field] = float(row[field].strip().replace(",", "."))
                except (ValueError, AttributeError):
                    errors[f"row.{index + 2}.{field}"] = "Нужно число."
            rows.append(row)
        if errors:
            raise ValidationError("CSV не импортирован. Исправьте строки файла.", errors)
        if not rows:
            raise ValidationError("В CSV нет позиций.")
        try:
            return validate_positions(rows)
        except ValidationError as exc:
            fields = {}
            for key, value in exc.fields.items():
                match = re.match(r"positions\.(\d+)\.(.+)", key)
                fields[f"row.{int(match[1]) + 2}.{match[2]}" if match else key] = value
            raise ValidationError("CSV не импортирован. Исправьте строки файла.", fields) from exc
    except csv.Error as exc:
        raise ValidationError("Не удалось прочитать CSV. Используйте образец.") from exc


def export_csv(positions):
    buffer = io.StringIO(newline="")
    fields = CSV_FIELDS + (["source_price", "principal_average_price", "accrued_interest", "blocked"] if any("source_price" in row for row in positions) else [])
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in positions:
        safe = dict(row)
        for field in ("ticker", "name", "sector"):
            if safe[field].lstrip().startswith(("=", "+", "-", "@")):
                safe[field] = "'" + safe[field]
        writer.writerow(safe)
    return "\ufeff" + buffer.getvalue()


def analytics(positions, profile):
    values = [(p, Decimal(str(p["quantity"])) * Decimal(str(p["price"]))) for p in positions]
    total = sum((v for _, v in values), Decimal(0))
    invested = sum((Decimal(str(p["quantity"])) * Decimal(str(p.get("principal_average_price", p["average_price"]))) for p in positions), Decimal(0))
    price_value = sum((Decimal(str(p["quantity"])) * Decimal(str(p.get("source_price", p["price"]))) for p in positions), Decimal(0))
    pct = lambda value: money(value / total * 100) if total else 0
    allocation = [{"key": key, "label": label, "value": money(sum(v for p, v in values if p["asset_class"] == key)), "percent": pct(sum(v for p, v in values if p["asset_class"] == key))} for key, label in ASSETS.items()]
    sectors = {}
    for position, value in values:
        sector = position["sector"]
        sectors[sector] = sectors.get(sector, Decimal(0)) + value
    sectors = [{"label": name, "value": money(value), "percent": pct(value)} for name, value in sorted(sectors.items(), key=lambda item: item[1], reverse=True)]
    largest = max(values, key=lambda pair: pair[1]) if values else None
    securities = [(p, value) for p, value in values if p["asset_class"] != "cash"]
    largest_security = max(securities, key=lambda pair: pair[1]) if securities else None
    concentration = pct(largest_security[1]) if largest_security else 0
    stocks = sum(a["percent"] for a in allocation if a["key"] in ("stocks", "funds"))
    risk_label = "Высокий" if stocks > 60 or concentration > 40 else "Умеренный" if stocks > 25 or concentration > 25 else "Низкий"
    insights = []
    if not total:
        insights.append({"title": "Добавьте свой портфель", "text": "Подключите чтение Т-Инвестиций, внесите позиции вручную или загрузите CSV. Пустой снимок не содержит активов для анализа.", "severity": "info"})
    if concentration > 30:
        insights.append({"title": "Крупная доля одной позиции", "text": f"На «{largest_security[0]['name']}» приходится {concentration:g}% портфеля. Это увеличивает зависимость от одного актива.", "severity": "warning"})
    elif largest_security and total:
        insights.append({"title": "Крупнейшая бумага ниже порога концентрации", "text": f"На «{largest_security[0]['name']}» приходится {concentration:g}% портфеля. Порог концентрации 30% не превышен. Проверка не оценивает качество эмитентов и состав фондов.", "severity": "info"})
    if stocks > 60:
        insights.append({"title": "Большая доля акций и фондов", "text": "Состав фондов не раскрывается: по одной этой доле нельзя определить их рыночный риск.", "severity": "warning"})
    if profile and profile["risk_tolerance"] == "low" and stocks > 25:
        insights.append({"title": "Сверьте состав с готовностью к риску", "text": f"Вы указали низкую готовность к риску. Акции и фонды составляют {stocks:g}% портфеля: в упрощенной классификации доля выше 25% повышает категорию риска. Состав фондов не раскрывается.", "severity": "warning"})
    if profile and profile["reserve_months"] < 3:
        insights.append({"title": "Сначала финансовая подушка", "text": "В анкете указан резерв меньше трех месяцев. Резерв ниже указанного минимума.", "severity": "warning"})
    if profile and profile["expensive_debt"]:
        insights.append({"title": "Указан дорогой кредит", "text": "Сначала оцените долговую нагрузку. Учтите это обязательство при выборе суммы.", "severity": "warning"})
    if profile and profile["excluded_sectors"]:
        excluded = [p["name"] for p in positions if p["sector"].casefold() in {s.casefold() for s in profile["excluded_sectors"]}]
        if excluded:
            insights.append({"title": "Проверьте ограничения профиля", "text": "В исключенных секторах найдены позиции: " + ", ".join(excluded) + ". Сопоставление выполнено по названиям секторов из файла.", "severity": "warning"})
    if total:
        cash = next(item for item in allocation if item["key"] == "cash")
        cash_amount = f"{cash['value']:,.0f}".replace(",", " ")
        cash_text = f"Рублевый остаток — {cash['percent']:g}% портфеля, {cash_amount} ₽." if cash["value"] else "В позициях не указан рублевый остаток."
        insights.append({"title": "Деньги внутри портфеля", "text": cash_text + " Финансовая подушка из анкеты считается отдельно; ее сумма в стоимость портфеля не добавляется.", "severity": "info"})
    scenarios = [("Падение рынка", {"stocks": .30, "bonds": .10, "funds": .25, "gold": .15, "cash": 0}), ("Стресс облигаций", {"stocks": .15, "bonds": .15, "funds": .15, "gold": .10, "cash": 0})]
    stress = []
    for label, rates in scenarios:
        loss = sum((value * Decimal(str(rates[p["asset_class"]])) for p, value in values), Decimal(0))
        stress.append({"label": label, "loss_percent": pct(loss), "loss_value": money(loss), "assumptions": {key: rate * 100 for key, rate in rates.items()}})
    goal = None
    if profile:
        months = Decimal(str(profile["horizon_years"])) * 12
        contributions = Decimal(str(profile["monthly_contribution"])) * months
        target = Decimal(str(profile["goal_amount"]))
        goal = {"target": money(target), "current": money(total), "contributions": money(contributions), "projected_no_growth": money(total + contributions), "gap": money(max(Decimal(0), target - total - contributions)), "monthly_needed": money(max(Decimal(0), (target - total) / months)), "progress": money(min(Decimal(100), total / target * 100))}
        if goal["gap"] > 0:
            gap = f"{goal['gap']:,.0f}".replace(",", " ")
            needed = f"{goal['monthly_needed']:,.0f}".replace(",", " ")
            insights.append({"title": "Взносов пока не хватает до цели", "text": f"Без роста активов до цели не хватает {gap} ₽. Расчетный взнос для закрытия разрыва — около {needed} ₽ в месяц вместо указанного вами. Сценарий не учитывает инфляцию, налоги и расходы.", "severity": "warning"})
        elif total < target:
            insights.append({"title": "Цель покрывается планом взносов", "text": "Текущая стоимость и указанные ежемесячные взносы покрывают сумму цели в сценарии без роста. Это арифметический сценарий без инфляции, налогов и расходов, не прогноз результата.", "severity": "info"})
        else:
            insights.append({"title": "Текущая стоимость достигла суммы цели", "text": "Стоимость указанных позиций не ниже суммы цели. Она рассчитана по введенным ценам; результат продажи, налоги и расходы могут отличаться.", "severity": "info"})
    return {"total_value": money(total), "invested_value": money(invested), "pnl": money(price_value - invested), "pnl_percent": money((price_value - invested) / invested * 100) if invested else None, "allocation": allocation, "sectors": sectors, "largest_position": {"name": largest[0]["name"], "percent": pct(largest[1])} if largest else None, "risk": {"label": risk_label, "stock_percent": stocks, "concentration_percent": concentration}, "insights": insights, "stress": stress, "goal": goal}


def paper_eligibility(profile):
    if not profile:
        raise ValidationError("Сначала заполните инвестиционный профиль.")
    if profile["reserve_months"] < 3:
        raise ValidationError("Для запуска тренажёра укажите резерв минимум на три месяца.")
    if profile["expensive_debt"]:
        raise ValidationError("Запуск тренажёра недоступен при указанном дорогом долге.")
    if profile["horizon_years"] < 1:
        raise ValidationError("Для запуска тренажёра нужен горизонт от одного года.")
    if profile["excluded_sectors"]:
        raise ValidationError("Тренажёр недоступна при исключенных секторах: состав искусственных инструментов нельзя проверить. Ограничения профиля сохраняются.")


def paper_plan(profile, balance):
    paper_eligibility(profile)
    stock = {"low": 15, "medium": 40, "high": 65}[profile["risk_tolerance"]]
    reasons = []
    for condition, cap, reason in ((profile["experience"] == "none", 40, "нет инвестиционного опыта"), (profile["reserve_months"] < 6, 30, "резерв меньше шести месяцев"), (profile["horizon_years"] < 3, 20, "горизонт меньше трех лет"), (profile["loss_reaction"] == "sell", 15, "желание продать при снижении")):
        if condition and stock > cap:
            stock = cap
            reasons.append(reason)
    weights = {"stocks": stock, "bonds": 80 - stock, "gold": 10}
    names = {"stocks": ("STOCK-DEMO", "Акции тренажёра"), "bonds": ("BOND-DEMO", "Облигации тренажёра"), "gold": ("GOLD-DEMO", "Золото тренажёра")}
    orders = []
    for asset, weight in weights.items():
        quantity = int(Decimal(str(balance)) * Decimal(weight) / 100 / Decimal("1.001") / 100)
        if quantity:
            ticker, name = names[asset]
            orders.append({"ticker": ticker, "name": name, "asset_class": asset, "quantity": quantity, "price": 100, "value": quantity * 100})
    value = sum(order["value"] for order in orders)
    fee = money(Decimal(value) * Decimal("0.001"))
    total = money(Decimal(value) + Decimal(str(fee)))
    remaining = money(Decimal(str(balance)) - Decimal(str(total)))
    allocation = [{"key": key, "label": ASSETS[key], "value": sum(o["value"] for o in orders if o["asset_class"] == key), "percent": money(sum(o["value"] for o in orders if o["asset_class"] == key) / balance * 100)} for key in weights]
    allocation.append({"key": "cash", "label": ASSETS["cash"], "value": remaining, "percent": money(remaining / balance * 100)})
    explanation = "Синтетические инструменты по 100 ₽, без связи с биржей. Доли определены анкетой; денежный резерв не меньше 10% начального баланса, модельная комиссия 0,1%."
    if reasons:
        explanation += " Доля акций тренажёра ограничена: " + "; ".join(reasons) + "."
    return {"orders": orders, "fee": fee, "total": total, "remaining_cash": remaining, "allocation": allocation, "explanation": explanation}
