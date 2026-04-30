"""Monobank webhook — автоматична видача доступу після оплати в банку.

Як це працює:
  1. Користувач відкриває посилання банки і платить будь-яку суму.
  2. У коментарі до платежу вказує свій Telegram user_id (показується у /start).
  3. Monobank надсилає POST-запит на /mono-webhook.
  4. Ми визначаємо тариф за сумою, видаємо доступ і пишемо в Telegram.

Налаштування в .env:
  MONO_TOKEN   — Personal token з api.monobank.ua
  MONO_JAR_ID  — ID банки (з client-info → jars[].id)
  MONO_WEBHOOK_PORT — порт aiohttp (default 8080)
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

from aiohttp import web

from .config import PROJECT_ROOT

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
# Сортуємо від більшого до меншого — беремо перший що підходить
PLANS_UAH: list[tuple[int, int]] = [
    (2800, 365),
    (1500, 180),
    (800,  90),
    (300,  30),
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
    import re
    m = re.search(r"\b(\d{5,12})\b", comment or "")
    return int(m.group(1)) if m else None


async def mono_webhook_handler(request: web.Request) -> web.Response:
    """Обробник POST /mono-webhook від Monobank."""
    jar_id: str = request.app["mono_jar_id"]
    bot = request.app["bot"]

    # ── Читаємо тіло ──
    try:
        data = await request.json()
    except Exception:
        log.warning("mono_webhook: не вдалося розпарсити JSON")
        return web.Response(status=400)

    # Monobank надсилає тестовий запит без поля data при реєстрації webhook
    if "data" not in data:
        log.info("mono_webhook: тестовий ping від Monobank — OK")
        return web.Response(status=200)

    account = data["data"].get("account", "")
    stmt = data["data"].get("statementItem", {})

    # Перевіряємо що це саме наша банка
    if jar_id and account != jar_id:
        log.debug("mono_webhook: чужий account=%s, очікуємо %s", account, jar_id)
        return web.Response(status=200)

    # Тільки надходження (amount > 0)
    amount: int = stmt.get("amount", 0)
    if amount <= 0:
        return web.Response(status=200)

    comment: str = stmt.get("comment", "") or ""
    description: str = stmt.get("description", "") or ""

    # Спочатку шукаємо ID у коментарі, потім у description
    tg_uid = _tg_id_from_comment(comment) or _tg_id_from_comment(description)

    if tg_uid is None:
        uah_str = f"{amount / 100:.0f}"
        _log_payment("NO_ID", amount_uah=uah_str, comment=repr(comment))
        log.info("mono_webhook: платіж %s грн без Telegram ID (comment=%r)", uah_str, comment)
        await _notify_admins_unknown(bot, amount, comment)
        return web.Response(status=200)

    days = _amount_to_days(amount)
    uah_str = f"{amount / 100:.0f}"
    if days == 0:
        _log_payment("LOW_AMOUNT", uid=tg_uid, amount_uah=uah_str, comment=repr(comment))
        log.info("mono_webhook: сума %s грн не відповідає жодному тарифу (uid=%d)", uah_str, tg_uid)
        return web.Response(status=200)

    # ── Видаємо доступ ──
    from .storage import extend_access_days, grant_access_days
    new_until = extend_access_days(tg_uid, days)
    if new_until is None:
        new_until = grant_access_days(tg_uid, days) or "невідомо"

    _log_payment(
        "SUCCESS",
        uid=tg_uid,
        amount_uah=uah_str,
        days=days,
        access_until=new_until,
        comment=repr(comment),
    )
    log.info("✅ Mono payment: uid=%d  amount=%s грн  days=%d → access_until=%s",
             tg_uid, uah_str, days, new_until)

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

    return web.Response(status=200)


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
    from .storage import load_admins
    admins = load_admins()
    if not admins:
        return
    text = (
        f"⚠️  <b>Оплата Monobank без Telegram ID</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💰 Сума: <b>{amount / 100:.0f} грн</b>\n"
        f"📝 Коментар: <code>{comment or '—'}</code>\n\n"
        f"<i>Зв'яжіться з платником вручну.</i>"
    )
    for admin_id in admins:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except Exception as exc:
            log.debug("notify admins (unknown) failed for %d: %s", admin_id, exc)


def build_app(bot, mono_jar_id: str) -> web.Application:
    """Будує aiohttp Application з webhook-ендпоінтом."""
    app = web.Application()
    app["bot"] = bot
    app["mono_jar_id"] = mono_jar_id
    app.router.add_post("/mono-webhook", mono_webhook_handler)
    return app


async def register_webhook(mono_token: str, webhook_url: str) -> None:
    """Реєструє webhook у Monobank API (викликати один раз при старті)."""
    import aiohttp as _aiohttp
    url = "https://api.monobank.ua/personal/webhook"
    headers = {"X-Token": mono_token}
    payload = {"webHookUrl": webhook_url}
    async with _aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, headers=headers) as resp:
            text = await resp.text()
            if resp.status == 200:
                log.info("✅ Monobank webhook зареєстровано: %s", webhook_url)
            else:
                log.warning("⚠️ Monobank webhook помилка %d: %s", resp.status, text)
