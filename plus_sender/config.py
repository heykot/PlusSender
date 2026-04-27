"""Конфігурація: змінні оточення, шляхи, тексти, стиль."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ---------- Шляхи (зберігаємо сумісність зі старою структурою) ----------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SESSIONS_DIR = PROJECT_ROOT / "sessions"
USERS_DIR = PROJECT_ROOT / "user_data"
MEDIA_DIR = PROJECT_ROOT / "user_data" / "media"
ADMINS_FILE = PROJECT_ROOT / "admins.json"


def ensure_runtime_dirs() -> None:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)


def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


@dataclass(frozen=True)
class Settings:
    bot_token: str
    alarm_api_key: str
    alarm_region_id: str
    alarm_poll_interval: int
    telegram_proxy: str   # "" = без проксі; "socks5://…" або "http://…"
    mono_token: str       # Personal token з api.monobank.ua (опціонально)
    mono_jar_id: str      # ID банки-скарбнички (опціонально)
    mono_webhook_port: int  # Порт для aiohttp сервера (default 8080)

    @classmethod
    def load(cls) -> "Settings":
        token = _env("BOT_TOKEN")
        if not token:
            raise RuntimeError(
                "BOT_TOKEN не задано. Додай його у .env або змінні оточення."
            )

        alarm_key = _env("ALARM_API_KEY")
        if not alarm_key:
            raise RuntimeError(
                "ALARM_API_KEY не задано. Отримай ключ на https://api.ukrainealarm.com/"
            )

        try:
            interval = int(_env("ALARM_POLL_INTERVAL", "10"))
        except ValueError:
            interval = 10

        try:
            mono_port = int(_env("MONO_WEBHOOK_PORT", "8080"))
        except ValueError:
            mono_port = 8080

        return cls(
            bot_token=token,
            alarm_api_key=alarm_key,
            alarm_region_id=_env("ALARM_REGION_ID", "31"),  # 31 = Київ
            alarm_poll_interval=max(5, interval),
            telegram_proxy=_env("TELEGRAM_PROXY", ""),
            mono_token=_env("MONO_TOKEN", ""),
            mono_jar_id=_env("MONO_JAR_ID", ""),
            mono_webhook_port=mono_port,
        )


# ============================================================================
#                              СТИЛЬ / БРЕНДИНГ
# ============================================================================

# Назва бренду для шапок
BRAND = "Plus Sender"
TAGLINE = "Радар повітряної тривоги"

# ---------- Розділювачі ----------
DIV = "━━━━━━━━━━━━━━━━━━━━━━━━━━"   # головний розділювач секцій
DIV_THIN = "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄"  # тонкий розділювач у списках
DIV_DOTS = "··············"             # дрібний роздільник (пункти меню)
BULLET = "▎"                            # маркер секції
SEP = "·"                               # маленький роздільник
ARROW = "›"                             # вказівка/розгортання


# Хедер для верху повідомлень. Використовуй з h() якщо вкладаєш user-input.
def make_header(title: str, emoji: str = "🤖") -> str:
    """Формує заголовок повідомлення:

    🤖  <b>Plus Sender</b>
    ━━━━━━━━━━━━━━━━━━━━━━━━━━
    <i>title</i>
    """
    return (
        f"{emoji}  <b>{BRAND}</b>\n"
        f"{DIV}\n"
        f"<i>{title}</i>"
    )


# ===================== EMOJI =====================
EMO = {
    # status / state
    "ok": "✅", "info": "ℹ️", "err": "❌", "warn": "⚠️",
    "active": "🟢", "inactive": "🔴", "pending": "🟡",

    # actions
    "bolt": "⚡", "play": "▶️", "stop": "⏹️", "back": "↩️",
    "edit": "✏️", "trash": "🗑", "save": "💾", "refresh": "🔄",

    # core entities
    "alert": "🚨", "clear": "🟢", "chat": "💬", "magn": "🔎",
    "pin": "📌", "gear": "⚙️", "target": "🎯", "key": "🔑",
    "phone": "📱", "code": "🔢", "lock": "🔐", "calendar": "📅",
    "card": "💳", "user": "👤", "shield": "🛡", "rocket": "🚀",
    "star": "✨", "timer": "⏱", "list": "🗂", "tune": "🎚",
    "dot_full": "●", "dot_empty": "○",
}

# Типові badge-токени
BADGE_ACTIVE = f"{EMO['active']} <b>Активно</b>"
BADGE_INACTIVE = f"{EMO['inactive']} <b>Не активно</b>"


# ===================== КНОПКИ ГОЛОВНОГО МЕНЮ =====================
# Уніфікований стиль: іконка + слово; кнопки розташовані компактно по 2 в ряд.
BTN_CONNECT = "🔌 Підключити"
BTN_PROFILE = "👤 Профіль"
BTN_BROADCAST = "🎛 Налаштування"
BTN_START = "▶️ Старт"
BTN_STOP = "⏹ Стоп"
BTN_PAYMENT = "💳 Оплата"
BTN_HELP = "ℹ️ Довідка"

# Ця кнопка змінюється динамічно в keyboards.main_menu_kb (статус)
BTN_STATUS_PREFIX = "📊 Стан:"

# Скасування / disable надсилання
BTN_CANCEL = "↩️ Скасувати"
BTN_DISABLE_TEXT = "🚫 Не надсилати"

CANCEL_TEXTS = {BTN_CANCEL, "Скасувати", "/cancel"}
EMPTY_TEXT_MARKERS = {"-", "—", "пусто", "не надсилати", "skip", "none", "off"}

# Обмеження
MAX_DELAY_SECONDS = 86400  # 24 години
DEFAULT_ALERT_TEXT = "+"
DEFAULT_CLEAR_TEXT = "✅ Відбій"


# ===================== СТАРИЙ HR (для зворотньої сумісності) =====================
HR = DIV
TITLE = f"🤖 <b>{BRAND}</b>"
