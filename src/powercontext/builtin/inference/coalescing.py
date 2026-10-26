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

"""Cross-call request coalescing and a bounded success cache for Embedding models.

``CoalescingEmbeddingModel`` wraps any :class:`EmbeddingModel`. Calls sharing
one wrapper on one event loop share identical texts, whether they arrive in the
same call, inside one coalescing window, or while a provider batch is still in
flight. Successful vectors enter an LRU cache; failures are delivered to every
waiter of the failed batch and never cached. Provider work runs in tasks owned
by the wrapper, so cancelling one caller never cancels shared work.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from powercontext.builtin.inference.models import EmbeddingResult, EmbeddingVector, InferenceUsage
from powercontext.builtin.inference.protocols import EmbeddingModel, QueryEmbeddingModel

if TYPE_CHECKING:
    from powercontext.builtin.artifacts.memory.models import EmbeddingProfile

__all__ = ["CoalescingEmbeddingModel"]

_Provider = Callable[[tuple[str, ...]], Awaitable[EmbeddingResult]]


class _Batch:
    """One deduplicated provider request and the future its waiters share."""

    __slots__ = ("future", "texts")

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.texts: list[str] = []
        self.future: asyncio.Future[tuple[tuple[EmbeddingVector, ...], InferenceUsage]] = loop.create_future()


class _Channel:
    """Coalescing state for one provider entrypoint (documents or queries)."""

    def __init__(self, provider: _Provider, *, max_batch_size: int, coalesce_seconds: float, cache_size: int) -> None:
        self._provider = provider
        self._max_batch_size = max_batch_size
        self._coalesce_seconds = coalesce_seconds
        self._cache_size = cache_size
        self._cache: OrderedDict[str, EmbeddingVector] = OrderedDict()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._inflight: dict[str, _Batch] = {}
        self._open: _Batch | None = None
        self._open_timer: asyncio.Handle | None = None
        self._tasks: set[asyncio.Task[None]] = set()

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is not loop:
            # Futures and tasks belong to one loop; cached vectors are plain data.
            self._loop = loop
            self._inflight = {}
            self._open = None
            self._open_timer = None
            self._tasks = set()
        return loop

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingResult:
        texts = tuple(texts)
        if not texts:
            return EmbeddingResult(vectors=(), usage=InferenceUsage(requests=0))
        loop = self._bind_loop()
        resolved: dict[str, EmbeddingVector] = {}
        waiting: dict[str, _Batch] = {}
        # Registration is synchronous so later callers see this call's work.
        for text in dict.fromkeys(texts):
            cached = self._cache.get(text)
            if cached is not None:
                self._cache.move_to_end(text)
                resolved[text] = cached
                continue
            batch = self._inflight.get(text)
            if batch is None:
                batch = self._enqueue(loop, text)
            waiting[text] = batch
        if self._open is not None and self._open_timer is None:
            self._schedule_open(loop)
        batches = list(dict.fromkeys(waiting.values()))
        usage = InferenceUsage(requests=0)
        if batches:
            # Shield shared futures: cancelling this caller must not cancel them.
            outcomes = await asyncio.shield(asyncio.gather(*(batch.future for batch in batches), return_exceptions=True))
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            by_batch = {id(batch): outcome for batch, outcome in zip(batches, outcomes, strict=True)}
            for batch, outcome in zip(batches, outcomes, strict=True):
                usage = _add_usage(usage, outcome[1])  # type: ignore[index]
            for text, batch in waiting.items():
                vectors = by_batch[id(batch)][0]  # type: ignore[index]
                resolved[text] = vectors[batch.texts.index(text)]
        return EmbeddingResult(vectors=tuple(resolved[text] for text in texts), usage=usage)

    def _enqueue(self, loop: asyncio.AbstractEventLoop, text: str) -> _Batch:
        batch = self._open
        if batch is None:
            batch = self._open = _Batch(loop)
        batch.texts.append(text)
        self._inflight[text] = batch
        if len(batch.texts) >= self._max_batch_size:
            self._dispatch_open()
        return batch

    def _schedule_open(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._coalesce_seconds > 0:
            self._open_timer = loop.call_later(self._coalesce_seconds, self._dispatch_open)
        else:
            self._open_timer = loop.call_soon(self._dispatch_open)

    def _dispatch_open(self) -> None:
        batch = self._open
        if self._open_timer is not None:
            self._open_timer.cancel()
            self._open_timer = None
        self._open = None
        if batch is None or not batch.texts:
            return
        assert self._loop is not None
        task = self._loop.create_task(self._run(batch))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(self, batch: _Batch) -> None:
        texts = tuple(batch.texts)
        try:
            result = await self._provider(texts)
            if len(result.vectors) != len(texts):
                raise ValueError(  # noqa: TRY301 - delivered to every waiter below
                    f"embedding provider returned {len(result.vectors)} vectors for {len(texts)} texts"
                )
        except BaseException as error:
            self._release(batch)
            if not batch.future.done():
                if isinstance(error, Exception):
                    batch.future.set_exception(error)
                    # Waiters may all have been cancelled; never log an unretrieved failure.
                    batch.future.exception()
                else:
                    batch.future.cancel()
            if not isinstance(error, Exception):
                raise
            return
        vectors = tuple(result.vectors)
        self._release(batch)
        if self._cache_size > 0:
            for text, vector in zip(texts, vectors, strict=True):
                self._cache[text] = vector
                self._cache.move_to_end(text)
            while len(self._cache) > self._cache_size:
                self._cache.popitem(last=False)
        if not batch.future.done():
            batch.future.set_result((vectors, result.usage))

    def _release(self, batch: _Batch) -> None:
        for text in batch.texts:
            if self._inflight.get(text) is batch:
                del self._inflight[text]


def _add_usage(total: InferenceUsage, usage: InferenceUsage) -> InferenceUsage:
    def tokens(left: int | None, right: int | None) -> int | None:
        if left is None:
            return right
        if right is None:
            return left
        return left + right

    return InferenceUsage(
        requests=total.requests + usage.requests,
        input_tokens=tokens(total.input_tokens, usage.input_tokens),
        output_tokens=tokens(total.output_tokens, usage.output_tokens),
    )


def _positive_int(name: str, value: object, *, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")  # noqa: TRY003
    return value


class CoalescingEmbeddingModel:
    """Share concurrent Embedding work and cache successful vectors with LRU bounds."""

    def __init__(
        self,
        model: EmbeddingModel,
        *,
        max_batch_size: int = 64,
        coalesce_ms: float = 5.0,
        cache_size: int = 1024,
    ) -> None:
        batch_size = _positive_int("max_batch_size", max_batch_size, minimum=1)
        capacity = _positive_int("cache_size", cache_size, minimum=0)
        if isinstance(coalesce_ms, bool) or not isinstance(coalesce_ms, int | float) or not coalesce_ms >= 0:
            raise ValueError("coalesce_ms must be a non-negative number")  # noqa: TRY003
        if coalesce_ms == float("inf"):
            raise ValueError("coalesce_ms must be finite")  # noqa: TRY003
        self._model = model
        settings = {"max_batch_size": batch_size, "coalesce_seconds": float(coalesce_ms) / 1000, "cache_size": capacity}
        self._documents = _Channel(model.embed, **settings)  # type: ignore[arg-type]
        # Asymmetric providers embed queries differently; keep their vectors apart.
        self._queries = (
            _Channel(model.embed_query, **settings)  # type: ignore[arg-type]
            if isinstance(model, QueryEmbeddingModel)
            else self._documents
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._model.profile

    async def embed(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return await self._documents.embed(texts)

    async def embed_query(self, texts: tuple[str, ...], /) -> EmbeddingResult:
        return await self._queries.embed(texts)
