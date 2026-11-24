from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, insert, select, text
from sqlalchemy.exc import IntegrityError

from cinemind.models import Base, Conversation, Document, DocumentChunk, EmailVerification, Memory, Message, MessageCitation, User
from cinemind.schemas import ConversationCreate


def test_exact_nine_tables_and_model_migration_parity(db):
    with db.engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
        assert len(Base.metadata.tables) == 9
        assert compare_metadata(MigrationContext.configure(connection, opts={"compare_type": True, "compare_server_default": True}), Base.metadata) == []
        timestamp_types = connection.execute(text("SELECT DISTINCT data_type FROM information_schema.columns WHERE table_schema = 'public' AND column_name IN ('created_at', 'updated_at', 'expires_at', 'used_at', 'invalidated_at', 'revoked_at', 'email_verified_at')")).scalars().all()
        assert timestamp_types == ["timestamp with time zone"]


@pytest.mark.parametrize("field", ["email", "username"])
def test_postgresql_case_insensitive_uniqueness(make_user, db, field):
    make_user()
    values = dict(username="another", email="another@example.com", password_hash="internal-test-value")
    values[field] = "ALICE@example.com" if field == "email" else "ALICE"
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(User(**values))


@pytest.mark.parametrize("table", ["memory", "document"])
def test_composite_fk_rejects_other_conversation_owner(make_user, app, db, table):
    _, user_id, _ = make_user()
    _, other_id, _ = make_user("bob")
    row = app.state.conversations.create(user_id, ConversationCreate())
    with pytest.raises(IntegrityError), db.transaction() as session:
        if table == "memory":
            session.add(Memory(user_id=other_id, conversation_id=row.id, kind="note", content="x"))
        else:
            session.add(Document(scope="conversation", owner_user_id=other_id, conversation_id=row.id,
                title="x", original_filename="x", media_type="text/plain", storage_key="internal", file_size_bytes=1, content_sha256="a" * 64))


@pytest.mark.parametrize("change", [
    {"scope": "global", "owner_user_id": "owner"},
    {"scope": "personal", "owner_user_id": None},
    {"file_size_bytes": -1}, {"content_sha256": "invalid"}, {"scope": "unknown"},
])
def test_document_checks(make_user, db, change):
    _, owner, _ = make_user()
    values = dict(scope="personal", owner_user_id=owner, title="x", original_filename="x", media_type="text/plain",
                  storage_key="internal", file_size_bytes=1, content_sha256="a" * 64)
    values.update({key: owner if value == "owner" else value for key, value in change.items()})
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(Document(**values))


@pytest.mark.parametrize("change", [
    {"sequence_number": 0}, {"role": "system"}, {"content": None}, {"content": "   "}, {"content": "\t\n"},
    {"status": "pending"}, {"response_mode": "grounded"},
    {"role": "assistant", "response_mode": None},
])
def test_message_policy_checks(make_user, app, db, change):
    _, owner, _ = make_user()
    row = app.state.conversations.create(owner, ConversationCreate())
    values = dict(conversation_id=row.id, sequence_number=1, role="user", status="completed", content="x", response_mode=None)
    values.update(change)
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(Message(**values))


def test_outstanding_verification_is_unique_and_attempts_nonnegative(make_user, db):
    _, user_id, _ = make_user()
    with db.transaction() as session:
        template = session.scalar(select(EmailVerification))
        expiry = template.expires_at
    values = dict(user_id=user_id, email="alice@example.com", code_digest="internal-test-digest", expires_at=expiry)
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(EmailVerification(**values, failed_attempts=-1))
    with db.transaction() as session:
        session.add(EmailVerification(**values))
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(EmailVerification(**values))


@pytest.mark.parametrize("bounds", [(None, 1), (1, None), (0, 1), (3, 2)])
def test_chunk_page_constraints(make_user, app, db, bounds):
    from test_persistence import document_input
    _, owner, _ = make_user()
    document = app.state.documents.create_private(owner, document_input())
    with pytest.raises(IntegrityError), db.transaction() as session:
        session.add(DocumentChunk(document_id=document.id, chunk_index=0, content="x", page_start=bounds[0], page_end=bounds[1]))


def test_ordered_child_uniqueness_and_citation_number_checks(make_user, app, db):
    from test_persistence import source
    from cinemind.schemas import CitationInput, UserMessageCreate
    _, owner, _ = make_user()
    row = app.state.conversations.create(owner, ConversationCreate())
    user = app.state.conversations.append_user(owner, row.id, UserMessageCreate(content="Supplied text"))
    document, chunk = source(app)
    assistant = app.state.conversations.append_assistant(owner, row.id, "general")
    app.state.conversations.complete_assistant(owner, assistant.id, "Supplied answer", [CitationInput(chunk_id=chunk.id, citation_number=1)])
    invalid_rows = [
        Message(conversation_id=row.id, sequence_number=user.sequence_number, role="user", status="completed", content="duplicate"),
        DocumentChunk(document_id=document.id, chunk_index=0, content="duplicate"),
        MessageCitation(message_id=assistant.id, chunk_id=chunk.id, citation_number=1),
        MessageCitation(message_id=assistant.id, chunk_id=chunk.id, citation_number=0),
    ]
    for invalid_row in invalid_rows:
        with pytest.raises(IntegrityError), db.transaction() as session:
            session.add(invalid_row)
