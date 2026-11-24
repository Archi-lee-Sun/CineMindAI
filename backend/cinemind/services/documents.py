"""Authorized metadata, supplied text chunks and scoped checksum lookup."""

import re
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from cinemind.database import Clock, Database, utcnow
from cinemind.errors import ServiceError, invalid, not_found
from cinemind.models import Document, DocumentChunk
from cinemind.schemas import ChunkCreate, ChunkView, DocumentCreate, DocumentScope, DocumentView, IngestionUpdate, Mode
from cinemind.services.access import conversation, document_visible, pagination


class Documents:
    def __init__(self, db: Database, clock: Clock = utcnow):
        self.db, self.clock = db, clock

    def _create(self, owner: UUID | None, data: DocumentCreate) -> DocumentView:
        with self.db.transaction() as session:
            if data.conversation_id is not None:
                conversation(session, owner, data.conversation_id, lock=True)
            now = self.clock()
            row = Document(owner_user_id=owner, **data.model_dump(), ingestion_status="pending", created_at=now, updated_at=now)
            session.add(row)
            session.flush()
            return DocumentView.model_validate(row)

    def create_private(self, user_id: UUID, data: DocumentCreate) -> DocumentView:
        if data.scope == "global":
            raise invalid("Use the trusted global-document operation.")
        return self._create(user_id, data)

    def create_global(self, data: DocumentCreate) -> DocumentView:
        """Trusted administrative/pipeline operation; never exposed as an API route."""
        if data.scope != "global":
            raise invalid("A global document is required.")
        return self._create(None, data)

    def _writable(self, session: Session, owner: UUID | None, document_id: UUID) -> Document:
        query = select(Document).where(Document.id == document_id)
        query = query.where(Document.scope == "global") if owner is None else query.where(Document.owner_user_id == owner)
        row = session.scalar(query.with_for_update())
        if row is None:
            raise not_found()
        return row

    def read(self, user_id: UUID, document_id: UUID) -> DocumentView:
        with self.db.transaction() as session:
            row = session.scalar(select(Document).where(Document.id == document_id, or_(Document.scope == "global", Document.owner_user_id == user_id)))
            if row is None:
                raise not_found()
            return DocumentView.model_validate(row)

    def list(self, user_id: UUID, *, scope: DocumentScope | None = None, conversation_id: UUID | None = None,
             limit: int = 50, offset: int = 0) -> list[DocumentView]:
        pagination(limit, offset)
        with self.db.transaction() as session:
            query = select(Document).where(or_(Document.scope == "global", Document.owner_user_id == user_id))
            if conversation_id is not None:
                conversation(session, user_id, conversation_id)
                query = query.where(Document.conversation_id == conversation_id)
            if scope is not None:
                query = query.where(Document.scope == scope)
            rows = session.scalars(query.order_by(Document.created_at.desc(), Document.id).limit(limit).offset(offset))
            return [DocumentView.model_validate(row) for row in rows]

    def find_duplicate(self, owner: UUID | None, scope: DocumentScope, checksum: str,
                       conversation_id: UUID | None = None) -> DocumentView | None:
        if not re.fullmatch(r"[0-9a-f]{64}", checksum):
            raise invalid("Invalid SHA-256 checksum.")
        if ((scope == "global") != (owner is None) or (scope == "conversation") != (conversation_id is not None)):
            raise invalid("Invalid document scope.")
        with self.db.transaction() as session:
            if conversation_id is not None:
                conversation(session, owner, conversation_id)
            row = session.scalar(select(Document).where(Document.scope == scope, Document.owner_user_id == owner,
                Document.conversation_id == conversation_id, Document.content_sha256 == checksum)
                .order_by(Document.created_at, Document.id).limit(1))
            return DocumentView.model_validate(row) if row else None

    def set_ingestion_state(self, owner: UUID | None, document_id: UUID, data: IngestionUpdate) -> DocumentView:
        with self.db.transaction() as session:
            row = self._writable(session, owner, document_id)
            row.ingestion_status, row.error_code = data.status, data.error_code
            row.updated_at = self.clock()
            session.flush()
            return DocumentView.model_validate(row)

    def delete(self, owner: UUID | None, document_id: UUID) -> None:
        """Database-only deletion. Future storage integration must coordinate file cleanup."""
        with self.db.transaction() as session:
            session.delete(self._writable(session, owner, document_id))

    def save_chunks(self, owner: UUID | None, document_id: UUID, chunks: list[ChunkCreate]) -> list[ChunkView]:
        """Atomic insert of already extracted text. Does not parse or chunk documents."""
        if not chunks or len(chunks) > 2000 or len({item.chunk_index for item in chunks}) != len(chunks):
            raise invalid("Supply 1–2000 chunks with unique indexes.")
        try:
            with self.db.transaction() as session:
                document = self._writable(session, owner, document_id)
                document.updated_at = self.clock()
                rows = [DocumentChunk(document_id=document_id, **item.model_dump(), created_at=self.clock()) for item in chunks]
                session.add_all(rows)
                session.flush()
                return [ChunkView.model_validate(row) for row in rows]
        except IntegrityError:
            raise ServiceError(409, "chunk_conflict", "Chunk indexes already exist.") from None

    def allowed_chunks(self, user_id: UUID, conversation_id: UUID, document_id: UUID, mode: Mode,
                       *, limit: int = 100, after_index: int = -1) -> list[ChunkView]:
        pagination(limit)
        if after_index < -1 or mode not in {"general", "grounded", "personal_library"}:
            raise invalid()
        with self.db.transaction() as session:
            conversation(session, user_id, conversation_id)
            source = session.get(Document, document_id)
            if source is None or not document_visible(source, user_id, conversation_id, mode):
                raise not_found()
            rows = session.scalars(select(DocumentChunk).where(DocumentChunk.document_id == document_id,
                DocumentChunk.chunk_index > after_index).order_by(DocumentChunk.chunk_index).limit(limit))
            return [ChunkView.model_validate(row) for row in rows]
