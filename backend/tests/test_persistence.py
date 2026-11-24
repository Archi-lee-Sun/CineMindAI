from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from cinemind.errors import ServiceError
from cinemind.models import Document, Memory, Message, MessageCitation, User
from cinemind.schemas import (
    ChunkCreate, CitationInput, ConversationCreate, DocumentCreate, IngestionUpdate,
    MemoryCreate, MemoryPatch, UserMessageCreate,
)


def document_input(scope="personal", conversation_id=None, checksum="a" * 64):
    return DocumentCreate(scope=scope, conversation_id=conversation_id, title="Supplied source title",
        original_filename="source.txt", media_type="text/plain", storage_key="private/reference-only",
        file_size_bytes=20, content_sha256=checksum)


def source(app, owner=None, scope="global", conversation_id=None):
    service = app.state.documents
    data = document_input(scope, conversation_id)
    document = service.create_global(data) if owner is None else service.create_private(owner, data)
    chunks = service.save_chunks(owner, document.id, [ChunkCreate(chunk_index=0, content="Supplied source text", page_start=2, page_end=3)])
    service.set_ingestion_state(owner, document.id, IngestionUpdate(status="ready"))
    return document, chunks[0]


def test_conversation_crud_messages_and_owner_isolation(make_user):
    alice, _, _ = make_user()
    bob, _, _ = make_user("bob")
    response = alice.post("/conversations", json={})
    assert response.status_code == 201
    conversation_id = response.json()["id"]
    assert response.json()["mode"] == "general"
    assert bob.get(f"/conversations/{conversation_id}").status_code == 404
    assert bob.patch(f"/conversations/{conversation_id}", json={"title": "stolen"}).status_code == 404
    assert bob.delete(f"/conversations/{conversation_id}").status_code == 404
    assert bob.get(f"/conversations/{conversation_id}/messages").status_code == 404
    assert bob.post(f"/conversations/{conversation_id}/messages", json={"content": "stolen"}).status_code == 404
    assert bob.get("/conversations").json() == []
    message = alice.post(f"/conversations/{conversation_id}/messages", json={"content": "My first message"})
    assert message.status_code == 201
    assert message.json()["role"] == "user" and message.json()["status"] == "completed"
    assert message.json()["response_mode"] is None and message.json()["sequence_number"] == 1
    assert alice.get(f"/conversations/{conversation_id}").json()["title"] == "My first message"
    assert alice.patch(f"/conversations/{conversation_id}", json={"title": "Renamed", "mode": "personal_library"}).status_code == 200
    assert alice.get(f"/conversations/{conversation_id}/messages").json()[0]["id"] == message.json()["id"]
    assert alice.get("/conversations?limit=101").status_code == 422
    assert alice.delete(f"/conversations/{conversation_id}").status_code == 204
    assert alice.get(f"/conversations/{conversation_id}").status_code == 404


def test_renamed_default_title_is_preserved_and_role_is_server_policy(make_user):
    client, user_id, _ = make_user()
    conversation_id = client.post("/conversations", json={}).json()["id"]
    client.patch(f"/conversations/{conversation_id}", json={"title": "New chat"})
    assert client.post(f"/conversations/{conversation_id}/messages", json={"content": "Keep custom title"}).status_code == 201
    assert client.get(f"/conversations/{conversation_id}").json()["title"] == "New chat"
    assert client.post(f"/conversations/{conversation_id}/messages", json={"content": "bad", "role": "assistant"}).status_code == 422
    assert client.post("/conversations", json={"user_id": str(user_id)}).status_code == 422


