"""Реєстрація всіх handler-роутерів."""
from aiogram import Dispatcher

from . import admin, broadcast, common, connect, payment, profile, source_chat


def register(dp: Dispatcher) -> None:
    """Порядок підключення важливий: common іде ПЕРШИМ, щоб /cancel,
    /start і кнопки меню могли перервати будь-який FSM-майстер.
    source_chat іде ОСТАННІМ — ловить групові повідомлення, не заважає решті."""
    dp.include_router(common.router)
    dp.include_router(connect.router)
    dp.include_router(broadcast.router)
    dp.include_router(profile.router)
    dp.include_router(payment.router)
    dp.include_router(admin.router)
    dp.include_router(source_chat.router)
