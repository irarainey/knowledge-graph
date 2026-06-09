"""Response model for the ``/users`` endpoint."""

from __future__ import annotations

from pydantic import BaseModel, Field

from authz import Identity


class UsersResponse(BaseModel):
    """The selectable identities and the policy version they came from.

    The chat UI calls this to populate its identity selector, so it never has to
    hard-code the list of users; changing the policy file changes the selector.
    """

    version: str = Field(description="Version of the access policy these identities came from.")
    users: list[Identity] = Field(description="Selectable identities, in policy order.")
