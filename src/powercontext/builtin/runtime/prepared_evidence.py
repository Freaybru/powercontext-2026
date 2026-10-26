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

"""Query-driven selection of verbatim evidence fragments from Memory candidates.

Evidence selection is an opt-in mode of the prepared-context builder. Instead of delivering
whole Memory entries (truncated from the front when they do not fit), it picks the passages
of each candidate entry that best answer the query and delivers them as exact,
half-open character ranges of the original entry text. Every byte of the rendered output is
accounted for, so the final context never exceeds the request budget.

The selector is pure and deterministic: no I/O, no models, only lexical relevance (BM25 over
sentences, with a document-level prior) and exact UTF-8 measurement of the rendered envelope.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_BEGIN_MARKER = "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1"
_END_MARKER = "END_POWERCONTEXT_PREPARED_CONTEXT_V1"

_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_END = re.compile(r"[.!?。！？;；]+[\"')\]]*(?=\s|$)|[。！？；]+|\n\s*\n")
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff]")
_MAX_SENTENCE_CHARS = 600
_BM25_K1 = 1.2
_BM25_B = 0.75
_RELATIVE_SEED_THRESHOLD = 0.5
_DOCUMENT_PRIOR_WEIGHT = 0.5
_MIN_PARTIAL_CHARS = 24
_LEADING_SENTENCE_WEIGHT = 0.1
_POSITION_WEIGHT = 1.0
_RETRIEVAL_PRIOR_DECAY = 0.4
_SENTENCE_PRIOR_FLOOR = 0.25

_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because been before being
    below between both but by can could did do does doing down during each few for from further had
    has have having he her here hers him his how i if in into is it its itself just may me might more
    most must my no nor not now of off on once only or other our ours out over own same she should so
    some such than that the their theirs them then there these they this those through to too under
    until up very was we were what when where which while who whom why will with would you your yours
    """.split()
)


@dataclass(frozen=True)
class EvidenceCandidate:
    """One Memory entry offered to the selector, with the address it will be cited by."""

    memory: ArtifactAddress
    entry_id: str
    entry_version_id: str
    text: str


@dataclass(frozen=True)
class EvidenceSpan:
    """One selected half-open character range ``[start, end)`` of a candidate's text."""

    candidate: EvidenceCandidate
    start: int
    end: int

    @property
    def text(self) -> str:
        return self.candidate.text[self.start : self.end]


@dataclass(frozen=True)
class _Sentence:
    document: int
    index: int
    start: int
    end: int
    terms: Counter[str]
    length: int


def select_evidence(
    *, query: str, max_bytes: int, candidates: Sequence[EvidenceCandidate]
) -> tuple[str | None, tuple[EvidenceSpan, ...]]:
    """Select query-relevant verbatim spans and render them within ``max_bytes``.

    Returns ``(content, spans)``; ``content`` is ``None`` exactly when nothing fits or nothing
    relevant was offered. ``spans`` are in the rendered order.
    """

    documents = [candidate for candidate in candidates if candidate.text.strip()]
    if not documents:
        return None, ()
    query_terms = _query_terms(query)
    sentences = [_split(index, document.text) for index, document in enumerate(documents)]
    flat = [sentence for document in sentences for sentence in document]
    if not flat:
        return None, ()
    sentence_scores = _bm25(query_terms, flat)
    document_scores = _bm25(
        query_terms,
        [
            _Sentence(
                document=index,
                index=0,
                start=0,
                end=len(document.text),
                terms=Counter(term for sentence in sentences[index] for term in sentence.terms.elements()),
                length=sum(sentence.length for sentence in sentences[index]),
            )
            for index, document in enumerate(documents)
        ],
    )
    # Candidates arrive in retrieval order; that order is the strongest document-level signal
    # available here, so it leads, and lexical document relevance only refines it.
    top_document_score = max(document_scores, default=0.0)
    priors = [
        _RETRIEVAL_PRIOR_DECAY**rank
        * (0.5 + 0.5 * (document_scores[rank] / top_document_score if top_document_score > 0 else 0.0))
        for rank in range(len(documents))
    ]
    scores: dict[tuple[int, int], float] = {}
    for sentence, score in zip(flat, sentence_scores, strict=True):
        prior = priors[sentence.document]
        count = len(sentences[sentence.document])
        position = sentence.index / (count - 1) if count > 1 else 0.0
        if sentence.index == 0 and count > 1:
            score *= _LEADING_SENTENCE_WEIGHT
        scores[(sentence.document, sentence.index)] = (
            score * (_SENTENCE_PRIOR_FLOOR + (1.0 - _SENTENCE_PRIOR_FLOOR) * prior)
            + _DOCUMENT_PRIOR_WEIGHT * prior * (1.0 if score > 0 else 0.25)
            + _POSITION_WEIGHT * prior * position
        )
    document_rank = sorted(range(len(documents)), key=lambda index: (-priors[index], index))

    selector = _Selector(documents=documents, sentences=sentences, document_rank=document_rank, max_bytes=max_bytes)
    if selector.render_bytes() > max_bytes:
        return None, ()

    ranked = sorted(scores.items(), key=lambda item: (-item[1], document_rank.index(item[0][0]), item[0][1]))
    best = ranked[0][1] if ranked else 0.0
    if best <= 0:
        # Nothing in the query matches: deliver the leading passage of the first candidate.
        ranked = [((document_rank[0], 0), 1.0)]
        best = 1.0
    seeds = [key for key, score in ranked if score >= best * _RELATIVE_SEED_THRESHOLD]
    for key in seeds:
        selector.try_add(*key)
    # Grow every selected passage with its neighbours, best document first, so the budget
    # buys the context around each match rather than isolated sentences.
    progress = True
    while progress:
        progress = False
        for document in document_rank:
            chosen = sorted(selector.chosen.get(document, ()))
            if not chosen:
                continue
            for neighbour in _neighbours(chosen, len(sentences[document])):
                if selector.try_add(document, neighbour):
                    progress = True
                    break
            if progress:
                break
    # Spend what is left of the budget on lower-ranked matching sentences, then on a partial
    # extension of the strongest passage.
    for key, score in ranked:
        if score > 0:
            selector.try_add(*key)
    selector.extend_partially()
    spans = selector.spans()
    if not spans:
        return None, ()
    content = render_evidence(spans)
    if len(content.encode("utf-8")) > max_bytes:
        raise PreparedContextInvariantError("output-budget")
    return content, spans


