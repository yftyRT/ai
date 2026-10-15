"""Transport-agnostic messaging interface. The orchestrator and bot logic depend only on this,
so they are testable without Telegram and aiogram is confined to app/bot/telegram_app.py."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, Protocol


class Messenger(Protocol):
    async def send_text(self, chat_id: int, text: str, buttons: Optional[list[tuple[str, str]]] = None) -> Optional[int]:
        """Send a message; returns the message id when Telegram confirmed it, else None."""

    async def edit_text(self, chat_id: int, message_id: int, text: str) -> bool: ...

    async def send_document(self, chat_id: int, path: Path, caption: str = "") -> Optional[int]:
        """Returns the message id ONLY when delivery was confirmed; raises or returns None on failure."""


class RecordingMessenger:
    """In-memory messenger for tests and dry runs."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.edits: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.fail_documents = 0  # number of upcoming send_document calls that fail
        self._id = 100

    async def send_text(self, chat_id, text, buttons=None):
        self._id += 1
        self.sent.append({"chat_id": chat_id, "text": text, "buttons": buttons, "id": self._id})
        return self._id

    async def edit_text(self, chat_id, message_id, text):
        self.edits.append({"chat_id": chat_id, "id": message_id, "text": text})
        return True

    async def send_document(self, chat_id, path, caption=""):
        if self.fail_documents > 0:
            self.fail_documents -= 1
            raise ConnectionError("simulated Telegram upload failure")
        self._id += 1
        self.documents.append({"chat_id": chat_id, "path": Path(path), "caption": caption, "id": self._id})
        return self._id
