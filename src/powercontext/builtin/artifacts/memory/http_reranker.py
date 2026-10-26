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

"""HTTP scoring-service adapter for the Memory reranker contract."""

from __future__ import annotations

import json
import math
from typing import Any

import httpx

from powercontext.builtin.artifacts.memory.models import MemoryHit
from powercontext.builtin.artifacts.memory.reranking import MemoryRerankDecision
from powercontext.builtin.inference import InferenceUsage

HTTP_RERANK_POLICY_ID = "http-cross-encoder-v1"


def _reject_constant(value: str) -> Any:
    raise ValueError(f"rerank response contains a non-finite number: {value}")


class HTTPMemoryReranker:
    """Score candidates with an offline HTTP service in bounded batches, then select globally.

    Every candidate keeps its one-based rank in the full coarse pool; batches never renumber.
    The final selection orders all scores by descending score, ties by ascending original rank.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        client: httpx.AsyncClient,
        batch_size: int = 32,
        policy_id: str = HTTP_RERANK_POLICY_ID,
    ) -> None:
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise ValueError("endpoint must be a non-empty string")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("batch_size must be a positive integer")
        if not isinstance(policy_id, str) or not policy_id:
            raise ValueError("policy_id must be a non-empty string")
        self._endpoint = endpoint
        self._client = client
        self._batch_size = batch_size
        self.policy_id = policy_id

    async def rerank(
        self,
        query: str,
        candidates: tuple[MemoryHit, ...],
        limit: int,
        /,
    ) -> MemoryRerankDecision:
        """Request service scores for every candidate and return the global top ``limit`` ranks."""

        candidates = tuple(candidates)
        if not candidates:
            raise ValueError("rerank requires at least one candidate")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > len(candidates):
            raise ValueError("rerank limit must be between one and the candidate count")
        if not isinstance(query, str):
            raise ValueError("rerank query must be a string")

        scores: dict[int, float] = {}
        requests = 0
        for start in range(0, len(candidates), self._batch_size):
            batch = tuple(range(start + 1, min(start + self._batch_size, len(candidates)) + 1))
            payload = {
                "query": query,
                "candidates": [{"rank": rank, "text": candidates[rank - 1].text} for rank in batch],
            }
            requests += 1
            response = await self._client.post(self._endpoint, json=payload)
            response.raise_for_status()
            scores.update(_parse_scores(response.content, batch))

        ordered = sorted(scores, key=lambda rank: (-scores[rank], rank))
        return MemoryRerankDecision(
            selected_ranks=tuple(ordered[:limit]),
            usage=InferenceUsage(requests=requests),
        )


def _parse_scores(content: bytes, batch: tuple[int, ...]) -> dict[int, float]:
    try:
        body = json.loads(content, parse_constant=_reject_constant)
    except ValueError:
        raise
    except Exception as error:  # e.g. UnicodeDecodeError, RecursionError
        raise ValueError("rerank response is not valid JSON") from error
    if not isinstance(body, dict) or set(body) != {"scores"}:
        raise ValueError("rerank response must contain exactly the 'scores' field")
    records = body["scores"]
    if not isinstance(records, list):
        raise ValueError("rerank response 'scores' must be a list")
    expected = set(batch)
    parsed: dict[int, float] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {"rank", "score"}:
            raise ValueError("each rerank score must contain exactly 'rank' and 'score'")
        rank, score = record["rank"], record["score"]
        if isinstance(rank, bool) or not isinstance(rank, int):
            raise ValueError("rerank score rank must be an integer")
        if rank not in expected:
            raise ValueError(f"rerank score rank {rank} is not part of this batch")
        if rank in parsed:
            raise ValueError(f"rerank score rank {rank} is duplicated")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            raise ValueError("rerank score must be a finite number")
        parsed[rank] = float(score)
    if len(parsed) != len(expected):
        raise ValueError("rerank response must score every candidate in the batch exactly once")
    return parsed


__all__ = ["HTTP_RERANK_POLICY_ID", "HTTPMemoryReranker"]
