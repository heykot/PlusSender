"""Monobank webhook — автоматична видача доступу після оплати в банку.

Як це працює:
  1. Користувач відкриває посилання банки і платить будь-яку суму.
  2. У коментарі до платежу вказує свій Telegram user_id (показується у /start).
  3. Monobank надсилає POST-запит на /mono-webhook/<MONO_WEBHOOK_SECRET>.
  4. Тіло webhook-а НЕ вважається достовірним (Monobank його не підписує):
     беремо лише id транзакції і знаходимо її у виписці банки через
     /personal/statement. Суму й коментар беремо саме з виписки.
  5. Кожна транзакція зараховується один раз (processed_payments.json).
  6. Визначаємо тариф за сумою, видаємо доступ і пишемо в Telegram.

Налаштування в .env:
  MONO_TOKEN          — Personal token з api.monobank.ua
  MONO_JAR_ID         — ID банки (з client-info → jars[].id)
  MONO_WEBHOOK_SECRET — випадковий рядок; входить у шлях webhook-а
  MONO_WEBHOOK_URL    — базовий публічний URL (…/mono-webhook), секрет додається сам
  MONO_WEBHOOK_PORT   — порт aiohttp (default 8080)
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from logging.handlers import RotatingFileHandler
from typing import Optional

import aiohttp
from aiohttp import web

from .config import PROJECT_ROOT, REFERRAL_BONUS_DAYS, Settings

log = logging.getLogger(__name__)

# ── Окремий лог платежів ──────────────────────────────────────────────────────
_pay_log = logging.getLogger("plus_sender.payments")


def _setup_payment_log() -> None:
    if _pay_log.handlers:
        return
    logs_dir = PROJECT_ROOT / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(
        logs_dir / "payments.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=10,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter(
        fmt="%(asctime)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    _pay_log.addHandler(fh)
    _pay_log.propagate = True


_setup_payment_log()


def _log_payment(status: str, **kwargs) -> None:
    parts = [f"[{status}]"]
    for k, v in kwargs.items():
        parts.append(f"{k}={v}")
    _pay_log.info("  ".join(parts))

# ─── Тарифна сітка: (мін. сума UAH, кількість днів) ───────────────────────
# Сортуємо від більшого до меншого — беремо перший що підходить.
# Мінімум — нова (акційна) ціна. Хто платить більше (наприклад стару ціну) —
# теж потрапляє в той самий тариф, оскільки матчинг = "сума >= мінімум".
PLANS_UAH: list[tuple[int, int]] = [
    (2500, 365),
    (1300, 180),
    (700,  90),
    (250,  30),
]


def _amount_to_days(kopecks: int) -> int:
    """Конвертує суму в копійках у кількість днів доступу. 0 = не підходить."""
    uah = kopecks / 100
    for min_uah, days in PLANS_UAH:
        if uah >= min_uah:
            return days
    return 0


def _tg_id_from_comment(comment: str) -> Optional[int]:
    """Витягує Telegram user_id з коментаря платежу.

    Підтримувані формати коментаря:
      "123456789"          → 123456789
      "id: 123456789"      → 123456789
      "tg 123456789"       → 123456789
      "telegram:123456789" → 123456789
    """
    m = re.search(r"\b(\d{5,12})\b", comment or "")
    return int(m.group(1)) if m else None


# ─── Перевірка платежу через виписку Monobank ────────────────────────────────
STATEMENT_URL = "https://api.monobank.ua/personal/statement/{account}/{frm}/{to}"
_STATEMENT_MIN_INTERVAL = 61   # ліміт Monobank: 1 запит виписки на 60 с
_STATEMENT_MAX_RANGE = 31 * 24 * 3600
_STATEMENT_CACHE_LIMIT = 500


class StatementUnavailable(Exception):
    """Виписку не вдалося отримати (мережа, 429, 5xx…) — платіж лишається неперевіреним."""


class StatementVerifier:
    """Шукає транзакцію за id у виписці банки. Запити серіалізовані й
    рознесені мінімум на 60 с, отримані транзакції кешуються."""

    def __init__(self, token: str, account: str) -> None:
        self._token = token
        self._account = account
        self._lock = asyncio.Lock()
        self._last_call = 0.0
        self._items: dict[str, dict] = {}

    async def _fetch(self, frm: int, to: int) -> list[dict]:
        wait = self._last_call + _STATEMENT_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_call = time.monotonic()

        url = STATEMENT_URL.format(account=self._account, frm=frm, to=to)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
                async with session.get(url, headers={"X-Token": self._token}) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        raise StatementUnavailable(f"HTTP {resp.status}: {text[:200]}")
                    payload = await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise StatementUnavailable(f"{type(exc).__name__}: {exc}") from exc

        if not isinstance(payload, list):
            raise StatementUnavailable("неочікуваний формат виписки")
        return [it for it in payload if isinstance(it, dict)]

    def _remember(self, items: list[dict]) -> None:
        for it in items:
            if it.get("id"):
                self._items[str(it["id"])] = it
        while len(self._items) > _STATEMENT_CACHE_LIMIT:
            self._items.pop(next(iter(self._items)))

    async def lookup(self, item_id: str, claimed_time: int) -> Optional[dict]:
        """Повертає транзакцію з виписки або None, якщо її там немає.
        Кидає StatementUnavailable, якщо виписку так і не вдалося отримати."""
        async with self._lock:
            not_found = 0
            last_error: Optional[StatementUnavailable] = None
            for _ in range(3):
                if item_id in self._items:
                    return self._items[item_id]
                now = int(time.time())
                frm = max(now - _STATEMENT_MAX_RANGE + 3600, min(claimed_time, now) - 3600)
                try:
                    self._remember(await self._fetch(frm, now))
                except StatementUnavailable as exc:
                    last_error = exc
                    log.warning("mono statement: %s", exc)
                    continue
                if item_id in self._items:
                    return self._items[item_id]
                # Друга спроба — на випадок, якщо виписка трохи відстає від webhook-а
                not_found += 1
                if not_found >= 2:
                    return None
            if not_found:
                return None
            raise StatementUnavailable(str(last_error))


# ─── HTTP-обробники ──────────────────────────────────────────────────────────
def _secret_ok(request: web.Request) -> bool:
    return hmac.compare_digest(
        request.match_info.get("secret", "").encode(),
        request.app["mono_secret"].encode(),
    )


async def mono_webhook_check(request: web.Request) -> web.Response:
    """GET, яким Monobank перевіряє URL під час реєстрації webhook-а."""
    return web.Response(status=200 if _secret_ok(request) else 404)


async def mono_webhook_legacy(request: web.Request) -> web.Response:
    log.error(
        "mono_webhook: запит на старий шлях /mono-webhook без секрету від %s — ігноровано. "
        "Перереєструйте webhook з MONO_WEBHOOK_SECRET.",
        request.remote,
    )
    return web.Response(status=404)


async def mono_webhook_handler(request: web.Request) -> web.Response:
    """Обробник POST /mono-webhook/<secret> від Monobank.

    Відповідає одразу (Monobank чекає ≤5 с), а перевірка й зарахування
    виконуються у фоні: запит виписки може чекати до хвилини через ліміт API.
    """
    if not _secret_ok(request):
        log.warning("mono_webhook: невірний секрет від %s", request.remote)
        return web.Response(status=404)

    try:
        data = await request.json()
    except Exception:
        log.warning("mono_webhook: не вдалося розпарсити JSON")
        return web.Response(status=400)

    body = data.get("data") if isinstance(data, dict) else None
    if not isinstance(body, dict):
        log.info("mono_webhook: тестовий ping від Monobank — OK")
        return web.Response(status=200)

    if body.get("account", "") != request.app["mono_jar_id"]:
        log.debug("mono_webhook: чужий account=%s", body.get("account"))
        return web.Response(status=200)

    stmt = body.get("statementItem") or {}
    item_id = str(stmt.get("id") or "")
    try:
        amount = int(stmt.get("amount") or 0)
        claimed_time = int(stmt.get("time") or time.time())
    except (TypeError, ValueError):
        return web.Response(status=200)
    if amount <= 0:
        return web.Response(status=200)
    if not item_id:
        log.warning("mono_webhook: надходження без id транзакції — ігноровано")
        return web.Response(status=200)

    task = asyncio.create_task(_process_payment(request.app, item_id, claimed_time))
    tasks: set = request.app["tasks"]
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return web.Response(status=200)


async def _process_payment(app: web.Application, item_id: str, claimed_time: int) -> None:
    from .storage import (
        ensure_profile_for_id,
        extend_access_days,
        is_payment_processed,
        mark_payment_processed,
    )

    bot = app["bot"]
    try:
        if is_payment_processed(item_id):
            log.info("mono_webhook: транзакція %s уже оброблена — пропускаю", item_id)
            return

        verifier: StatementVerifier = app["verifier"]
        try:
            item = await verifier.lookup(item_id, claimed_time)
        except StatementUnavailable as exc:
            _log_payment("UNVERIFIED", id=item_id, error=str(exc))
            await _notify_admins_unverified(bot, item_id, str(exc))
            return
        if item is None:
            _log_payment("NOT_IN_STATEMENT", id=item_id)
            log.warning("mono_webhook: транзакції %s немає у виписці — можлива підробка", item_id)
            return

        # Далі — тільки дані з виписки, не з тіла webhook-а
        amount = int(item.get("amount") or 0)
        comment: str = item.get("comment") or ""
        description: str = item.get("description") or ""
        uah_str = f"{amount / 100:.0f}"
        if amount <= 0:
            return

        async with app["lock"]:
            if is_payment_processed(item_id):
                return

            tg_uid = _tg_id_from_comment(comment) or _tg_id_from_comment(description)
            if tg_uid is None:
                mark_payment_processed(item_id, status="NO_ID", amount=amount)
                _log_payment("NO_ID", id=item_id, amount_uah=uah_str, comment=repr(comment))
                log.info("mono_webhook: платіж %s грн без Telegram ID (comment=%r)", uah_str, comment)
                await _notify_admins_unknown(bot, amount, comment)
                return

            days = _amount_to_days(amount)
            if days == 0:
                mark_payment_processed(item_id, status="LOW_AMOUNT", amount=amount, uid=tg_uid)
                _log_payment("LOW_AMOUNT", id=item_id, uid=tg_uid, amount_uah=uah_str, comment=repr(comment))
                log.info("mono_webhook: сума %s грн не відповідає жодному тарифу (uid=%d)", uah_str, tg_uid)
                return

            # Профіль міг ще не існувати (оплатив до /start) — створюємо, щоб доступ не загубився
            ensure_profile_for_id(tg_uid)
            new_until = extend_access_days(tg_uid, days) or "невідомо"
            mark_payment_processed(
                item_id, status="SUCCESS", amount=amount, uid=tg_uid, days=days, until=new_until,
            )

            _log_payment(
                "SUCCESS",
                id=item_id,
                uid=tg_uid,
                amount_uah=uah_str,
                days=days,
                access_until=new_until,
                comment=repr(comment),
            )
            log.info("✅ Mono payment: uid=%d  amount=%s грн  days=%d → access_until=%s",
                     tg_uid, uah_str, days, new_until)

            # Реферальна нагорода — під тим самим локом, щоб дві одночасні
            # оплати одного користувача не нарахували бонус двічі
            await _reward_referrer_if_any(bot, tg_uid)

        # ── Повідомляємо користувача ──
        label = _days_label(days)
        try:
            await bot.send_message(
                tg_uid,
                f"✅  <b>Оплату отримано!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Сума: <b>{uah_str} грн</b>\n"
                f"📦 Тариф: <b>{label}</b>\n"
                f"📅 Доступ до: <b>{new_until}</b>\n\n"
                f"Дякуємо за підтримку! 🚀",
                parse_mode="HTML",
            )
        except Exception as exc:
            _log_payment("NOTIFY_FAIL", uid=tg_uid, error=str(exc))
            log.warning("mono_webhook: не вдалося написати uid=%d: %s", tg_uid, exc)

        # ── Сповіщаємо адмінів ──
        await _notify_admins_success(bot, tg_uid, uah_str, label, new_until)
    except Exception:
        log.exception("mono_webhook: помилка обробки транзакції %s", item_id)


async def _reward_referrer_if_any(bot, buyer_uid: int) -> None:
    """Якщо в платника є запрошувач і нагорода ще не нарахована —
    видає +REFERRAL_BONUS_DAYS днів запрошувачу і пише йому в Telegram."""
    from .storage import (
        extend_access_days,
        get_referrer,
        grant_access_days,
        is_referral_rewarded,
        iter_user_files,
        load_user_json,
        mark_referral_rewarded,
    )

    # Знаходимо профіль платника
    buyer_data: Optional[dict] = None
    for path in iter_user_files():
        data = load_user_json(path)
        if data.get("user_id") == buyer_uid:
            buyer_data = data
            break
    if not buyer_data:
        return

    referrer_id = get_referrer(buyer_data)
    if not referrer_id:
        return
    if is_referral_rewarded(buyer_data):
        log.debug("referral: уже нагороджено за uid=%d", buyer_uid)
        return

    # Видаємо реферу бонусні дні
    new_until = extend_access_days(referrer_id, REFERRAL_BONUS_DAYS)
    if new_until is None:
        new_until = grant_access_days(referrer_id, REFERRAL_BONUS_DAYS)
    if new_until is None:
        log.warning(
            "referral: профіль реферера uid=%d не знайдено — бонус не видано",
            referrer_id,
        )
        return

    # Фіксуємо що нагорода видана (щоб не повторити при наступних оплатах того ж юзера)
    mark_referral_rewarded(buyer_uid)

    _log_payment(
        "REFERRAL_REWARD",
        referrer=referrer_id,
        buyer=buyer_uid,
        days=REFERRAL_BONUS_DAYS,
        new_until=new_until,
    )
    log.info(
        "🎁 Referral reward: referrer=%d (+%d дн.) buyer=%d new_until=%s",
        referrer_id, REFERRAL_BONUS_DAYS, buyer_uid, new_until,
    )

    # Повідомляємо запрошувача
    try:
        await bot.send_message(
            referrer_id,
            f"🎁  <b>Бонус +{REFERRAL_BONUS_DAYS} днів!</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Ваш друг купив тариф — дякуємо, що поділились ботом!\n\n"
            f"📅 Ваш доступ продовжено до:  <b>{new_until}</b>\n\n"
            f"<i>Запрошуйте більше друзів — отримуйте більше днів. "
            f"Посилання — у меню «🎁 Запросити друзів».</i>",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.warning("referral: не вдалось повідомити реферера %d: %s", referrer_id, exc)


def _days_label(days: int) -> str:
    labels = {30: "30 днів", 90: "90 днів", 180: "180 днів", 365: "365 днів"}
    return labels.get(days, f"{days} днів")


async def _notify_admins_success(bot, tg_uid: int, uah: str, label: str, until: str) -> None:
    from .storage import load_admins
    admins = load_admins()
    if not admins:
        return
    text = (
        f"💳  <b>Нова оплата Monobank</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 UID: <code>{tg_uid}</code>\n"
        f"💰 {uah} грн — <b>{label}</b>\n"
        f"📅 До: <b>{until}</b>"
    )
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as exc:
            log.debug("notify admins failed for %d: %s", admin_id, exc)


async def _notify_admins_unknown(bot, amount: int, comment: str) -> None:
    from html import escape
    from .storage import load_admins
    admins = load_admins()
    if not admins:
        return
    text = (
        f"⚠️  <b>Оплата Monobank без Telegram ID</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Сума: <b>{amount / 100:.0f} грн</b>\n"
        f"📝 Коментар: <code>{escape(comment) or '—'}</code>\n\n"
        f"<i>Зв'яжіться з платником вручну.</i>"
    )
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as exc:
            log.debug("notify admins (unknown) failed for %d: %s", admin_id, exc)


async def _notify_admins_unverified(bot, item_id: str, error: str) -> None:
    from html import escape
    from .storage import load_admins
    admins = load_admins()
    if not admins:
        return
    text = (
        f"⚠️  <b>Не вдалося перевірити оплату Monobank</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🧾 Транзакція: <code>{escape(item_id)}</code>\n"
        f"❗ {escape(error[:300])}\n\n"
        f"<i>Доступ НЕ видано. Перевірте виписку банки вручну.</i>"
    )
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as exc:
            log.debug("notify admins (unverified) failed for %d: %s", admin_id, exc)


def build_app(bot, settings: Settings) -> web.Application:
    """Будує aiohttp Application з webhook-ендпоінтом."""
    app = web.Application()
    app["bot"] = bot
    app["mono_jar_id"] = settings.mono_jar_id
    app["mono_secret"] = settings.mono_webhook_secret
    app["verifier"] = StatementVerifier(settings.mono_token, settings.mono_jar_id)
    app["lock"] = asyncio.Lock()
    app["tasks"] = set()
    app.router.add_post("/mono-webhook/{secret}", mono_webhook_handler)
    app.router.add_get("/mono-webhook/{secret}", mono_webhook_check)
    app.router.add_post("/mono-webhook", mono_webhook_legacy)
    return app


async def register_webhook(mono_token: str, webhook_url: str) -> None:
    """Реєструє webhook у Monobank API (викликати один раз при старті)."""
    url = "https://api.monobank.ua/personal/webhook"
    headers = {"X-Token": mono_token}
    payload = {"webHookUrl": webhook_url}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            text = await resp.text()
            if resp.status == 200:
                # Останній сегмент — секрет, у лог його не пишемо
                log.info("✅ Monobank webhook зареєстровано: %s/***", webhook_url.rsplit("/", 1)[0])
            else:
                log.warning("⚠️ Monobank webhook помилка %d: %s", resp.status, text)
