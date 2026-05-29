"""Реферальна програма: запросити друзів, отримати бонусні дні за їх оплати."""
from __future__ import annotations

import logging
from urllib.parse import quote
from typing import Optional

from aiogram import F, Router, types
from aiogram.filters import Command
from aiogram.types import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from ...config import (
    BTN_REFERRAL,
    DIV,
    EMO,
    REFERRAL_BONUS_DAYS,
    REFERRAL_PAYLOAD_PREFIX,
)
from ...storage import count_referrals_for
from ..keyboards import main_menu_kb

log = logging.getLogger(__name__)
router = Router(name="referral")

# Кеш bot.username — запитуємо один раз на старті, далі віддаємо з пам'яті
_BOT_USERNAME: Optional[str] = None


async def _get_bot_username(bot) -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME is None:
        try:
            me = await bot.get_me()
            _BOT_USERNAME = me.username or ""
        except Exception as exc:
            log.warning("referral: не вдалося отримати username бота: %s", exc)
            _BOT_USERNAME = ""
    return _BOT_USERNAME


def _ref_link(bot_username: str, user_id: int) -> str:
    return f"https://t.me/{bot_username}?start={REFERRAL_PAYLOAD_PREFIX}{user_id}"


@router.message(Command("referral", "invite"))
@router.message(F.text == BTN_REFERRAL)
async def show_referral(msg: types.Message) -> None:
    user = msg.from_user
    bot_username = await _get_bot_username(msg.bot)

    total, paid = count_referrals_for(user.id)
    earned_days = paid * REFERRAL_BONUS_DAYS

    if not bot_username:
        # Аварійний випадок — get_me() не спрацював
        await msg.answer(
            f"🎁  <b>Запросити друзів</b>\n{DIV}\n\n"
            f"<i>На жаль, посилання тимчасово недоступне. "
            f"Спробуйте за хвилину або зверніться у «🆘 Підтримка».</i>",
            reply_markup=main_menu_kb(user),
        )
        return

    link = _ref_link(bot_username, user.id)

    text = (
        f"🎁  <b>Запросіть друзів — отримуйте бонусні дні</b>\n"
        f"{DIV}\n\n"
        f"<b>Як це працює:</b>\n"
        f"  ①  Поділіться <b>своїм посиланням</b> з другом\n"
        f"  ②  Друг переходить, налаштовує бота\n"
        f"  ③  Коли він <b>купує будь-який тариф</b>, "
        f"ви автоматично отримуєте <b>+{REFERRAL_BONUS_DAYS} днів</b> доступу\n\n"
        f"<b>Ваше посилання:</b>\n"
        f"<code>{link}</code>\n\n"
        f"📊  <b>Ваша статистика:</b>\n"
        f"  • Запрошено друзів:  <b>{total}</b>\n"
        f"  • Із них купили тариф:  <b>{paid}</b>\n"
        f"  • Зароблено днів:  <b>{earned_days}</b>\n\n"
        f"<i>Бонус нараховується <u>один раз</u> за кожного друга — "
        f"при його першій оплаті. Самозапрошення не зараховується.</i>"
    )

    # Кнопки: копіювання посилання + готовий share-чат
    share_text = (
        "Привіт! Глянь корисний бот для авто-сповіщень "
        "про повітряну тривогу в Києві:"
    )
    share_url = (
        f"https://t.me/share/url"
        f"?url={quote(link, safe='')}"
        f"&text={quote(share_text, safe='')}"
    )

    rows = [
        [
            InlineKeyboardButton(
                text="📋  Копіювати посилання",
                copy_text=CopyTextButton(text=link),
            ),
        ],
        [
            InlineKeyboardButton(
                text="📤  Поділитись з другом",
                url=share_url,
            ),
        ],
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    await msg.answer(text, reply_markup=kb, disable_web_page_preview=True)
