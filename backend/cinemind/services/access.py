"""Shared owner-scoped lookups and bounded pagination."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from cinemind.errors import invalid, not_found
from cinemind.models import Conversation, Document
from cinemind.schemas import Mode


def pagination(limit: int, offset: int = 0) -> None:
    if not 1 <= limit <= 100 or not 0 <= offset <= 100000:
        raise invalid("Pagination is out of range.")


def conversation(session: Session, user_id: UUID, conversation_id: UUID, *, lock: bool = False) -> Conversation:
    query = select(Conversation).where(Conversation.id == conversation_id, Conversation.user_id == user_id)
    if lock:
        query = query.with_for_update()
    row = session.scalar(query)
    if row is None:
        raise not_found()
    return row


def document_visible(document: Document, user_id: UUID, conversation_id: UUID, mode: Mode) -> bool:
    if document.ingestion_status != "ready":
        return False
    if mode == "personal_library":
        return document.owner_user_id == user_id and (
            document.scope == "personal" or
            (document.scope == "conversation" and document.conversation_id == conversation_id)
        )
    return document.scope == "global"
