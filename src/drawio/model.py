from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import UUID, uuid4


FetchRow = Callable[..., Awaitable[Mapping[str, Any] | None]]


class NodeStore:
    """Use the host PostgreSQL pool; fetch_row follows asyncpg's positional binds."""

    def __init__(self, fetch_row: FetchRow):
        self.fetch_row = fetch_row

    async def create(self, owner: str, xml: str):
        return await self.fetch_row(
            "INSERT INTO drawio_nodes (id, owner, xml) VALUES ($1, $2, $3) "
            "RETURNING id, version, updated_at",
            uuid4(), owner, xml,
        )

    async def metadata(self, owner: str, node_id: UUID):
        return await self.fetch_row(
            "SELECT id, version, updated_at FROM drawio_nodes WHERE id = $1 AND owner = $2",
            node_id, owner,
        )

    async def read(self, owner: str, node_id: UUID):
        return await self.fetch_row(
            "SELECT xml, version FROM drawio_nodes WHERE id = $1 AND owner = $2",
            node_id, owner,
        )

    async def update(self, owner: str, node_id: UUID, version: int, xml: str):
        return await self.fetch_row(
            "UPDATE drawio_nodes SET xml = $4, version = version + 1, updated_at = clock_timestamp() "
            "WHERE id = $1 AND owner = $2 AND version = $3 RETURNING id, version, updated_at",
            node_id, owner, version, xml,
        )

    async def delete(self, owner: str, node_id: UUID, version: int):
        return await self.fetch_row(
            "DELETE FROM drawio_nodes WHERE id = $1 AND owner = $2 AND version = $3 RETURNING id",
            node_id, owner, version,
        )
