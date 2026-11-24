from uuid import UUID

from fastapi import APIRouter, Query, Request

from cinemind.routes.dependencies import Identity
from cinemind.schemas import (
    ConversationCreate, ConversationPatch, ConversationView, DocumentScope, DocumentView,
    MemoryPatch, MemoryView, MessageView, UserMessageCreate,
)

router = APIRouter(tags=["persistence"])


@router.post("/conversations", response_model=ConversationView, status_code=201)
def create_conversation(data: ConversationCreate, request: Request, identity: Identity) -> ConversationView:
    return request.app.state.conversations.create(identity.user_id, data)


@router.get("/conversations", response_model=list[ConversationView])
def list_conversations(request: Request, identity: Identity, limit: int = Query(50, ge=1, le=100),
                       offset: int = Query(0, ge=0, le=100000)) -> list[ConversationView]:
    return request.app.state.conversations.list(identity.user_id, limit, offset)


@router.get("/conversations/{conversation_id}", response_model=ConversationView)
def read_conversation(conversation_id: UUID, request: Request, identity: Identity) -> ConversationView:
    return request.app.state.conversations.read(identity.user_id, conversation_id)


@router.patch("/conversations/{conversation_id}", response_model=ConversationView)
def patch_conversation(conversation_id: UUID, data: ConversationPatch, request: Request, identity: Identity) -> ConversationView:
    return request.app.state.conversations.patch(identity.user_id, conversation_id, data)


@router.delete("/conversations/{conversation_id}", status_code=204)
def delete_conversation(conversation_id: UUID, request: Request, identity: Identity) -> None:
    request.app.state.conversations.delete(identity.user_id, conversation_id)


@router.post("/conversations/{conversation_id}/messages", response_model=MessageView, status_code=201)
def create_message(conversation_id: UUID, data: UserMessageCreate, request: Request, identity: Identity) -> MessageView:
    return request.app.state.conversations.append_user(identity.user_id, conversation_id, data)


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageView])
def list_messages(conversation_id: UUID, request: Request, identity: Identity,
                  limit: int = Query(50, ge=1, le=100), after: int = Query(0, ge=0)) -> list[MessageView]:
    return request.app.state.conversations.messages(identity.user_id, conversation_id, limit, after)


@router.get("/memories", response_model=list[MemoryView])
def list_memories(request: Request, identity: Identity, conversation_id: UUID | None = None,
                 include_account_wide: bool = True, include_superseded: bool = False,
                 limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=100000)) -> list[MemoryView]:
    return request.app.state.memories.list(identity.user_id, conversation_id, include_account_wide=include_account_wide,
        include_superseded=include_superseded, limit=limit, offset=offset)


@router.patch("/memories/{memory_id}", response_model=MemoryView)
def edit_memory(memory_id: UUID, data: MemoryPatch, request: Request, identity: Identity) -> MemoryView:
    return request.app.state.memories.edit(identity.user_id, memory_id, data)


@router.delete("/memories/{memory_id}", status_code=204)
def delete_memory(memory_id: UUID, request: Request, identity: Identity) -> None:
    request.app.state.memories.delete(identity.user_id, memory_id)


@router.get("/documents", response_model=list[DocumentView])
def list_documents(request: Request, identity: Identity, scope: DocumentScope | None = None,
                   conversation_id: UUID | None = None, limit: int = Query(50, ge=1, le=100),
                   offset: int = Query(0, ge=0, le=100000)) -> list[DocumentView]:
    return request.app.state.documents.list(identity.user_id, scope=scope, conversation_id=conversation_id,
                                           limit=limit, offset=offset)


@router.get("/documents/{document_id}", response_model=DocumentView)
def read_document(document_id: UUID, request: Request, identity: Identity) -> DocumentView:
    return request.app.state.documents.read(identity.user_id, document_id)
