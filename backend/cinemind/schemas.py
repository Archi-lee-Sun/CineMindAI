"""Typed public contracts and trusted internal persistence inputs."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator, model_validator

Mode = Literal["general", "grounded", "personal_library"]
DocumentScope = Literal["global", "personal", "conversation"]
IngestionStatus = Literal["pending", "processing", "ready", "failed"]
Nonblank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100000)]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True, hide_input_in_errors=True)


class ErrorDetail(Contract):
    code: str
    message: str


class ErrorResponse(Contract):
    error: ErrorDetail


class Acknowledged(Contract):
    message: str


class EmailRequest(Contract):
    email: EmailStr = Field(max_length=254)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, email: str) -> str:
        # Case-insensitive account identity; no provider alias rewriting.
        return email.lower()


class Signup(EmailRequest):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
    password: str = Field(min_length=12, max_length=128, repr=False)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, username: object) -> object:
        return username.strip().lower() if isinstance(username, str) else username


class Login(EmailRequest):
    password: str = Field(min_length=1, max_length=128, repr=False)


class VerifyEmail(EmailRequest):
    code: str = Field(pattern=r"^[0-9]{6}$", repr=False)


class Profile(Contract):
    id: UUID
    username: str
    email: str
    email_verified_at: datetime | None
    created_at: datetime
    updated_at: datetime


class SessionView(Contract):
    id: UUID
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    is_current: bool


class ConversationCreate(Contract):
    title: Title = "New chat"
    mode: Mode = "general"


class ConversationPatch(Contract):
    title: Title | None = None
    mode: Mode | None = None

    @model_validator(mode="after")
    def reject_null_updates(self) -> "ConversationPatch":
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("Updates must have non-null values")
        return self


class ConversationView(Contract):
    id: UUID
    title: str
    mode: Mode
    created_at: datetime
    updated_at: datetime


class UserMessageCreate(Contract):
    content: Nonblank


class CitationInput(Contract):
    chunk_id: UUID
    citation_number: int = Field(gt=0)


class CitationView(Contract):
    citation_number: int
    available: bool
    chunk_id: UUID | None = None
    document_id: UUID | None = None
    title: str | None = None
    page_start: int | None = None
    page_end: int | None = None


class MessageView(Contract):
    id: UUID
    conversation_id: UUID
    sequence_number: int
    role: Literal["user", "assistant"]
    content: str | None
    status: Literal["pending", "completed", "failed"]
    response_mode: Mode | None
    created_at: datetime
    citations: list[CitationView] = Field(default_factory=list)


class MemoryCreate(Contract):
    conversation_id: UUID | None = None
    kind: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    content: Nonblank
    source_message_id: UUID | None = None


class MemoryPatch(Contract):
    content: Nonblank


class MemoryView(Contract):
    id: UUID
    conversation_id: UUID | None
    kind: str
    content: str
    source_message_id: UUID | None
    status: Literal["active", "superseded"]
    created_at: datetime
    updated_at: datetime


class DocumentCreate(Contract):
    scope: DocumentScope
    conversation_id: UUID | None = None
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    original_filename: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    media_type: str = Field(min_length=1, max_length=200)
    storage_key: str = Field(min_length=1, max_length=2000, repr=False)
    file_size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    author: str | None = Field(default=None, max_length=500)
    source_url: str | None = Field(default=None, max_length=2000)
    license: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def check_scope(self) -> "DocumentCreate":
        if (self.scope == "conversation") != (self.conversation_id is not None):
            raise ValueError("Only conversation documents require conversation_id")
        return self


class DocumentView(Contract):
    id: UUID
    scope: DocumentScope
    conversation_id: UUID | None
    title: str
    original_filename: str
    media_type: str
    file_size_bytes: int
    content_sha256: str
    ingestion_status: IngestionStatus
    error_code: str | None
    author: str | None
    source_url: str | None
    license: str | None
    created_at: datetime
    updated_at: datetime


class IngestionUpdate(Contract):
    status: IngestionStatus
    error_code: str | None = Field(default=None, pattern=r"^[a-z0-9_]{1,80}$")


class ChunkCreate(Contract):
    chunk_index: int = Field(ge=0)
    content: Nonblank
    page_start: int | None = Field(default=None, gt=0)
    page_end: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def check_pages(self) -> "ChunkCreate":
        if (self.page_start is None) != (self.page_end is None):
            raise ValueError("Both page bounds are required or both must be null")
        if self.page_start is not None and self.page_end < self.page_start:
            raise ValueError("Invalid page range")
        return self


class ChunkView(ChunkCreate):
    id: UUID
    document_id: UUID
    created_at: datetime
