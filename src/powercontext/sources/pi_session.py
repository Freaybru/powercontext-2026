# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Pi v3 session branch capture with durable Connector checkpoints."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.connectors import ConnectorRunCompletion, ConnectorRunSession, ConnectorRunStatus
from powercontext.sources.models import Source, SourceMaterialization

PI_EVENT_SOURCE_NAME = "pi-event"
PI_SESSION_CONNECTOR_NAME = "pi-session-history"


class PiSessionInput(BaseModel):
    """Offline Pi v3 JSONL bytes plus an optional selected leaf."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: bytes
    leaf_id: str | None = None


class PiEventInput(BaseModel):
    """One visible Pi session event ready to materialize as a Source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    event_id: str
    parent_id: str | None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue]


class PiEventSource(Source):
    """Captured Pi session event with stable session/event identity."""

    source_id: str
    session_id: str
    event_id: str
    parent_id: str | None = None
    event_type: str
    role: str
    content: str
    record: dict[str, JsonValue] = Field(default_factory=dict)


def pi_event_source_id(session_id: str, event_id: str) -> str:
    """Build the bounded, delimiter-safe identity for one Pi event."""

    if not isinstance(session_id, str) or not session_id or session_id != session_id.strip():
        raise ValueError("session_id must be a non-empty trimmed string")
    if not isinstance(event_id, str) or not event_id or event_id != event_id.strip():
        raise ValueError("event_id must be a non-empty trimmed string")
    identity = f"pi:{quote(session_id, safe='')}:{quote(event_id, safe='')}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pi event source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return identity


def parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    """Parse a Pi v3 JSONL archive into the selected branch's visible events."""

    try:
        return _parse_pi_session(value)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("invalid Pi session") from error


def _parse_pi_session(value: PiSessionInput) -> tuple[PiEventInput, ...]:
    try:
        text = value.content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Pi session content must be valid UTF-8") from error

    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid JSON on line {line_number}") from error
        if not isinstance(parsed, dict):
            raise ValueError(f"Pi session records must be objects (line {line_number})")
        records.append(parsed)

    if not records:
        raise ValueError("Pi session must contain a session header")

    header = records[0]
    if header.get("type") != "session":
        raise ValueError("first Pi session record must have type=session")
    if header.get("version") != 3:
        raise ValueError("Pi session version must be 3")
    session_id = header.get("id")
    if not isinstance(session_id, str) or not session_id:
        raise ValueError("Pi session id must be a non-empty string")

    nodes: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in records[1:]:
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            raise ValueError("Pi event id must be a non-empty string")
        if event_id in nodes:
            raise ValueError(f"duplicate Pi event id: {event_id}")
        parent_id = record.get("parentId", None)
        if parent_id is not None and not isinstance(parent_id, str):
            raise ValueError(f"invalid parentId for event {event_id}")
        if isinstance(parent_id, str) and parent_id not in nodes:
            raise ValueError(f"missing parent for event {event_id}")
        if parent_id == event_id:
            raise ValueError(f"cyclic parent for event {event_id}")
        nodes[event_id] = record
        order.append(event_id)

    if not order:
        raise ValueError("Pi session has no events")

    leaf_id = value.leaf_id if value.leaf_id is not None else order[-1]
    if leaf_id not in nodes:
        raise ValueError(f"missing leaf event: {leaf_id}")

    chain_ids: list[str] = []
    seen: set[str] = set()
    current: str | None = leaf_id
    while current is not None:
        if current in seen:
            raise ValueError("cyclic Pi session parent chain")
        seen.add(current)
        chain_ids.append(current)
        parent = nodes[current].get("parentId", None)
        if parent is not None and parent not in nodes:
            raise ValueError(f"missing parent for event {current}")
        current = parent
    chain_ids.reverse()

    events: list[PiEventInput] = []
    for event_id in chain_ids:
        visible = _visible_event(session_id, nodes[event_id])
        if visible is not None:
            pi_event_source_id(visible.session_id, visible.event_id)
            events.append(visible)
    return tuple(events)


def _visible_event(session_id: str, record: dict[str, Any]) -> PiEventInput | None:
    event_type = record.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise ValueError("Pi event type must be a non-empty string")
    event_id = record["id"]
    parent_id = record.get("parentId", None)
    if not isinstance(parent_id, str | None):
        raise ValueError(f"invalid parentId for event {event_id}")

    if event_type in {"compaction", "branch_summary"}:
        summary = record.get("summary")
        if not isinstance(summary, str) or summary == "":
            return None
        role = event_type
        body = summary
    elif event_type == "custom_message":
        body = _extract_text_payload(record.get("content"))
        if body is None or body == "":
            return None
        role = "custom"
    elif event_type == "message":
        message = record.get("message")
        if not isinstance(message, dict):
            raise ValueError(f"message event {event_id} must contain a message object")
        role_value = message.get("role")
        if not isinstance(role_value, str) or not role_value:
            raise ValueError(f"message event {event_id} must declare a role")
        role = role_value
        body = _message_body(message)
        if body is None or body == "":
            return None
    else:
        # Control / non-textual nodes keep graph connectivity only.
        return None

    return PiEventInput(
        session_id=session_id,
        event_id=event_id,
        parent_id=parent_id,
        event_type=event_type,
        role=role,
        content=f"{role}: {body}",
        record=dict(record),
    )


