"""aiogram 3.x wiring. All logic lives in BotService; this file only translates Telegram <-> strings.

NOTE: aiogram is imported lazily-at-module-level here only; the rest of the code base (and the unit
tests) never import it, so the system stays testable without Telegram.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.bot.service import BotService

log = logging.getLogger("factory.bot")
CHUNK = 3900


class AiogramMessenger:
    """Implements the Messenger protocol on top of an aiogram Bot."""

    def __init__(self, bot: Bot):
        self.bot = bot

    async def send_text(self, chat_id: int, text: str, buttons: Optional[list[tuple[str, str]]] = None) -> Optional[int]:
        markup = None
        if buttons:
            markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d) for t, d in buttons]])
        msg = await self.bot.send_message(chat_id, text[:4096], reply_markup=markup)
        return msg.message_id

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> bool:
        try:
            await self.bot.edit_message_text(text[:4096], chat_id=chat_id, message_id=message_id)
            return True
        except Exception as exc:  # noqa: BLE001 - "message is not modified", deleted message, flood control...
            log.debug("edit failed: %s", exc)
            return False

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> Optional[int]:
        msg = await self.bot.send_document(chat_id, FSInputFile(path), caption=caption[:1000])
        return msg.message_id if msg and msg.document else None  # confirmed only if Telegram returned the document


def build_dispatcher(service: BotService) -> Dispatcher:
    router = Router()

    async def reply(message: Message, text: str) -> None:
        for i in range(0, len(text), CHUNK):
            await message.answer(text[i:i + CHUNK])

    @router.message(F.text.startswith("/"))
    async def on_command(message: Message) -> None:
        cmd, _, args = (message.text or "").partition(" ")
        uid = message.from_user.id if message.from_user else None
        await reply(message, await service.handle_command(uid, message.chat.id, cmd, args))

    @router.message(F.text)
    async def on_text(message: Message) -> None:
        uid = message.from_user.id if message.from_user else None
        await reply(message, await service.handle_text(uid, message.chat.id, message.text or ""))

    @router.callback_query(F.data.regexp(r"^(ok|no):"))
    async def on_callback(cb: CallbackQuery) -> None:
        uid = cb.from_user.id if cb.from_user else None
        text = await service.handle_callback(uid, cb.data or "")
        await cb.answer(text[:190])
        if cb.message:
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:  # noqa: BLE001
                pass

    dp = Dispatcher()
    dp.include_router(router)
    return dp


def create_bot(token: str) -> Bot:
    return Bot(token=token, default=DefaultBotProperties(parse_mode=None))