def render_evidence(spans: Sequence[EvidenceSpan]) -> str:
    envelope = {
        "trust": "untrusted_history",
        "items": [
            {
                "citation": {
                    "scope_id": span.candidate.memory.scope_id,
                    "memory_ref": span.candidate.memory.artifact.model_dump(mode="json"),
                    "entry_id": span.candidate.entry_id,
                    "entry_version_id": span.candidate.entry_version_id,
                },
                "content": span.text,
                "start": span.start,
                "end": span.end,
            }
            for span in spans
        ],
    }
    encoded = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))
    return "\n\n".join((TRUST_POLICY, f"{_BEGIN_MARKER}\n{encoded}\n{_END_MARKER}"))


class _Selector:
    def __init__(
        self,
        *,
        documents: Sequence[EvidenceCandidate],
        sentences: Sequence[Sequence[_Sentence]],
        document_rank: Sequence[int],
        max_bytes: int,
    ) -> None:
        self.documents = documents
        self.sentences = sentences
        self.document_rank = list(document_rank)
        self.max_bytes = max_bytes
        self.chosen: dict[int, set[int]] = {}
        self.extension: tuple[int, int, int] | None = None  # (document, span start, span end)
        self._bridge_cache: dict[int, int] = {}

    def try_add(self, document: int, index: int) -> bool:
        chosen = self.chosen.setdefault(document, set())
        if index in chosen:
            return False
        chosen.add(index)
        if self.render_bytes() <= self.max_bytes:
            return True
        chosen.discard(index)
        return False

    def spans(self) -> tuple[EvidenceSpan, ...]:
        spans: list[EvidenceSpan] = []
        for document in self.document_rank:
            indices = sorted(self.chosen.get(document, ()))
            if not indices:
                continue
            sentences = self.sentences[document]
            candidate = self.documents[document]
            run_start = indices[0]
            previous = indices[0]
            for index in [*indices[1:], None]:
                if index is not None and (
                    index == previous + 1 or _gap_bytes(candidate.text, sentences[previous].end, sentences[index].start)
                    <= self._bridge_bytes(candidate)
                ):
                    previous = index
                    continue
                start, end = sentences[run_start].start, sentences[previous].end
                spans.append(EvidenceSpan(candidate=candidate, start=start, end=end))
                if index is not None:
                    run_start = previous = index
        if self.extension is not None:
            document, start, end = self.extension
            spans = [
                EvidenceSpan(candidate=span.candidate, start=start, end=end)
                if span.candidate is self.documents[document] and span.start == start
                else span
                for span in spans
            ]
            spans = _merge_touching(spans)
        return tuple(spans)

    def _bridge_bytes(self, candidate: EvidenceCandidate) -> int:
        """Bytes one extra item costs; a gap cheaper than that is bridged instead of split."""

        cached = self._bridge_cache.get(id(candidate))
        if cached is None:
            one = len(render_evidence((EvidenceSpan(candidate=candidate, start=0, end=1),)).encode("utf-8"))
            two = len(
                render_evidence(
                    (EvidenceSpan(candidate=candidate, start=0, end=1), EvidenceSpan(candidate=candidate, start=0, end=1))
                ).encode("utf-8")
            )
            cached = two - one
            self._bridge_cache[id(candidate)] = cached
        return cached

    def render_bytes(self) -> int:
        return len(render_evidence(self.spans()).encode("utf-8"))

    def extend_partially(self) -> None:
        """Stretch the strongest passage into the next unselected sentence as far as fits."""

        for document in self.document_rank:
            indices = sorted(self.chosen.get(document, ()))
            if not indices:
                continue
            spans = [span for span in self.spans() if span.candidate is self.documents[document]]
            text = self.documents[document].text
            for span in spans:
                limit = _next_boundary(self.sentences[document], span.end, len(text))
                if limit <= span.end:
                    continue
                best = self._longest_fit(document, span.start, span.end, limit)
                if best is not None:
                    self.extension = (document, span.start, best)
                    return
            return

    def _longest_fit(self, document: int, start: int, end: int, limit: int) -> int | None:
        text = self.documents[document].text
        low, high, best = end + 1, limit, None
        while low <= high:
            middle = (low + high) // 2
            self.extension = (document, start, middle)
            if self.render_bytes() <= self.max_bytes:
                best, low = middle, middle + 1
            else:
                high = middle - 1
        self.extension = None
        if best is None:
            return None
        cut = best
        while cut > end and not text[cut - 1].isspace() and cut < len(text) and not text[cut].isspace():
            cut -= 1
        cut = len(text[:cut].rstrip())
        if cut - end < _MIN_PARTIAL_CHARS:
            return None
        return cut