def test_concurrent_messages_have_stable_sequences(make_user, app, clock):
    _, user_id, _ = make_user()
    row = app.state.conversations.create(user_id, ConversationCreate())
    clock.advance(1)
    def append(index):
        return app.state.conversations.append_user(user_id, row.id, UserMessageCreate(content=f"Message {index}")).sequence_number
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(pool.map(append, range(20)))
    assert sorted(sequences) == list(range(1, 21))
    history = app.state.conversations.messages(user_id, row.id, limit=100)
    assert [item.sequence_number for item in history] == list(range(1, 21))
    assert app.state.conversations.read(user_id, row.id).updated_at > row.updated_at
    assert [item.sequence_number for item in app.state.conversations.recent_history(user_id, row.id, 3)] == [18, 19, 20]


def test_memory_provenance_scope_and_mutations(make_user, app):
    alice, user_id, _ = make_user()
    bob, other_id, _ = make_user("bob")
    conversations = app.state.conversations
    first = conversations.create(user_id, ConversationCreate())
    second = conversations.create(user_id, ConversationCreate())
    foreign = conversations.create(other_id, ConversationCreate())
    message = conversations.append_user(user_id, first.id, UserMessageCreate(content="User-supplied text"))
    foreign_message = conversations.append_user(other_id, foreign.id, UserMessageCreate(content="Private foreign text"))
    memories = app.state.memories
    account = memories.create(user_id, MemoryCreate(kind="preference", content="Supplied account text", source_message_id=message.id))
    scoped = memories.create(user_id, MemoryCreate(kind="note", content="Supplied scoped text", conversation_id=first.id, source_message_id=message.id))
    for data in (
        MemoryCreate(kind="note", content="x", conversation_id=foreign.id),
        MemoryCreate(kind="note", content="x", source_message_id=foreign_message.id),
        MemoryCreate(kind="note", content="x", conversation_id=second.id, source_message_id=message.id),
    ):
        with pytest.raises(ServiceError) as caught:
            memories.create(user_id, data)
        assert caught.value.status == 404
    assert {item.id for item in memories.list(user_id, first.id)} == {account.id, scoped.id}
    assert {item.id for item in memories.list(user_id, second.id)} == {account.id}
    assert bob.get(f"/memories?conversation_id={first.id}").status_code == 404
    assert bob.patch(f"/memories/{account.id}", json={"content": "stolen"}).status_code == 404
    assert bob.delete(f"/memories/{account.id}").status_code == 404
    assert alice.patch(f"/memories/{account.id}", json={"content": "Edited text"}).status_code == 200
    memories.supersede(user_id, account.id)
    assert memories.list(user_id) == []
    assert len(memories.list(user_id, include_superseded=True)) == 1
    assert alice.delete(f"/memories/{account.id}").status_code == 204


def test_document_scope_duplicates_and_private_metadata(make_user, app):
    alice, user_id, _ = make_user()
    bob, other_id, _ = make_user("bob")
    conversation = app.state.conversations.create(user_id, ConversationCreate())
    private, _ = source(app, user_id, "personal")
    scoped, _ = source(app, user_id, "conversation", conversation.id)
    global_document, _ = source(app)
    rows = alice.get("/documents").json()
    assert {row["id"] for row in rows} == {str(private.id), str(scoped.id), str(global_document.id)}
    assert all("storage_key" not in row for row in rows)
    assert bob.get(f"/documents/{private.id}").status_code == 404
    assert bob.get(f"/documents/{scoped.id}").status_code == 404
    assert bob.get(f"/documents?conversation_id={conversation.id}").status_code == 404
    assert bob.get(f"/documents/{global_document.id}").status_code == 200
    assert app.state.documents.find_duplicate(other_id, "personal", "a" * 64) is None
    assert app.state.documents.find_duplicate(user_id, "personal", "a" * 64).id == private.id
    with pytest.raises(ServiceError):
        app.state.documents.create_private(other_id, document_input("conversation", conversation.id))
    with pytest.raises(ServiceError):
        app.state.documents.save_chunks(other_id, private.id, [ChunkCreate(chunk_index=1, content="x")])
    with pytest.raises(ServiceError):
        app.state.documents.delete(other_id, private.id)


