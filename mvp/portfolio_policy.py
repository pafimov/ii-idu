import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP

from engine import ValidationError, number, text


STRATEGIES = (
    {"id": "reserve", "name": "Осторожный", "risk_level": 1, "description": "85% в рублях, 15% в фонде золота. Небольшая доля вложений может колебаться.", "weights": {"RUB": 85, "TGLD": 15}},
    {"id": "moderate", "name": "Умеренный", "risk_level": 2, "description": "60% в рублях, остальное в фондах и акциях. Больше вложений и больше колебаний.", "weights": {"RUB": 60, "TGLD": 20, "TMOS": 10, "SBER": 5, "LKOH": 5}},
    {"id": "balanced", "name": "Сбалансированный", "risk_level": 3, "description": "40% в рублях, 60% в фондах и российских акциях. Стоимость вложений может заметно меняться.", "weights": {"RUB": 40, "TGLD": 20, "TMOS": 25, "SBER": 10, "LKOH": 5}},
    {"id": "growth", "name": "Динамичный", "risk_level": 4, "description": "10% в рублях, большая часть в фондах и акциях. Возможны сильные колебания стоимости.", "weights": {"RUB": 10, "TGLD": 20, "TMOS": 35, "SBER": 20, "LKOH": 15}},
)


def utcnow():
    return datetime.now(timezone.utc)


def allowed(profile, strategy_id):
    if not profile:
        return "Сначала укажите цель и подтвердите профиль."
    if profile.get("expensive_debt"):
        return "В профиле указан дорогой долг. Сначала пересмотрите доступную сумму."
    if profile.get("reserve_months", 0) < 3:
        return "Нужен резерв минимум на три месяца."
    if profile.get("excluded_sectors"):
        return "В этой корзине нельзя проверить исключённые секторы внутри фондов."
    if strategy_id == "balanced" and (profile["horizon_years"] < 3 or profile["risk_tolerance"] == "low" or profile["loss_reaction"] == "sell"):
        return "Эта стратегия требует горизонта от трёх лет и готовности к колебаниям."
    if strategy_id == "growth" and (profile["horizon_years"] < 5 or profile["risk_tolerance"] != "high" or profile["experience"] == "none" or profile["loss_reaction"] == "sell"):
        return "Нужны горизонт от пяти лет, опыт и высокая готовность к риску."
    return None


def strategies(profile):
    return [{**s, "available": not allowed(profile, s["id"]), "blocked_reason": allowed(profile, s["id"])} for s in STRATEGIES]


def demo_strategies():
    return [{**strategy, "weights": dict(strategy["weights"])} for strategy in STRATEGIES]


def validate_demo_config(data):
    if not isinstance(data.get("strategy_id"), str) or data["strategy_id"] not in {strategy["id"] for strategy in STRATEGIES}:
        raise ValidationError("Выберите один из четырёх вариантов риска.", {"strategy_id": "Нужно выбрать вариант."})
    years = number(data.get("horizon_years"), "horizon_years", low=1, high=40)
    if not years.is_integer():
        raise ValidationError("Укажите срок в полных годах.", {"horizon_years": "Целое число от 1 до 40."})
    return {"strategy_id": data["strategy_id"], "goal_name": text(data.get("goal_name"), "goal_name", 80),
            "target_amount": number(data.get("target_amount"), "target_amount", low=1_000, high=1_000_000_000),
            "horizon_years": int(years), "monthly_contribution": number(data.get("monthly_contribution"), "monthly_contribution", high=10_000_000)}


def make_demo_policy(config, account_id, version, config_version):
    strategy = next(strategy for strategy in STRATEGIES if strategy["id"] == config["strategy_id"])
    created = utcnow()
    weights = {ticker: strategy["weights"].get(ticker, 0) for ticker in ("RUB", "TGLD", "TMOS", "SBER", "LKOH")}
    return {**strategy, "weights": weights, "id": str(uuid.uuid4()), "strategy_id": strategy["id"],
            "mode": "demo", "config_version": config_version, "demo_config": dict(config), "version": version,
            "account_id": account_id, "cash_reserve_percent": strategy["weights"]["RUB"], "rebalance_threshold": 5,
            "max_weights": {ticker: min(100, weight + 5) if weight else 0 for ticker, weight in weights.items() if ticker != "RUB"},
            "created_at": created.isoformat(timespec="seconds"), "expires_at": (created + timedelta(days=30)).isoformat(timespec="seconds"),
            "environment": "sandbox", "quote_policy": "sandbox_last_close", "service_fee": 0}


def make_policy(profile, strategy_id, account_id, version):
    strategy = next((s for s in STRATEGIES if s["id"] == strategy_id), None)
    if not strategy:
        raise ValidationError("Выберите стратегию из списка.")
    reason = allowed(profile, strategy_id)
    if reason:
        raise ValidationError(reason)
    created = utcnow()
    return {**strategy, "id": str(uuid.uuid4()), "strategy_id": strategy_id, "account_id": account_id,
            "version": version, "profile_version": profile["version"], "profile": dict(profile),
            "cash_reserve_percent": strategy["weights"]["RUB"], "rebalance_threshold": 5,
            "max_weights": {ticker: min(100, weight + 5) for ticker, weight in strategy["weights"].items() if ticker != "RUB"},
            "created_at": created.isoformat(timespec="seconds"),
            "expires_at": (created + timedelta(days=30)).isoformat(timespec="seconds"),
            "environment": "sandbox", "service_fee": 0}