def _message_body(message: dict[str, Any]) -> str | None:
    role = message.get("role")
    if role == "toolResult":
        text = _extract_text_payload(message.get("content"))
        if text is None:
            text = ""
        tool_name = message.get("toolName", "")
        tool_call_id = message.get("toolCallId", "")
        return f"[tool_result {tool_name} {tool_call_id}] {text}"
    if role == "bashExecution":
        command = message.get("command", "")
        output = message.get("output", "")
        if not isinstance(command, str):
            command = "" if command is None else str(command)
        if not isinstance(output, str):
            output = "" if output is None else str(output)
        return f"$ {command}\n{output}"
    return _extract_text_payload(message.get("content"))


def _extract_text_payload(content: object) -> str | None:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return None
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            parts.append("" if text is None else str(text))
        elif block_type == "toolCall":
            name = block.get("name", "")
            call_id = block.get("id", "")
            arguments = block.get("arguments", {})
            if not isinstance(arguments, dict):
                raise ValueError("toolCall arguments must be a JSON object")
            encoded = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            parts.append(f"[tool_call {name} {call_id}] {encoded}")
        # thinking / image / unknown blocks are not textual evidence
    if not parts:
        return None
    return "\n".join(parts)


def _record_digest(record: dict[str, JsonValue] | dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _checkpoint_imported(checkpoint: JsonValue | None) -> dict[str, str]:
    if checkpoint is None:
        return {}
    if not isinstance(checkpoint, dict):
        raise ValueError("invalid Pi session connector checkpoint")
    imported = checkpoint.get("imported")
    if imported is None:
        return {}
    if not isinstance(imported, dict):
        raise ValueError("invalid Pi session connector checkpoint")
    result: dict[str, str] = {}
    for key, value in imported.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError("invalid Pi session connector checkpoint")
        result[key] = value
    return result


class PiEventSourceAdapter:
    """Materialize and read captured Pi event Sources."""

    input_class = PiEventInput
    name = PI_EVENT_SOURCE_NAME
    source_class = PiEventSource

    async def resolve(self, value: PiEventInput, /) -> PiEventSource:
        source_id = pi_event_source_id(value.session_id, value.event_id)
        return PiEventSource(
            name=source_id,
            definition_version="1",
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            session_id=value.session_id,
            event_id=value.event_id,
            parent_id=value.parent_id,
            event_type=value.event_type,
            role=value.role,
            content=value.content,
            record=value.record,
        )

    async def read(self, source: PiEventSource, /) -> PiEventInput:
        return PiEventInput(
            session_id=source.session_id,
            event_id=source.event_id,
            parent_id=source.parent_id,
            event_type=source.event_type,
            role=source.role,
            content=source.content,
            record=source.record,
        )


class PiEventTextEvidenceProjection:
    """Project a Pi event as full textual evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PiEventSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PiEventSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PI_EVENT_SOURCE_NAME,
            source_id=source.source_id,
            content=source.content,
            metadata={
                "session_id": source.session_id,
                "event_id": source.event_id,
                "parent_id": source.parent_id,
                "event_type": source.event_type,
                "role": source.role,
            },
        )


class PiSessionConnector:
    """Incrementally import visible events from one Pi session JSONL file."""

    name = PI_SESSION_CONNECTOR_NAME
    version = "1"
    source_definitions = frozenset({PI_EVENT_SOURCE_NAME})

    def __init__(self, path: str | Path, leaf_id: str | None = None) -> None:
        self.path = Path(path)
        self.leaf_id = leaf_id

    async def run(self, session: ConnectorRunSession, /) -> ConnectorRunCompletion:
        content = self.path.read_bytes()
        events = parse_pi_session(PiSessionInput(content=content, leaf_id=self.leaf_id))
        imported = _checkpoint_imported(session.checkpoint)
        next_imported = dict(imported)

        for event in events:
            source_id = pi_event_source_id(event.session_id, event.event_id)
            digest = _record_digest(event.record)
            previous = imported.get(source_id)
            if previous is not None:
                if previous != digest:
                    raise ValueError(f"imported Pi event was rewritten: {event.event_id}")
                continue
            await session.submit(source_id, PI_EVENT_SOURCE_NAME, event)
            next_imported[source_id] = digest

        return ConnectorRunCompletion(
            status=ConnectorRunStatus.COMPLETE,
            checkpoint={"imported": next_imported},
        )


PI_EVENT_SOURCE_ADAPTER = PiEventSourceAdapter()
PI_EVENT_SOURCE_DEFINITION = AdapterSourceDefinition(
    PI_EVENT_SOURCE_ADAPTER,
    version="1",
    projections=(PiEventTextEvidenceProjection(),),
)

__all__ = [
    "PI_EVENT_SOURCE_ADAPTER",
    "PI_EVENT_SOURCE_DEFINITION",
    "PI_EVENT_SOURCE_NAME",
    "PI_SESSION_CONNECTOR_NAME",
    "PiEventInput",
    "PiEventSource",
    "PiEventSourceAdapter",
    "PiEventTextEvidenceProjection",
    "PiSessionConnector",
    "PiSessionInput",
    "parse_pi_session",
    "pi_event_source_id",
]