def test_citations_enforce_role_mode_owner_and_snapshot(make_user, app):
    _, user_id, _ = make_user()
    _, other_id, _ = make_user("bob")
    conversations = app.state.conversations
    row = conversations.create(user_id, ConversationCreate())
    other_row = conversations.create(user_id, ConversationCreate())
    global_document, global_chunk = source(app)
    private_document, private_chunk = source(app, user_id, "personal")
    _, scoped_chunk = source(app, user_id, "conversation", other_row.id)
    _, foreign_chunk = source(app, other_id, "personal")
    user_message = conversations.append_user(user_id, row.id, UserMessageCreate(content="User text"))
    with pytest.raises(ServiceError):
        conversations.complete_assistant(user_id, user_message.id, "Supplied persisted text")
    assistant = conversations.append_assistant(user_id, row.id, "grounded")
    for chunk in (private_chunk, foreign_chunk):
        with pytest.raises(ServiceError):
            conversations.complete_assistant(user_id, assistant.id, "Supplied persisted text", [CitationInput(chunk_id=chunk.id, citation_number=1)])
    completed = conversations.complete_assistant(user_id, assistant.id, "Supplied persisted text [1]",
        [CitationInput(chunk_id=global_chunk.id, citation_number=1)])
    assert completed.citations[0].title == global_document.title and completed.citations[0].page_start == 2
    personal = conversations.append_assistant(user_id, row.id, "personal_library")
    for chunk in (global_chunk, scoped_chunk, foreign_chunk):
        with pytest.raises(ServiceError):
            conversations.complete_assistant(user_id, personal.id, "Supplied persisted text", [CitationInput(chunk_id=chunk.id, citation_number=1)])
    conversations.complete_assistant(user_id, personal.id, "Personal supplied text", [CitationInput(chunk_id=private_chunk.id, citation_number=1)])
    from cinemind.schemas import ConversationPatch
    conversations.patch(user_id, row.id, ConversationPatch(mode="personal_library"))
    assert conversations.messages(user_id, row.id)[1].response_mode == "grounded"
    assert conversations.messages(user_id, row.id)[1].citations[0].available
    app.state.documents.set_ingestion_state(None, global_document.id, IngestionUpdate(status="failed", error_code="source_unavailable"))
    assert not conversations.messages(user_id, row.id)[1].citations[0].available
    with pytest.raises(ServiceError):
        conversations.resolve_citation(other_id, row.id, assistant.id, 1)


def test_source_deletion_preserves_answer_and_resolves_unavailable(make_user, app, db):
    _, user_id, _ = make_user()
    row = app.state.conversations.create(user_id, ConversationCreate())
    document, chunk = source(app)
    assistant = app.state.conversations.append_assistant(user_id, row.id, "general")
    app.state.conversations.complete_assistant(user_id, assistant.id, "Supplied saved answer [1]", [CitationInput(chunk_id=chunk.id, citation_number=1)])
    app.state.documents.delete(None, document.id)
    history = app.state.conversations.messages(user_id, row.id)
    assert history[0].content == "Supplied saved answer [1]" and history[0].citations == []
    assert not app.state.conversations.resolve_citation(user_id, row.id, assistant.id, 1).available
    with db.sessions.begin() as session:
        assert session.scalar(select(func.count()).select_from(MessageCitation)) == 0


def test_conversation_cascades_and_account_memory_survives(make_user, app, db):
    _, user_id, _ = make_user()
    row = app.state.conversations.create(user_id, ConversationCreate())
    message = app.state.conversations.append_user(user_id, row.id, UserMessageCreate(content="Source"))
    account = app.state.memories.create(user_id, MemoryCreate(kind="note", content="Account text", source_message_id=message.id))
    scoped = app.state.memories.create(user_id, MemoryCreate(kind="note", content="Scoped text", conversation_id=row.id, source_message_id=message.id))
    scoped_document, _ = source(app, user_id, "conversation", row.id)
    global_document, _ = source(app)
    app.state.conversations.delete(user_id, row.id)
    with db.sessions.begin() as session:
        assert session.get(Memory, account.id).source_message_id is None
        assert session.get(Memory, scoped.id) is None
        assert session.get(Document, scoped_document.id) is None
        assert session.get(Document, global_document.id) is not None
        assert session.get(Message, message.id) is None