def limit_price(price, tick, direction="buy"):
    value, step = Decimal(str(price)), Decimal(str(tick))
    if not value.is_finite() or not step.is_finite() or value <= 0 or step <= 0:
        raise ValidationError("Нет корректной цены инструмента.")
    rounding = ROUND_UP if direction == "buy" else ROUND_DOWN
    multiplier = Decimal("1.003") if direction == "buy" else Decimal("0.997")
    return float((value * multiplier / step).to_integral_value(rounding=rounding) * step)


def rebalance_orders(policy, snapshot, catalog):
    instruments = {p["ticker"]: p for p in catalog if p.get("available")}
    held = {p["ticker"]: p for p in snapshot["positions"] if p.get("asset_class") != "cash"}
    unsupported = set(held) - (set(policy["weights"]) - {"RUB"})
    if unsupported:
        raise ValidationError("В счёте есть позиции вне правил: " + ", ".join(sorted(unsupported)) + ".")
    total = Decimal(str(snapshot["total_value"]))
    plans = []
    for ticker, weight in policy["weights"].items():
        if ticker == "RUB":
            continue
        if not weight and not held.get(ticker, {}).get("quantity"):
            continue
        item = instruments.get(ticker)
        if not item:
            raise ValidationError("Нет доступных торговых данных для " + ticker + ".")
        quantity = Decimal(str(held.get(ticker, {}).get("quantity", 0)))
        value = quantity * Decimal(str(item["price"]))
        target = total * Decimal(weight) / 100
        if total and abs(value - target) / total * 100 <= Decimal(str(policy["rebalance_threshold"])) and quantity:
            continue
        direction = "buy" if target > value else "sell"
        price = Decimal(str(limit_price(item["price"], item["tick_size"], direction)))
        lot = int(item["lot"])
        budget = abs(target - value)
        lots = int((budget / (price * lot * Decimal("1.01"))).to_integral_value(rounding=ROUND_DOWN))
        if direction == "sell":
            lots = min(lots, int(quantity / lot))
        if lots:
            plans.append({"instrument_uid": item["instrument_uid"], "ticker": ticker,
                          "name": item["name"], "direction": direction, "lots": lots,
                          "quantity": lots * lot, "lot": lot, "limit_price": float(price)})
    return sorted(plans, key=lambda p: 0 if p["direction"] == "sell" else 1)


def insights(snapshot, policy):
    total = snapshot.get("total_value", 0)
    if not total:
        return [{"title": "Счёт пуст", "text": "Пополните счёт песочницы и выберите стратегию."}]
    positions = [p for p in snapshot["positions"] if p.get("asset_class") != "cash"]
    result = []
    if positions:
        largest = max(positions, key=lambda p: p["quantity"] * p["price"])
        share = largest["quantity"] * largest["price"] / total * 100
        result.append({"title": "Крупнейшая позиция — " + largest["ticker"], "text": f"{share:.1f}% стоимости счёта. Состав фондов здесь не раскрывается."})
    cash_share = snapshot.get("cash", 0) / total * 100
    cash_text = "Это деньги внутри портфеля; их целевая доля задана выбранной стратегией." if policy and policy.get("mode") == "demo" else "Это деньги внутри портфеля; резерв из вашего профиля считается отдельно."
    result.append({"title": f"В рублях — {cash_share:.1f}%", "text": cash_text})
    if policy:
        deviations = []
        for ticker, weight in policy["weights"].items():
            value = snapshot["cash"] if ticker == "RUB" else sum(p["quantity"] * p["price"] for p in positions if p["ticker"] == ticker)
            deviation = value / total * 100 - weight
            if abs(deviation) > policy["rebalance_threshold"]:
                deviations.append(f"{ticker}: {deviation:+.1f} п. п.")
        result.append({"title": "Доли отличаются от правил" if deviations else "Доли в пределах правил", "text": "; ".join(deviations) if deviations else f"Отклонения не превышают {policy['rebalance_threshold']} п. п."})
    goal = policy.get("demo_config") if policy and policy.get("mode") == "demo" else policy.get("profile") if policy else None
    if goal:
        profile = goal
        months = profile["horizon_years"] * 12
        planned = total + profile["monthly_contribution"] * months
        target = profile["target_amount"] if "target_amount" in profile else profile["goal_amount"]
        gap = max(0, target - planned)
        if gap:
            needed = max(0, (target - total) / months)
            result.append({"title": "Взносов до цели пока не хватает", "text": f"Без роста цен через {profile['horizon_years']} лет получится {planned:,.0f} ₽. До цели не хватает {gap:,.0f} ₽. Расчётный взнос — {needed:,.0f} ₽ в месяц; инфляция и расходы не включены.".replace(",", " ")})
        else:
            result.append({"title": "Цель покрывается взносами", "text": "Текущая стоимость и запланированные взносы покрывают цель без роста цен. Инфляция и расходы не включены."})
    return result
