"""User management request and response types shared by the desktop and server."""

from pydantic import BaseModel, ConfigDict, Field

from chat_archive.agent_access.policy import ChatScope, Grant, Identifier
from chat_archive.agent_access.tools import Document


class NewGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: Identifier
    scope: ChatScope
    operation_id: Identifier | None = None


class ChangeGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, strict=True)
    operation_id: Identifier
    scope: ChatScope | None


class ResolveAddition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allow: bool = Field(strict=True)


def grant_document(grant: Grant) -> Document:
    return {
        "id": grant.id, "version": grant.version, "account_id": grant.account_id,
        "scope": grant.scope.model_dump(mode="json") if grant.scope else None,
    }