def _merge_touching(spans: list[EvidenceSpan]) -> list[EvidenceSpan]:
    merged: list[EvidenceSpan] = []
    for span in spans:
        if merged and merged[-1].candidate is span.candidate and span.start <= merged[-1].end:
            previous = merged.pop()
            span = EvidenceSpan(candidate=span.candidate, start=previous.start, end=max(previous.end, span.end))
        merged.append(span)
    return merged


def _gap_bytes(text: str, start: int, end: int) -> int:
    return len(json.dumps(text[start:end], ensure_ascii=False).encode("utf-8")) - 2


def _next_boundary(sentences: Sequence[_Sentence], end: int, length: int) -> int:
    for sentence in sentences:
        if sentence.start >= end:
            return sentence.end
    return length if end < length else end


def _neighbours(chosen: Sequence[int], count: int) -> list[int]:
    picked = set(chosen)
    after = [index + 1 for index in chosen if index + 1 < count and index + 1 not in picked]
    before = [index - 1 for index in chosen if index - 1 >= 0 and index - 1 not in picked]
    ordered: list[int] = []
    for index in [*after, *before]:
        if index not in ordered:
            ordered.append(index)
    return ordered


def _split(document: int, text: str) -> list[_Sentence]:
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for match in _SENTENCE_END.finditer(text):
        end = match.end()
        bounds.extend(_bounded(text, cursor, end))
        cursor = end
    bounds.extend(_bounded(text, cursor, len(text)))
    sentences: list[_Sentence] = []
    for start, end in bounds:
        terms = _terms(text[start:end])
        sentences.append(
            _Sentence(
                document=document,
                index=len(sentences),
                start=start,
                end=end,
                terms=Counter(terms),
                length=max(1, len(terms)),
            )
        )
    return sentences


def _bounded(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Trim whitespace from ``text[start:end]`` and split overly long pieces at spaces."""

    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    pieces: list[tuple[int, int]] = []
    while end - start > _MAX_SENTENCE_CHARS:
        cut = text.rfind(" ", start + _MAX_SENTENCE_CHARS // 2, start + _MAX_SENTENCE_CHARS)
        if cut <= start:
            cut = start + _MAX_SENTENCE_CHARS
        piece_end = cut
        while piece_end > start and text[piece_end - 1].isspace():
            piece_end -= 1
        pieces.append((start, piece_end))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if end > start:
        pieces.append((start, end))
    return pieces


def _terms(text: str) -> list[str]:
    terms: list[str] = []
    for word in _WORD.findall(text.lower()):
        if _CJK.search(word):
            characters = [character for character in word if _CJK.match(character)]
            terms.extend(characters)
            terms.extend(a + b for a, b in zip(characters, characters[1:], strict=False))
            rest = _CJK.sub(" ", word).split()
            terms.extend(_stem(part) for part in rest if part not in _STOPWORDS)
            continue
        if word in _STOPWORDS:
            continue
        terms.append(_stem(word))
    return terms


def _query_terms(query: str) -> set[str]:
    return set(_terms(query))


def _stem(word: str) -> str:
    if word.isdigit() or len(word) <= 3:
        return word
    for suffix in ("ational", "ization", "iveness", "fulness", "ousness", "ations", "ation", "ities"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    for suffix in ("ingly", "edly", "ing", "ies", "ied", "ity", "ive", "ed", "es", "ly", "al", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            if suffix == "s" and word.endswith("ss"):
                return word
            return word[: -len(suffix)]
    return word


def _bm25(query_terms: set[str], units: Sequence[_Sentence]) -> list[float]:
    if not units or not query_terms:
        return [0.0 for _ in units]
    count = len(units)
    average = sum(unit.length for unit in units) / count
    frequency = Counter(term for unit in units for term in set(unit.terms) if term in query_terms)
    idf = {term: math.log(1.0 + (count - df + 0.5) / (df + 0.5)) for term, df in frequency.items()}
    scores: list[float] = []
    for unit in units:
        score = 0.0
        norm = _BM25_K1 * (1.0 - _BM25_B + _BM25_B * unit.length / average)
        for term, weight in idf.items():
            tf = unit.terms.get(term, 0)
            if tf:
                score += weight * tf * (_BM25_K1 + 1.0) / (tf + norm)
        scores.append(score)
    return scores