def test_account_deletion_removes_only_owned_rows(make_user, app, db):
    _, user_id, _ = make_user()
    _, other_id, _ = make_user("bob")
    first = app.state.conversations.create(user_id, ConversationCreate())
    second = app.state.conversations.create(other_id, ConversationCreate())
    message = app.state.conversations.append_user(user_id, first.id, UserMessageCreate(content="Private source"))
    app.state.memories.create(user_id, MemoryCreate(kind="note", content="Private memory", source_message_id=message.id))
    private, _ = source(app, user_id, "personal")
    other, _ = source(app, other_id, "personal")
    global_document, _ = source(app)
    app.state.auth.delete_account(user_id)
    from cinemind.models import Conversation, EmailVerification, LoginSession
    with db.sessions.begin() as session:
        assert session.get(User, user_id) is None and session.get(User, other_id) is not None
        for model, field in ((Conversation, Conversation.user_id), (Memory, Memory.user_id),
                             (EmailVerification, EmailVerification.user_id), (LoginSession, LoginSession.user_id)):
            assert session.scalar(select(func.count()).select_from(model).where(field == user_id)) == 0
        assert session.get(Document, private.id) is None
        assert session.get(Document, other.id) is not None and session.get(Document, global_document.id) is not None
        assert session.get(Conversation, second.id) is not None


def test_assistant_failure_and_atomic_completion(make_user, app):
    _, user_id, _ = make_user()
    conversations = app.state.conversations
    row = conversations.create(user_id, ConversationCreate())
    pending = conversations.append_assistant(user_id, row.id, "general")
    failed = conversations.fail_assistant(user_id, pending.id)
    assert failed.status == "failed" and failed.content is None
    with pytest.raises(ServiceError):
        conversations.complete_assistant(user_id, failed.id, "Supplied text")
    pending = conversations.append_assistant(user_id, row.id, "grounded")
    _, chunk = source(app)
    with pytest.raises(ServiceError):
        conversations.complete_assistant(user_id, pending.id, "Supplied text", [
            CitationInput(chunk_id=chunk.id, citation_number=1), CitationInput(chunk_id=uuid4(), citation_number=2),
        ])
    history = conversations.messages(user_id, row.id)
    assert history[-1].status == "pending" and history[-1].content is None and history[-1].citations == []


def test_allowed_chunks_and_duplicate_batch_are_scoped(make_user, app):
    _, user_id, _ = make_user()
    _, other_id, _ = make_user("bob")
    row = app.state.conversations.create(user_id, ConversationCreate())
    global_document, _ = source(app)
    personal, _ = source(app, user_id, "personal")
    scoped, _ = source(app, user_id, "conversation", row.id)
    service = app.state.documents
    assert len(service.allowed_chunks(user_id, row.id, global_document.id, "general")) == 1
    assert len(service.allowed_chunks(user_id, row.id, personal.id, "personal_library")) == 1
    assert len(service.allowed_chunks(user_id, row.id, scoped.id, "personal_library")) == 1
    for owner, document_id, mode in ((user_id, global_document.id, "personal_library"),
        (user_id, personal.id, "grounded"), (other_id, personal.id, "personal_library")):
        with pytest.raises(ServiceError) as caught:
            service.allowed_chunks(owner, row.id, document_id, mode)
        assert caught.value.status == 404
    with pytest.raises(ServiceError) as caught:
        service.save_chunks(user_id, personal.id, [ChunkCreate(chunk_index=0, content="duplicate"), ChunkCreate(chunk_index=1, content="new")])
    assert caught.value.status == 409
    assert len(service.allowed_chunks(user_id, row.id, personal.id, "personal_library")) == 1
