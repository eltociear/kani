import os

from pydantic import BaseModel

from ..models import ChatMessage

PathLike = str | bytes | os.PathLike


class SavedKani(BaseModel):
    version: int = 1
    always_included_messages: list[ChatMessage]
    chat_history: list[ChatMessage]
