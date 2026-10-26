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

"""Context Pack: chunked, digest-checked context with prefix-reuse deltas.

A full pack carries every chunk of one prepared context. A delta names how many leading chunks
of a base pack the receiver keeps and the chunks to append; the receiver rebuilds the target
solely from serialized bytes and verifies the target digest.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

CONTEXT_PACK_SCHEMA = "powercontext.context-pack.v1"
CONTEXT_PACK_DELTA_SCHEMA = "powercontext.context-pack-delta.v1"
DEFAULT_CHUNK_BYTES = 512

_PACK_FIELDS = frozenset({"schema", "items", "digest", "byte_count"})
_DELTA_FIELDS = frozenset({"schema", "base_digest", "target_digest", "prefix_items", "appended"})
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class ContextPackError(ValueError):
    """A Context Pack or delta violates the wire protocol or chunk contract."""


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode(
        "utf-8"
    )


def _encode_items(items: tuple[bytes, ...]) -> list[str]:
    return [base64.b64encode(item).decode("ascii") for item in items]


def _decode_items(value: Any, *, name: str) -> tuple[bytes, ...]:
    if not isinstance(value, list):
        raise ContextPackError(f"{name} must be an array of Base64 strings")
    decoded: list[bytes] = []
    for item in value:
        if not isinstance(item, str):
            raise ContextPackError(f"{name} entries must be Base64 strings")
        try:
            raw = base64.b64decode(item.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError) as error:
            raise ContextPackError(f"{name} entry is not standard Base64") from error
        if base64.b64encode(raw).decode("ascii") != item:
            raise ContextPackError(f"{name} entry is not canonical standard Base64")
        decoded.append(raw)
    return tuple(decoded)


def _validate_items(items: Any) -> tuple[bytes, ...]:
    if isinstance(items, (bytes, bytearray, str)) or not isinstance(items, Iterable):
        raise ContextPackError("items must be a tuple of bytes")
    normalized = tuple(items)
    for item in normalized:
        if not isinstance(item, bytes):
            raise ContextPackError("every item must be bytes")
    return normalized


def _pack_digest(items: tuple[bytes, ...]) -> str:
    unsigned = {"schema": CONTEXT_PACK_SCHEMA, "items": _encode_items(items)}
    return "sha256:" + hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _pack_wire(items: tuple[bytes, ...], digest: str) -> bytes:
    encoded = _encode_items(items)
    base = {"schema": CONTEXT_PACK_SCHEMA, "items": encoded, "digest": digest}
    # byte_count includes its own decimal representation; iterate to the fixed point.
    count = len(_canonical_json({**base, "byte_count": 0}))
    while True:
        wire = _canonical_json({**base, "byte_count": count})
        if len(wire) == count:
            return wire
        count = len(wire)


def _load_object(data: Any, *, fields: frozenset[str], kind: str) -> dict[str, Any]:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise ContextPackError(f"{kind} wire data must be bytes")

    def reject_constant(value: str) -> Any:
        raise ContextPackError(f"{kind} contains a non-finite number: {value}")

    try:
        payload = json.loads(bytes(data).decode("utf-8"), parse_constant=reject_constant)
    except ContextPackError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise ContextPackError(f"{kind} is not valid UTF-8 JSON") from error
    if not isinstance(payload, dict):
        raise ContextPackError(f"{kind} must be a JSON object")
    if set(payload) != fields:
        missing = sorted(fields - set(payload))
        extra = sorted(set(payload) - fields)
        raise ContextPackError(f"{kind} fields are invalid (missing={missing}, extra={extra})")
    return payload


def _validate_digest(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ContextPackError(f"{name} must be 'sha256:<64 lowercase hex>'")
    return value


def _validate_prefix(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ContextPackError("prefix_items must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class ContextPack:
    """A complete, digest-checked chunk sequence of one prepared context."""

    items: tuple[bytes, ...]
    digest: str
    byte_count: int
    _wire: bytes = field(default=b"", repr=False, compare=False)

    def __post_init__(self) -> None:
        items = _validate_items(self.items)
        object.__setattr__(self, "items", items)
        expected = _pack_digest(items)
        if self.digest != expected:
            raise ContextPackError("pack digest does not match its items")
        wire = _pack_wire(items, expected)
        if isinstance(self.byte_count, bool) or self.byte_count != len(wire):
            raise ContextPackError("pack byte_count does not match its serialized size")
        object.__setattr__(self, "_wire", wire)

    @classmethod
    def build(cls, items: Iterable[bytes]) -> ContextPack:
        normalized = _validate_items(items)
        digest = _pack_digest(normalized)
        return cls(normalized, digest, len(_pack_wire(normalized, digest)))

    @classmethod
    def from_content(cls, content: str | None, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> ContextPack:
        return cls.build(chunk_content(content, chunk_bytes=chunk_bytes))

    @property
    def content(self) -> bytes:
        return b"".join(self.items)

    def to_bytes(self) -> bytes:
        return self._wire

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPack:
        payload = _load_object(data, fields=_PACK_FIELDS, kind="context pack")
        if payload["schema"] != CONTEXT_PACK_SCHEMA:
            raise ContextPackError("unsupported context pack schema")
        items = _decode_items(payload["items"], name="items")
        digest = _validate_digest(payload["digest"], name="digest")
        byte_count = payload["byte_count"]
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ContextPackError("byte_count must be a non-negative integer")
        return cls(items, digest, byte_count)


@dataclass(frozen=True, slots=True)
class ContextPackDelta:
    """Keep ``prefix_items`` leading chunks of the base pack, then append ``appended``."""

    base_digest: str
    target_digest: str
    prefix_items: int
    appended: tuple[bytes, ...]

    def __post_init__(self) -> None:
        _validate_digest(self.base_digest, name="base_digest")
        _validate_digest(self.target_digest, name="target_digest")
        _validate_prefix(self.prefix_items)
        object.__setattr__(self, "appended", _validate_items(self.appended))

    @classmethod
    def between(cls, base: ContextPack, target: ContextPack) -> ContextPackDelta:
        if not isinstance(base, ContextPack) or not isinstance(target, ContextPack):
            raise ContextPackError("delta endpoints must be ContextPack values")
        prefix = 0
        for left, right in zip(base.items, target.items, strict=False):
            if left != right:
                break
            prefix += 1
        return cls(base.digest, target.digest, prefix, target.items[prefix:])

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": CONTEXT_PACK_DELTA_SCHEMA,
            "base_digest": self.base_digest,
            "target_digest": self.target_digest,
            "prefix_items": self.prefix_items,
            "appended": _encode_items(self.appended),
        }

    def to_bytes(self) -> bytes:
        return _canonical_json(self._payload())

    @property
    def byte_count(self) -> int:
        return len(self.to_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> ContextPackDelta:
        payload = _load_object(data, fields=_DELTA_FIELDS, kind="context pack delta")
        if payload["schema"] != CONTEXT_PACK_DELTA_SCHEMA:
            raise ContextPackError("unsupported context pack delta schema")
        return cls(
            _validate_digest(payload["base_digest"], name="base_digest"),
            _validate_digest(payload["target_digest"], name="target_digest"),
            _validate_prefix(payload["prefix_items"]),
            _decode_items(payload["appended"], name="appended"),
        )


def apply_context_pack_delta(base: ContextPack, delta: ContextPackDelta) -> ContextPack:
    """Rebuild and verify the target pack from a base pack and a delta."""

    if not isinstance(base, ContextPack):
        raise ContextPackError("base must be a ContextPack")
    if not isinstance(delta, ContextPackDelta):
        raise ContextPackError("delta must be a ContextPackDelta")
    if base.digest != _pack_digest(base.items):
        raise ContextPackError("base pack digest is invalid")
    if delta.base_digest != base.digest:
        raise ContextPackError("delta base_digest does not match the base pack")
    prefix = _validate_prefix(delta.prefix_items)
    if prefix > len(base.items):
        raise ContextPackError("delta prefix_items exceeds the base pack")
    target = ContextPack.build((*base.items[:prefix], *delta.appended))
    if target.digest != delta.target_digest:
        raise ContextPackError("rebuilt pack does not match delta target_digest")
    return target


def chunk_content(content: str | bytes | None, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES) -> tuple[bytes, ...]:
    """Deterministically split content at line ends, then at UTF-8 boundaries within ``chunk_bytes``."""

    if isinstance(chunk_bytes, bool) or not isinstance(chunk_bytes, int) or chunk_bytes < 1:
        raise ContextPackError("chunk_bytes must be a positive integer")
    if content is None:
        return ()
    if isinstance(content, str):
        try:
            raw = content.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ContextPackError("content is not encodable as UTF-8") from error
    elif isinstance(content, bytes):
        raw = content
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ContextPackError("content is not valid UTF-8") from error
    else:
        raise ContextPackError("content must be text")
    chunks: list[bytes] = []
    for line in raw.splitlines(keepends=True):
        chunks.extend(_split_line(line, chunk_bytes))
    return tuple(chunks)


def _split_line(line: bytes, limit: int) -> list[bytes]:
    pieces: list[bytes] = []
    start = 0
    while start < len(line):
        end = min(start + limit, len(line))
        # Back off to a UTF-8 character boundary (never cut before a continuation byte).
        while end < len(line) and end > start and (line[end] & 0xC0) == 0x80:
            end -= 1
        if end == start:
            raise ContextPackError("chunk_bytes is too small to hold a complete UTF-8 character")
        pieces.append(line[start:end])
        start = end
    return pieces


def chunk_against_base(
    content: str | None, base: ContextPack, *, chunk_bytes: int = DEFAULT_CHUNK_BYTES
) -> tuple[bytes, ...]:
    """Chunk ``content`` so its leading chunks reuse as many base chunks as possible.

    Leading base chunks that are an exact byte prefix of the content are kept verbatim; the rest
    is chunked deterministically. The concatenation always equals the content.
    """

    raw = b"" if content is None else content.encode("utf-8")
    reused: list[bytes] = []
    offset = 0
    for item in base.items:
        if not item or not raw.startswith(item, offset):
            break
        try:
            item.decode("utf-8")
        except UnicodeDecodeError:
            break
        if len(item) > chunk_bytes:
            break
        reused.append(item)
        offset += len(item)
    remainder = raw[offset:]
    return (*reused, *chunk_content(remainder, chunk_bytes=chunk_bytes))


__all__ = [
    "CONTEXT_PACK_DELTA_SCHEMA",
    "CONTEXT_PACK_SCHEMA",
    "DEFAULT_CHUNK_BYTES",
    "ContextPack",
    "ContextPackDelta",
    "ContextPackError",
    "apply_context_pack_delta",
    "chunk_against_base",
    "chunk_content",
]
