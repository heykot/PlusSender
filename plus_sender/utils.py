"""Допоміжні функції: екранування, форматування, нормалізація, стилі."""
from __future__ import annotations

import html
from datetime import datetime
from typing import Iterable, Optional

from aiogram import types

from .config import (
    BADGE_ACTIVE,
    BADGE_INACTIVE,
    BTN_DISABLE_TEXT,
    BULLET,
    DEFAULT_ALERT_TEXT,
    DEFAULT_CLEAR_TEXT,
    DIV,
    DIV_THIN,
    EMPTY_TEXT_MARKERS,
    EMO,
    EXAMPLE_PREFIX,
    MAX_DELAY_SECONDS,
    NEXT_PREFIX,
    SOFT_ERROR_PREFIX,
    SOFT_RETRY,
    TIP_PREFIX,
)


def h(s: Optional[str]) -> str:
    """HTML-escape з підтримкою None."""
    return html.escape(s or "")


def safe_username_from(user: types.User) -> str:
    return user.username or f"user_{user.id}"


def safe_username(msg: types.Message) -> str:
    return safe_username_from(msg.from_user)


def truncate(s: Optional[str], n: int = 36) -> str:
    s = s or "—"
    return s if len(s) <= n else s[: n - 1] + "…"


def status_label(active: bool) -> str:
    return "Активно" if active else "Не активно"


def status_badge(active: bool) -> str:
    """Повертає форматований badge: '🟢 <b>Активно</b>' / '🔴 <b>Не активно</b>'."""
    return BADGE_ACTIVE if active else BADGE_INACTIVE


# ===================== Style-хелпери =====================
def step_indicator(step: int, total: int) -> str:
    """Прогрес у форматі 'Крок 2 з 4  ●●○○'."""
    step = max(1, min(step, total))
    dots = "●" * step + "○" * (total - step)
    return f"<i>Крок {step} з {total}  {dots}</i>"


def big_step_header(step: int, total: int, title: str, emoji: str = "✨") -> str:
    """Великий заголовок кроку: емоджі, номер, назва, прогрес-точки.

    Приклад:
        🔑  Крок 2 з 4
        ━━━━━━━━━━━━━━━
        ●●○○   Номер телефону
    """
    step = max(1, min(step, total))
    dots = "●" * step + "○" * (total - step)
    return (
        f"{emoji}  <b>Крок {step} з {total}</b>\n"
        f"{DIV_THIN}\n"
        f"<i>{dots}</i>   <b>{title}</b>"
    )


def tip(text: str) -> str:
    """Дружня підказка-курсив. Призначена для маленьких пояснень під полем вводу."""
    return f"{TIP_PREFIX} {text}"


def example_block(*lines: str) -> str:
    """Блок із прикладами вводу. Кожен рядок іде у <code>…</code>.

    Використовуй так:
        example_block("12345678 abcdef…", "12345678:abcdef…")
    """
    if not lines:
        return ""
    body = "\n".join(f"  <code>{line}</code>" for line in lines)
    return f"{EXAMPLE_PREFIX}\n{body}"


def next_hint(text: str) -> str:
    """«Що далі» — м'яка підказка наступної дії."""
    return f"{NEXT_PREFIX} <i>{text}</i>"


def soft_error(headline: str, body: str = "", retry: bool = True) -> str:
    """Дружня помилка: «Хм, щось не так… Спробуйте ще раз».

    Використовуй замість сухого «❌ помилка».
    """
    parts = [SOFT_ERROR_PREFIX]
    if headline:
        parts.append(f"<i>{headline}</i>")
    if body:
        parts.append(body)
    if retry:
        parts.append(SOFT_RETRY)
    return "\n".join(parts)


def warm_greeting(name: Optional[str]) -> str:
    """Тепле привітання за іменем користувача (без emoji-перегруження)."""
    safe = h(name) if name else ""
    if safe:
        return f"Привіт, <b>{safe}</b>! 👋"
    return "Привіт! 👋"


def section(title: str, body: str) -> str:
    """Секція з вертикальною рискою. Підтримує багаторядковий body."""
    indent = "   "
    body_lines = body.split("\n")
    body_indented = "\n".join(indent + line if line.strip() else line for line in body_lines)
    return f"{BULLET} <b>{title}</b>\n{body_indented}"


