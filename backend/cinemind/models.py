"""Nine persistence tables. Cross-table service rules live beside their writes."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
    Index, Integer, MetaData, String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class Identity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class Created:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Updated(Created):
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class User(Identity, Updated, Base):
    __tablename__ = "users"
    username: Mapped[str] = mapped_column(String(50))
    email: Mapped[str] = mapped_column(String(254))
    password_hash: Mapped[str] = mapped_column(Text)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("uq_users_username_lower", func.lower(username), unique=True),
        Index("uq_users_email_lower", func.lower(email), unique=True),
    )


class EmailVerification(Identity, Created, Base):
    __tablename__ = "email_verifications"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(254))
    code_digest: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_attempts: Mapped[int] = mapped_column(Integer, server_default=text("0"))
    __table_args__ = (
        CheckConstraint("failed_attempts >= 0", name="attempts_nonnegative"),
        Index("uq_email_verifications_outstanding", "user_id", unique=True,
              postgresql_where=text("used_at IS NULL AND invalidated_at IS NULL")),
        Index("ix_email_verifications_user_created", "user_id", "created_at", "id"),
    )


class LoginSession(Identity, Created, Base):
    __tablename__ = "sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_sessions_user_created", "user_id", "created_at", "id"),)


class Conversation(Identity, Updated, Base):
    __tablename__ = "conversations"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200), server_default=text("'New chat'"))
    title_is_customized: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    mode: Mapped[str] = mapped_column(Text, server_default=text("'general'"))
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_conversations_id_user"),
        CheckConstraint("mode IN ('general', 'grounded', 'personal_library')", name="mode"),
        Index("ix_conversations_user_updated", "user_id", "updated_at", "id"),
    )


class Message(Identity, Created, Base):
    __tablename__ = "messages"
    conversation_id: Mapped[UUID] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    sequence_number: Mapped[int] = mapped_column(BigInteger)
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    response_mode: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        UniqueConstraint("conversation_id", "sequence_number", name="uq_messages_conversation_sequence"),
        CheckConstraint("sequence_number > 0", name="sequence_positive"),
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        CheckConstraint("status IN ('pending', 'completed', 'failed')", name="status"),
        CheckConstraint("response_mode IS NULL OR response_mode IN ('general', 'grounded', 'personal_library')", name="response_mode"),
        CheckConstraint("role != 'user' OR (status = 'completed' AND response_mode IS NULL AND content IS NOT NULL AND content ~ '[^[:space:]]')", name="user_message"),
        CheckConstraint("role != 'assistant' OR response_mode IS NOT NULL", name="assistant_mode"),
        CheckConstraint("status != 'completed' OR (content IS NOT NULL AND content ~ '[^[:space:]]')", name="completed_content"),
    )


class Memory(Identity, Updated, Base):
    __tablename__ = "memories"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conversation_id: Mapped[UUID | None]
    kind: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    source_message_id: Mapped[UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    status: Mapped[str] = mapped_column(Text, server_default=text("'active'"))
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id", "user_id"], ["conversations.id", "conversations.user_id"], ondelete="CASCADE", name="fk_memories_conversation_owner"),
        CheckConstraint("status IN ('active', 'superseded')", name="status"),
        Index("ix_memories_user_status_created", "user_id", "status", "created_at", "id"),
        Index("ix_memories_conversation_owner", "conversation_id", "user_id"),
        Index("ix_memories_source_message", "source_message_id"),
    )


class Document(Identity, Updated, Base):
    __tablename__ = "documents"
    scope: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    conversation_id: Mapped[UUID | None]
    title: Mapped[str] = mapped_column(Text)
    original_filename: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(Text)
    ingestion_status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    error_code: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)
    license: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (
        ForeignKeyConstraint(["conversation_id", "owner_user_id"], ["conversations.id", "conversations.user_id"], ondelete="CASCADE", name="fk_documents_conversation_owner"),
        CheckConstraint("(scope = 'global' AND owner_user_id IS NULL AND conversation_id IS NULL) OR (scope = 'personal' AND owner_user_id IS NOT NULL AND conversation_id IS NULL) OR (scope = 'conversation' AND owner_user_id IS NOT NULL AND conversation_id IS NOT NULL)", name="scope_owner"),
        CheckConstraint("file_size_bytes >= 0", name="size_nonnegative"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="sha256"),
        CheckConstraint("ingestion_status IN ('pending', 'processing', 'ready', 'failed')", name="ingestion_status"),
        Index("ix_documents_owner_scope_created", "owner_user_id", "scope", "created_at", "id"),
        Index("ix_documents_conversation_owner", "conversation_id", "owner_user_id"),
        Index("ix_documents_scope_checksum", "scope", "owner_user_id", "conversation_id", "content_sha256"),
    )


class DocumentChunk(Identity, Created, Base):
    __tablename__ = "document_chunks"
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"))
    chunk_index: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("document_id", "chunk_index", name="uq_document_chunks_document_index"),
        CheckConstraint("chunk_index >= 0", name="index_nonnegative"),
        CheckConstraint("(page_start IS NULL AND page_end IS NULL) OR (page_start IS NOT NULL AND page_end IS NOT NULL AND page_start > 0 AND page_end >= page_start)", name="page_bounds"),
    )


class MessageCitation(Identity, Base):
    __tablename__ = "message_citations"
    message_id: Mapped[UUID] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"))
    chunk_id: Mapped[UUID] = mapped_column(ForeignKey("document_chunks.id", ondelete="CASCADE"))
    citation_number: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("message_id", "citation_number", name="uq_message_citations_message_number"),
        CheckConstraint("citation_number > 0", name="number_positive"),
        Index("ix_message_citations_chunk", "chunk_id"),
    )
