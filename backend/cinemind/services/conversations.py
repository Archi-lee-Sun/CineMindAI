"""Owner-scoped conversations and atomic ordered message/citation writes."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from cinemind.database import Clock, Database, utcnow
from cinemind.errors import invalid, not_found
from cinemind.models import Conversation, Document, DocumentChunk, Message, MessageCitation
from cinemind.schemas import (
    CitationInput, CitationView, ConversationCreate, ConversationPatch,
    ConversationView, MessageView, Mode, UserMessageCreate,
)
from cinemind.services.access import conversation, document_visible, pagination


class Conversations:
    def __init__(self, db: Database, clock: Clock = utcnow):
        self.db, self.clock = db, clock

    def create(self, user_id: UUID, data: ConversationCreate) -> ConversationView:
        now = self.clock()
        with self.db.transaction() as session:
            row = Conversation(user_id=user_id, title=data.title, mode=data.mode,
                               title_is_customized="title" in data.model_fields_set,
                               created_at=now, updated_at=now)
            session.add(row)
            session.flush()
            return ConversationView.model_validate(row)

    def list(self, user_id: UUID, limit: int = 50, offset: int = 0) -> list[ConversationView]:
        pagination(limit, offset)
        with self.db.transaction() as session:
            rows = session.scalars(select(Conversation).where(Conversation.user_id == user_id)
                                   .order_by(Conversation.updated_at.desc(), Conversation.id).limit(limit).offset(offset))
            return [ConversationView.model_validate(row) for row in rows]

    def read(self, user_id: UUID, conversation_id: UUID) -> ConversationView:
        with self.db.transaction() as session:
            return ConversationView.model_validate(conversation(session, user_id, conversation_id))

    def patch(self, user_id: UUID, conversation_id: UUID, data: ConversationPatch) -> ConversationView:
        with self.db.transaction() as session:
            row = conversation(session, user_id, conversation_id, lock=True)
            if data.title is not None:
                row.title = data.title
                row.title_is_customized = True
            if data.mode is not None:
                row.mode = data.mode
            row.updated_at = self.clock()
            session.flush()
            return ConversationView.model_validate(row)

    def delete(self, user_id: UUID, conversation_id: UUID) -> None:
        with self.db.transaction() as session:
            session.delete(conversation(session, user_id, conversation_id, lock=True))

    def _append(self, session: Session, row: Conversation, *, role: str, content: str | None,
                response_mode: Mode | None) -> Message:
        # All appenders hold the same conversation row lock, including future AI writes.
        last = session.scalar(select(func.max(Message.sequence_number)).where(Message.conversation_id == row.id)) or 0
        now = self.clock()
        message = Message(conversation_id=row.id, sequence_number=last + 1, role=role,
                          content=content, status="completed" if role == "user" else "pending",
                          response_mode=response_mode, created_at=now)
        session.add(message)
        row.updated_at = now
        session.flush()
        return message

    def append_user(self, user_id: UUID, conversation_id: UUID, data: UserMessageCreate) -> MessageView:
        with self.db.transaction() as session:
            row = conversation(session, user_id, conversation_id, lock=True)
            first_user = not session.scalar(select(Message.id).where(
                Message.conversation_id == row.id, Message.role == "user",
            ).limit(1))
            if first_user and not row.title_is_customized:
                row.title = " ".join(data.content.split())[:200]
            message = self._append(session, row, role="user", content=data.content, response_mode=None)
            return MessageView.model_validate(message)

    def append_assistant(self, user_id: UUID, conversation_id: UUID, response_mode: Mode) -> MessageView:
        """Persist a pending assistant turn; does not generate any response."""
        if response_mode not in {"general", "grounded", "personal_library"}:
            raise invalid("Unknown response mode.")
        with self.db.transaction() as session:
            row = conversation(session, user_id, conversation_id, lock=True)
            return MessageView.model_validate(self._append(session, row, role="assistant", content=None,
                                                           response_mode=response_mode))

    def _owned_message(self, session: Session, user_id: UUID, message_id: UUID) -> tuple[Conversation, Message]:
        conversation_id = session.scalar(select(Message.conversation_id).join(Conversation).where(
            Message.id == message_id, Conversation.user_id == user_id,
        ))
        if conversation_id is None:
            raise not_found()
        row = conversation(session, user_id, conversation_id, lock=True)
        message = session.scalar(select(Message).where(Message.id == message_id).with_for_update())
        if message is None:
            raise not_found()
        return row, message

    def complete_assistant(self, user_id: UUID, message_id: UUID, content: str,
                           citations: list[CitationInput] | None = None) -> MessageView:
        content = UserMessageCreate(content=content).content
        citations = citations or []
        if len(citations) > 100 or len({item.citation_number for item in citations}) != len(citations):
            raise invalid("Citation numbers must be unique; at most 100 are allowed.")
        with self.db.transaction() as session:
            row, message = self._owned_message(session, user_id, message_id)
            if message.role != "assistant" or message.status != "pending":
                raise invalid("Only pending assistant messages can be completed.")
            for item in citations:
                source = session.execute(select(DocumentChunk, Document).join(Document).where(
                    DocumentChunk.id == item.chunk_id,
                ).with_for_update(of=Document, read=True)).one_or_none()
                if source is None or not document_visible(source.Document, user_id, row.id, message.response_mode):
                    raise invalid("Citation source is unavailable for this message.")
                session.add(MessageCitation(message_id=message.id, chunk_id=item.chunk_id,
                                            citation_number=item.citation_number))
            message.content, message.status = content, "completed"
            row.updated_at = self.clock()
            session.flush()
            return self._views(session, user_id, row.id, [message])[0]

    def fail_assistant(self, user_id: UUID, message_id: UUID) -> MessageView:
        with self.db.transaction() as session:
            row, message = self._owned_message(session, user_id, message_id)
            if message.role != "assistant" or message.status != "pending":
                raise invalid("Only pending assistant messages can fail.")
            message.status = "failed"
            row.updated_at = self.clock()
            session.flush()
            return MessageView.model_validate(message)

    def _views(self, session: Session, user_id: UUID, conversation_id: UUID,
               messages: list[Message]) -> list[MessageView]:
        if not messages:
            return []
        views = {message.id: MessageView.model_validate(message) for message in messages}
        mappings = session.execute(select(MessageCitation, DocumentChunk, Document).join(
            DocumentChunk, MessageCitation.chunk_id == DocumentChunk.id,
        ).join(Document).where(MessageCitation.message_id.in_(views)).order_by(MessageCitation.citation_number))
        for mapping, chunk, source in mappings:
            view = views[mapping.message_id]
            if view.role == "assistant" and document_visible(source, user_id, conversation_id, view.response_mode):
                citation = CitationView(citation_number=mapping.citation_number, available=True,
                                        chunk_id=chunk.id, document_id=source.id, title=source.title,
                                        page_start=chunk.page_start, page_end=chunk.page_end)
            else:
                citation = CitationView(citation_number=mapping.citation_number, available=False)
            view.citations.append(citation)
        return list(views.values())

    def messages(self, user_id: UUID, conversation_id: UUID, limit: int = 50, after: int = 0) -> list[MessageView]:
        pagination(limit)
        if after < 0:
            raise invalid("Message cursor must be nonnegative.")
        with self.db.transaction() as session:
            conversation(session, user_id, conversation_id)
            rows = list(session.scalars(select(Message).where(
                Message.conversation_id == conversation_id, Message.sequence_number > after,
            ).order_by(Message.sequence_number).limit(limit)))
            return self._views(session, user_id, conversation_id, rows)

    def recent_history(self, user_id: UUID, conversation_id: UUID, limit: int = 50) -> list[MessageView]:
        pagination(limit)
        with self.db.transaction() as session:
            conversation(session, user_id, conversation_id)
            rows = list(session.scalars(select(Message).where(Message.conversation_id == conversation_id)
                                        .order_by(Message.sequence_number.desc()).limit(limit)))
            return self._views(session, user_id, conversation_id, list(reversed(rows)))

    def resolve_citation(self, user_id: UUID, conversation_id: UUID, message_id: UUID,
                         citation_number: int) -> CitationView:
        with self.db.transaction() as session:
            conversation(session, user_id, conversation_id)
            message = session.scalar(select(Message).where(Message.id == message_id,
                                                           Message.conversation_id == conversation_id))
            if message is None:
                raise not_found()
            view = self._views(session, user_id, conversation_id, [message])[0]
            return next((item for item in view.citations if item.citation_number == citation_number),
                        CitationView(citation_number=citation_number, available=False))