def card(title: str, sections: Iterable[tuple[str, str]], emoji: str = "ℹ️") -> str:
    """Стандартна 'картка' для повідомлення:

    ℹ️  <b>Заголовок</b>
    ━━━━━━━━━━━━━━━━━━━━━
    ▎ Section 1
       body
    ▎ Section 2
       body
    """
    lines = [f"{emoji}  <b>{title}</b>", DIV]
    for sec_title, sec_body in sections:
        lines.append(section(sec_title, sec_body))
        lines.append("")  # порожній рядок між секціями
    # прибираємо хвостовий порожній рядок
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def kv_line(key: str, value: str) -> str:
    """Виводить рядок 'Key:  value' з фіксованою візуальною шириною."""
    return f"{key} <b>{value}</b>"


def bullet_list(items: Iterable[str]) -> str:
    return "\n".join(f"  • {item}" for item in items)


def divider(thin: bool = False) -> str:
    return DIV_THIN if thin else DIV


# ---------- Тексти повідомлень ----------
def normalize_optional_text(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(value).strip()


def parse_text_input(raw: Optional[str]) -> Optional[str]:
    """
    Перетворює введення користувача:
      - None  -> None (не отримали текст)
      - спец-кнопка / маркери -> "" (відключити надсилання)
      - інше -> текст без зайвих пробілів
    """
    if raw is None:
        return None
    text = raw.strip()
    if text == BTN_DISABLE_TEXT:
        return ""
    if text.casefold() in EMPTY_TEXT_MARKERS:
        return ""
    return text


def normalize_delay_seconds(value: object, fallback: int = 0) -> int:
    try:
        delay = int(str(value).strip())
    except (TypeError, ValueError, AttributeError):
        return fallback
    if delay < 0:
        return fallback
    return min(delay, MAX_DELAY_SECONDS)


def preview_message(text: Optional[str], n: int = 44) -> str:
    if text is None:
        return "— не надсилати —"
    prepared = text.replace("\n", "\\n")
    if not prepared.strip():
        return "— не надсилати —"
    return prepared if len(prepared) <= n else prepared[: n - 1] + "…"


# ---------- Доступ ----------
def parse_access_until(value: object) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(str(value), "%Y-%m-%d")
    except ValueError:
        return None


def access_status_line(value: object) -> str:
    """Рендерить '2026-01-01 (12 дн.)' або '— не встановлено —'."""
    dt = parse_access_until(value)
    if dt is None:
        return "— не встановлено —"
    days = (dt - datetime.now()).days
    if days >= 0:
        return f"{dt:%Y-%m-%d} ({days} дн.)"
    return f"закінчився {dt:%Y-%m-%d}"


# ---------- Дефолти за замовчуванням ----------
def default_message_text(data: dict, mode: str) -> Optional[str]:
    """
    Повертає текст за замовчуванням для режиму.
    None -> «не надсилати», "" не повертаємо.
    """
    if mode == "alert":
        if "message_text_alert" in data:
            explicit = normalize_optional_text(data.get("message_text_alert"))
            if explicit is None:
                return DEFAULT_ALERT_TEXT
            return explicit if explicit else None
        legacy = normalize_optional_text(data.get("message_text"))
        return legacy if legacy else DEFAULT_ALERT_TEXT
    if mode == "clear":
        if "message_text_clear" in data:
            explicit = normalize_optional_text(data.get("message_text_clear"))
            if explicit is None:
                return DEFAULT_CLEAR_TEXT
            return explicit if explicit else None
        legacy = normalize_optional_text(data.get("message_text"))
        return legacy if legacy else DEFAULT_CLEAR_TEXT
    return DEFAULT_ALERT_TEXT


def default_delay_seconds(data: dict, mode: str) -> int:
    if mode == "alert":
        return normalize_delay_seconds(data.get("message_delay_alert_seconds"), 0)
    if mode == "clear":
        return normalize_delay_seconds(data.get("message_delay_clear_seconds"), 0)
    return 0
