"""Stored text persistence only; no extraction, ranking or retrieval algorithm."""

from uuid import UUID

from sqlalchemy import or_, select

from cinemind.database import Clock, Database, utcnow
from cinemind.errors import not_found
from cinemind.models import Conversation, Memory, Message
from cinemind.schemas import MemoryCreate, MemoryPatch, MemoryView
from cinemind.services.access import conversation, pagination


class Memories:
    def __init__(self, db: Database, clock: Clock = utcnow):
        self.db, self.clock = db, clock

    def create(self, user_id: UUID, data: MemoryCreate) -> MemoryView:
        """Trusted internal create with owner and source provenance validation."""
        with self.db.transaction() as session:
            if data.conversation_id is not None:
                conversation(session, user_id, data.conversation_id, lock=True)
            if data.source_message_id is not None:
                query = select(Message).join(Conversation).where(Message.id == data.source_message_id,
                                                                  Conversation.user_id == user_id)
                if data.conversation_id is not None:
                    query = query.where(Message.conversation_id == data.conversation_id)
                if session.scalar(query.with_for_update(of=Message, read=True)) is None:
                    raise not_found()
            now = self.clock()
            row = Memory(user_id=user_id, **data.model_dump(), status="active", created_at=now, updated_at=now)
            session.add(row)
            session.flush()
            return MemoryView.model_validate(row)

    def list(self, user_id: UUID, conversation_id: UUID | None = None, *, include_account_wide: bool = True,
             include_superseded: bool = False, limit: int = 50, offset: int = 0) -> list[MemoryView]:
        pagination(limit, offset)
        with self.db.transaction() as session:
            query = select(Memory).where(Memory.user_id == user_id)
            if conversation_id is None:
                query = query.where(Memory.conversation_id.is_(None))
            else:
                conversation(session, user_id, conversation_id)
                scope = Memory.conversation_id == conversation_id
                if include_account_wide:
                    scope = or_(scope, Memory.conversation_id.is_(None))
                query = query.where(scope)
            if not include_superseded:
                query = query.where(Memory.status == "active")
            rows = session.scalars(query.order_by(Memory.created_at, Memory.id).limit(limit).offset(offset))
            return [MemoryView.model_validate(row) for row in rows]

    def edit(self, user_id: UUID, memory_id: UUID, data: MemoryPatch) -> MemoryView:
        with self.db.transaction() as session:
            row = session.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id).with_for_update())
            if row is None:
                raise not_found()
            row.content, row.updated_at = data.content, self.clock()
            session.flush()
            return MemoryView.model_validate(row)

    def supersede(self, user_id: UUID, memory_id: UUID) -> MemoryView:
        with self.db.transaction() as session:
            row = session.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id).with_for_update())
            if row is None:
                raise not_found()
            row.status, row.updated_at = "superseded", self.clock()
            session.flush()
            return MemoryView.model_validate(row)

    def delete(self, user_id: UUID, memory_id: UUID) -> None:
        with self.db.transaction() as session:
            row = session.scalar(select(Memory).where(Memory.id == memory_id, Memory.user_id == user_id).with_for_update())
            if row is None:
                raise not_found()
            session.delete(row)
