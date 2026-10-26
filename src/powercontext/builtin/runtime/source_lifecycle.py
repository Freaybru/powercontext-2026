# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Propagate Source withdrawal and restoration through caller-declared evidence DAGs.

Each scope owns one directed acyclic graph of Source, Memory and Topic Memory
nodes. Parents must be registered before their children, so registration order
is a topological order and cycles cannot be expressed. Availability is derived
per node from its parents with an ``all`` or ``any`` policy and persisted.

Native effects are reconciled after the graph state commits:

* Memory nodes deactivate or reactivate their cited logical entry without
  creating body versions, so search and Context follow the derived state.
  Every Memory node carries a ``native_synced`` flag; an interrupted write
  leaves it unset, and replaying the same event finishes the reconciliation.
* Topic Memory nodes are excluded from native Topic search (and therefore from
  Context) while unavailable. Their exact ArtifactRef stays readable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, field_validator
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import ArtifactRef, MemoryCitation
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.tables import SOURCE_LIFECYCLE_NODES_TABLE
from powercontext.builtin.sources import validate_scope_id
from powercontext.limits import MAX_BINDING_NAME_LENGTH
from powercontext.sources import SourceRef

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import BuiltinRuntime

__all__ = [
    "LifecyclePolicy",
    "LifecyclePurpose",
    "ScopedSourceLifecycleService",
    "SourceLifecycleApplication",
    "SourceLifecycleError",
    "SourceLifecycleEvent",
]

LifecyclePolicy = Literal["all", "any"]
LifecyclePurpose = Literal["generation", "audit"]
LifecycleReference = SourceRef | MemoryCitation | ArtifactRef
_KIND_SOURCE = "source"
_KIND_MEMORY = "memory"
_KIND_TOPIC = "topic"
_TOPIC_FAMILY = "topic-memory"


class SourceLifecycleError(ValueError):
    """A lifecycle registration, event or resolution violates the contract."""


class SourceLifecycleEvent(BaseModel):
    """One ordered availability change for a registered Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: StrictStr
    revision: StrictInt
    sequence: StrictInt
    available: StrictBool
    reason: StrictStr

    @field_validator("node_id", "reason")
    @classmethod
    def _require_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be nonempty and not only whitespace")  # noqa: TRY003
        return value

    @field_validator("revision", "sequence")
    @classmethod
    def _require_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("must be a positive integer")  # noqa: TRY003
        return value


class _Node:
    __slots__ = (
        "available",
        "kind",
        "last_available",
        "last_reason",
        "last_sequence",
        "native_synced",
        "node_id",
        "ordinal",
        "parents",
        "policy",
        "reference",
        "source_revision",
    )

    def __init__(self, row: Any) -> None:
        self.node_id = str(row["node_id"])
        self.ordinal = int(row["ordinal"])
        self.kind = str(row["kind"])
        self.reference = _decode_reference(self.kind, str(row["reference"]))
        self.parents = tuple(json.loads(str(row["parents"])))
        self.policy = None if row["policy"] is None else str(row["policy"])
        self.source_revision = None if row["source_revision"] is None else int(row["source_revision"])
        self.available = bool(row["available"])
        self.native_synced = bool(row["native_synced"])
        self.last_sequence = None if row["last_sequence"] is None else int(row["last_sequence"])
        self.last_available = None if row["last_available"] is None else bool(row["last_available"])
        self.last_reason = None if row["last_reason"] is None else str(row["last_reason"])


def _encode_reference(reference: BaseModel) -> str:
    return json.dumps(reference.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def _decode_reference(kind: str, payload: str) -> LifecycleReference:
    data = json.loads(payload)
    if kind == _KIND_SOURCE:
        return SourceRef.model_validate(data)
    if kind == _KIND_MEMORY:
        return MemoryCitation.model_validate(data)
    return ArtifactRef.model_validate(data)


def _require_node_id(node_id: object) -> str:
    if (
        not isinstance(node_id, str)
        or not node_id.strip()
        or node_id != node_id.strip()
        or len(node_id) > MAX_BINDING_NAME_LENGTH
    ):
        raise SourceLifecycleError(f"invalid lifecycle node id: {node_id!r}")
    return node_id


def _derive(policy: str | None, parents: Sequence[str], available: dict[str, bool]) -> bool:
    support = [available[parent] for parent in parents]
    return all(support) if policy == "all" else any(support)


class SourceLifecycleApplication:
    """Select the lifecycle service for one scope."""

    def __init__(self, runtime: BuiltinRuntime, database: AsyncDatabase) -> None:
        self._runtime = runtime
        self._database = database
        self._locks: dict[str, asyncio.Lock] = {}

    def for_scope(self, scope_id: str, /) -> ScopedSourceLifecycleService:
        scope = validate_scope_id(scope_id)
        return ScopedSourceLifecycleService(self, scope)

    def _lock(self, scope_id: str) -> asyncio.Lock:
        lock = self._locks.get(scope_id)
        if lock is None:
            lock = self._locks[scope_id] = asyncio.Lock()
        return lock

    async def unavailable_topic_artifacts(self, scope_id: str, /) -> frozenset[str]:
        """Return Topic Memory artifact ids that currently lack lifecycle support."""

        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            rows = await connection.scalars(
                select(table.c.reference).where(
                    table.c.scope_id == scope_id,
                    table.c.kind == _KIND_TOPIC,
                    table.c.available.is_(False),
                )
            )
            return frozenset(str(json.loads(str(payload))["artifact_id"]) for payload in rows)


class ScopedSourceLifecycleService:
    """Register evidence dependencies and apply Source events in one scope."""

    def __init__(self, application: SourceLifecycleApplication, scope_id: str) -> None:
        self._application = application
        self._runtime = application._runtime
        self._database = application._database
        self.scope_id = scope_id

    # ----------------------------------------------------------------- registration

    async def register_source(self, node_id: str, source_ref: SourceRef, /, *, revision: int) -> None:
        node = _require_node_id(node_id)
        if not isinstance(source_ref, SourceRef):
            raise SourceLifecycleError("register_source requires a SourceRef")
        if type(revision) is not int or revision < 1:
            raise SourceLifecycleError("revision must be a positive integer")
        try:
            await self._runtime.records.for_scope(self.scope_id).get_source(source_ref.source_type, source_ref.source_id)
        except Exception as error:
            raise SourceLifecycleError(f"source is not readable: {source_ref!r}") from error
        await self._register(node, _KIND_SOURCE, source_ref, (), None, revision)

    async def register_memory(
        self,
        node_id: str,
        citation: MemoryCitation,
        /,
        *,
        parents: Sequence[str],
        policy: LifecyclePolicy,
    ) -> None:
        node = _require_node_id(node_id)
        if not isinstance(citation, MemoryCitation):
            raise SourceLifecycleError("register_memory requires a MemoryCitation")
        checked = self._check_derivation(node, parents, policy)
        from powercontext.builtin.runtime.models import GetMemoryEntryRequest

        try:
            await self._runtime.memory.for_scope(self.scope_id).get(GetMemoryEntryRequest(citation=citation))
        except Exception as error:
            raise SourceLifecycleError("memory citation is not readable") from error
        await self._register(node, _KIND_MEMORY, citation, checked, policy, None)

    async def register_topic(
        self,
        node_id: str,
        artifact_ref: ArtifactRef,
        /,
        *,
        parents: Sequence[str],
        policy: LifecyclePolicy,
    ) -> None:
        node = _require_node_id(node_id)
        if not isinstance(artifact_ref, ArtifactRef) or artifact_ref.family != _TOPIC_FAMILY:
            raise SourceLifecycleError("register_topic requires a Topic Memory ArtifactRef")
        checked = self._check_derivation(node, parents, policy)
        try:
            await self._runtime.records.for_scope(self.scope_id).get_artifact_revision(
                _TOPIC_FAMILY, artifact_ref.artifact_id, artifact_ref.revision
            )
        except Exception as error:
            raise SourceLifecycleError("topic artifact is not readable") from error
        await self._register(node, _KIND_TOPIC, artifact_ref, checked, policy, None)

    @staticmethod
    def _check_derivation(node: str, parents: Sequence[str], policy: object) -> tuple[str, ...]:
        if policy not in ("all", "any"):
            raise SourceLifecycleError(f"unknown lifecycle policy: {policy!r}")
        if isinstance(parents, str):
            raise SourceLifecycleError("parents must be a sequence of node ids")
        checked = tuple(_require_node_id(parent) for parent in parents)
        if not checked:
            raise SourceLifecycleError("derived nodes need at least one parent")
        if len(set(checked)) != len(checked):
            raise SourceLifecycleError("parents must not repeat")
        if node in checked:
            raise SourceLifecycleError("a node cannot depend on itself")
        return checked

    async def _register(
        self,
        node: str,
        kind: str,
        reference: BaseModel,
        parents: tuple[str, ...],
        policy: str | None,
        revision: int | None,
    ) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        encoded = _encode_reference(reference)
        async with self._application._lock(self.scope_id):
            async with self._database.transaction() as connection:
                nodes = await self._load(connection)
                existing = nodes.get(node)
                if existing is not None:
                    if (
                        existing.kind == kind
                        and _encode_reference(existing.reference) == encoded  # type: ignore[arg-type]
                        and existing.parents == parents
                        and existing.policy == policy
                        and existing.source_revision == revision
                    ):
                        needs_sync = kind == _KIND_MEMORY and not existing.native_synced
                    else:
                        raise SourceLifecycleError(f"lifecycle node {node!r} is already bound differently")
                else:
                    missing = [parent for parent in parents if parent not in nodes]
                    if missing:
                        raise SourceLifecycleError(f"unregistered parents: {missing}")
                    available = (
                        True
                        if kind == _KIND_SOURCE
                        else _derive(policy, parents, {key: value.available for key, value in nodes.items()})
                    )
                    ordinal = 1 + max((value.ordinal for value in nodes.values()), default=0)
                    # A Memory registered available keeps its native state; one
                    # registered without support must leave native retrieval now.
                    needs_sync = kind == _KIND_MEMORY and not available
                    await connection.execute(
                        insert(table).values(
                            scope_id=self.scope_id,
                            node_id=node,
                            ordinal=ordinal,
                            kind=kind,
                            reference=encoded,
                            parents=json.dumps(list(parents)),
                            policy=policy,
                            source_revision=revision,
                            available=available,
                            native_synced=not needs_sync,
                            last_sequence=None,
                            last_available=None,
                            last_reason=None,
                        )
                    )
            if needs_sync:
                await self._reconcile_memory("lifecycle registration without support")

    # ----------------------------------------------------------------------- events

    async def apply(self, event: SourceLifecycleEvent, /) -> list[str]:
        if not isinstance(event, SourceLifecycleEvent):
            raise SourceLifecycleError("apply requires a SourceLifecycleEvent")
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._application._lock(self.scope_id):
            async with self._database.transaction() as connection:
                nodes = await self._load(connection)
                source = nodes.get(event.node_id)
                if source is None or source.kind != _KIND_SOURCE:
                    raise SourceLifecycleError(f"unknown lifecycle Source: {event.node_id!r}")
                if source.source_revision != event.revision:
                    raise SourceLifecycleError(
                        f"revision {event.revision} does not match lifecycle revision {source.source_revision}"
                    )
                last = source.last_sequence
                if last is not None and event.sequence <= last:
                    replay = (
                        event.sequence == last
                        and source.last_available == event.available
                        and source.last_reason == event.reason
                    )
                    if not replay:
                        raise SourceLifecycleError(f"stale or conflicting event sequence {event.sequence}")
                else:
                    await connection.execute(
                        update(table)
                        .where(table.c.scope_id == self.scope_id, table.c.node_id == source.node_id)
                        .values(
                            available=event.available,
                            last_sequence=event.sequence,
                            last_available=event.available,
                            last_reason=event.reason,
                        )
                    )
                    source.available = event.available
                    await self._propagate(connection, nodes)
            await self._reconcile_memory(event.reason)
            return await self._active_ids()

    async def _propagate(self, connection: AsyncConnection, nodes: dict[str, _Node]) -> None:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        state: dict[str, bool] = {}
        for node in sorted(nodes.values(), key=lambda item: item.ordinal):
            if node.kind == _KIND_SOURCE:
                state[node.node_id] = node.available
                continue
            derived = _derive(node.policy, node.parents, state)
            state[node.node_id] = derived
            if derived == node.available:
                continue
            values: dict[str, object] = {"available": derived}
            if node.kind == _KIND_MEMORY:
                values["native_synced"] = False
            await connection.execute(
                update(table).where(table.c.scope_id == self.scope_id, table.c.node_id == node.node_id).values(**values)
            )
            node.available = derived

    async def _reconcile_memory(self, reason: str) -> None:
        """Bring native Memory entry states in line with unsynced lifecycle nodes."""

        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            pending = [
                node
                for node in (await self._load(connection)).values()
                if node.kind == _KIND_MEMORY and not node.native_synced
            ]
        if not pending:
            return
        from powercontext.builtin.runtime.application import _head_or_none

        runtime = self._runtime
        async with runtime._context(self.scope_id) as context, runtime._locked(self.scope_id):
            service = context.artifacts.memory
            head = await _head_or_none(service, context.artifacts.memory_artifact_id)
            if head is None:
                raise SourceLifecycleError("registered Memory entries have no current Memory")
            states = {item.entry_id: item.state for item in head.content.manifest.entries}
            versions = {entry.entry_id: entry for entry in await service.entries(head)}
            deactivate = []
            reactivate = []
            for node in pending:
                citation = node.reference
                assert isinstance(citation, MemoryCitation)
                version = versions.get(citation.entry_id)
                if version is None:
                    raise SourceLifecycleError(f"memory entry for {node.node_id!r} is no longer present")
                target = "active" if node.available else "inactive"
                if states.get(citation.entry_id) == target:
                    continue
                (reactivate if node.available else deactivate).append(version)
            current = head
            if deactivate:
                current = await service.forget(current, entries=deactivate, reason=reason)
            if reactivate:
                await service.reactivate(current, entries=reactivate, reason=reason)
        async with self._database.transaction() as connection:
            for node in pending:
                await connection.execute(
                    update(table)
                    .where(
                        table.c.scope_id == self.scope_id,
                        table.c.node_id == node.node_id,
                        table.c.available.is_(node.available),
                    )
                    .values(native_synced=True)
                )

    # -------------------------------------------------------------------- resolution

    async def resolve(self, node_id: str, /, *, purpose: LifecyclePurpose) -> LifecycleReference:
        if purpose not in ("generation", "audit"):
            raise SourceLifecycleError(f"unknown lifecycle purpose: {purpose!r}")
        async with self._database.transaction() as connection:
            nodes = await self._load(connection)
        node = nodes.get(node_id) if isinstance(node_id, str) else None
        if node is None:
            raise SourceLifecycleError(f"unknown lifecycle node: {node_id!r}")
        if purpose == "generation" and not node.available:
            raise SourceLifecycleError(f"lifecycle node {node_id!r} is not available for generation")
        return node.reference

    async def active(self) -> list[str]:
        """Return every currently available node id in sorted order."""

        return await self._active_ids()

    async def _active_ids(self) -> list[str]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        async with self._database.transaction() as connection:
            rows = await connection.scalars(
                select(table.c.node_id).where(table.c.scope_id == self.scope_id, table.c.available.is_(True))
            )
            return sorted(str(row) for row in rows)

    async def _load(self, connection: AsyncConnection) -> dict[str, _Node]:
        table = SOURCE_LIFECYCLE_NODES_TABLE
        rows = (
            await connection.execute(select(table).where(table.c.scope_id == self.scope_id).order_by(table.c.ordinal))
        ).mappings()
        return {node.node_id: node for node in map(_Node, rows)}

