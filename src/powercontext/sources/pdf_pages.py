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

"""PDF page Source: one physical page of an offline PDF captured as evidence."""

from __future__ import annotations

import hashlib
import io
import math
import re
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict

from powercontext.limits import MAX_SOURCE_ID_LENGTH
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

PDF_PAGE_SOURCE_NAME = "pdf-page"


class PDFDocumentInput(BaseModel):
    """Offline PDF bytes plus the caller-chosen document identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    content: bytes


class PDFPageInput(BaseModel):
    """One physical PDF page with its extracted text layer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: str
    document_sha256: str
    page_number: int
    text: str


class PDFPageSource(Source):
    """Captured PDF page bound to an exact document revision."""

    source_id: str
    document_id: str
    document_sha256: str
    page_number: int
    text: str


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _validate_document_id(document_id: Any) -> str:
    if not isinstance(document_id, str) or not document_id.strip():
        raise ValueError("document_id must be a non-empty, non-blank string")
    return document_id


def pdf_page_source_id(document_id: str, document_sha256: str, page_number: int) -> str:
    """Build the bounded identity of one page of one exact document revision."""

    _validate_document_id(document_id)
    if not isinstance(document_sha256, str) or _SHA256.fullmatch(document_sha256) is None:
        raise ValueError("document_sha256 must be a lowercase hex SHA-256 digest")
    if isinstance(page_number, bool) or not isinstance(page_number, int) or page_number < 1:
        raise ValueError("page_number must be a positive integer")
    identity = f"pdf:{quote(document_id, safe='')}:{document_sha256}:{page_number}"
    if len(identity) > MAX_SOURCE_ID_LENGTH:
        raise ValueError(f"pdf page source_id must not exceed {MAX_SOURCE_ID_LENGTH} characters")
    return identity


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every physical page in order; fail atomically on unreadable input."""

    document_id = _validate_document_id(value.document_id)
    content = value.content
    if not isinstance(content, bytes) or not content:
        raise ValueError("PDF content must be non-empty bytes")
    digest = hashlib.sha256(content).hexdigest()
    try:
        texts = _extract_page_texts(content)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("invalid or unreadable PDF document") from error
    pages = tuple(
        PDFPageInput(document_id=document_id, document_sha256=digest, page_number=number, text=text)
        for number, text in enumerate(texts, start=1)
    )
    for page in pages:
        pdf_page_source_id(page.document_id, page.document_sha256, page.page_number)
    return pages


def _extract_page_texts(content: bytes) -> list[str]:
    from pypdf import PdfReader

    if not content.lstrip()[:5].startswith(b"%PDF-"):
        raise ValueError("content is not a PDF document")
    reader = PdfReader(io.BytesIO(content), strict=False)
    if reader.is_encrypted:
        if not reader.decrypt(""):
            raise ValueError("PDF requires a non-empty password")
    count = len(reader.pages)
    if count < 1:
        raise ValueError("PDF document has no pages")
    texts: list[str] = []
    for index in range(count):
        text = reader.pages[index].extract_text()
        texts.append(text if isinstance(text, str) else "")
    return texts


class PDFPageSourceAdapter:
    """Materialize and read captured PDF page Sources."""

    input_class = PDFPageInput
    name = PDF_PAGE_SOURCE_NAME
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        source_id = pdf_page_source_id(value.document_id, value.document_sha256, value.page_number)
        return PDFPageSource(
            name=source_id,
            definition_version="1",
            materialization=SourceMaterialization.CAPTURED,
            source_id=source_id,
            document_id=value.document_id,
            document_sha256=value.document_sha256,
            page_number=value.page_number,
            text=value.text,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return PDFPageInput(
            document_id=source.document_id,
            document_sha256=source.document_sha256,
            page_number=source.page_number,
            text=source.text,
        )


class PDFPageTextEvidenceProjection:
    """Project a PDF page as document-labelled text evidence."""

    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_NAME,
            source_id=source.name,
            content=f"{source.document_id}\n{source.text}",
            metadata={
                "document_id": source.document_id,
                "document_sha256": source.document_sha256,
                "page_number": source.page_number,
            },
        )


PDF_PAGE_SOURCE_ADAPTER = PDFPageSourceAdapter()
PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(
    PDF_PAGE_SOURCE_ADAPTER,
    version="1",
    projections=(PDFPageTextEvidenceProjection(),),
)


# --------------------------------------------------------------------------- search

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    """a an the and or for of to in on at by from with as is are was were be been being what which who whom
    how why when where do does did can could should would will shall may might must i we you it its our your
    their they them this that these those please any all some has have had having there here than then so
    if not no into about over under between per each based using use used also only very much many more most
    give show tell list provide report reported reporting according primarily mainly currently ongoing
    as of fy year years fiscal company companies company's s""".split()
)
# Generic financial-report vocabulary expansions (no document- or answer-specific knowledge).
_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "capex": ("capital", "spending", "expenditures", "property", "plant", "equipment"),
    "capital": ("expenditures", "spending"),
    "ebitda": ("operating", "profit", "income", "depreciation", "amortization"),
    "ebit": ("operating", "profit", "income"),
    "margin": ("revenue", "operating", "profit"),
    "margins": ("revenue", "operating", "profit"),
    "revenue": ("revenues", "sales", "net"),
    "revenues": ("revenue", "sales", "net"),
    "sales": ("revenue", "revenues"),
    "geographies": ("segments", "geographic", "regions", "international", "countries"),
    "geography": ("segments", "geographic", "regions"),
    "geographic": ("segments", "regions"),
    "regions": ("segments", "geographic"),
    "legal": ("litigation", "proceedings", "lawsuits", "claims", "contingencies"),
    "lawsuits": ("litigation", "proceedings", "legal"),
    "litigation": ("legal", "proceedings", "lawsuits"),
    "battles": ("litigation", "proceedings"),
    "restructuring": ("impairment", "charges", "severance"),
    "cash": ("flows",),
    "flow": ("flows", "cash"),
    "debt": ("borrowings", "notes", "obligations"),
    "dividend": ("dividends", "payout"),
    "dividends": ("dividend", "payout"),
    "inventory": ("inventories",),
    "inventories": ("inventory",),
    "assets": ("total", "balance", "sheet"),
    "liabilities": ("total", "balance", "sheet"),
    "eps": ("earnings", "per", "share", "diluted"),
    "earnings": ("income", "net"),
    "profit": ("income",),
    "cogs": ("cost", "sold"),
    "acquisition": ("acquisitions", "acquired"),
    "acquisitions": ("acquisition", "acquired"),
    "employees": ("employee", "workforce", "associates"),
    "segment": ("segments",),
    "segments": ("segment",),
    "expense": ("expenses",),
    "expenses": ("expense",),
    "interest": ("expense",),
    "tax": ("taxes", "income", "effective", "rate"),
    "taxes": ("tax", "income"),
    "share": ("shares",),
    "shares": ("share", "repurchases"),
    "buyback": ("repurchases", "repurchase", "shares"),
    "buybacks": ("repurchases", "repurchase", "shares"),
}
_STATEMENT_HINTS = {
    "income": ("consolidated statement of income", "consolidated statements of income", "statement of operations"),
    "cash": ("consolidated statement of cash flows", "consolidated statements of cash flows"),
    "balance": ("consolidated balance sheet", "consolidated balance sheets"),
}
_STATEMENT_TRIGGERS = {
    "income": {"income", "revenue", "revenues", "ebitda", "ebit", "margin", "margins", "eps", "earnings", "profit",
               "cogs", "sales", "operating"},
    "cash": {"cash", "capex", "ebitda", "depreciation", "amortization", "dividends", "flow", "flows", "buyback"},
    "balance": {"assets", "liabilities", "equity", "inventory", "inventories", "debt", "balance", "receivables",
                "working"},
}
_EXPLICIT_STATEMENTS = {
    "income": ("income statement", "statement of income", "statements of income", "p&l", "profit and loss",
               "statement of operations", "statements of operations", "statement of earnings"),
    "cash": ("cash flow statement", "statement of cash flows", "statements of cash flows", "cash flows statement"),
    "balance": ("balance sheet", "statement of financial position"),
}
_PER_QUERY_LIMIT = 50
_MAX_TERM_QUERIES = 24
_BM25_K1 = 1.2
_BM25_B = 0.75


def _terms(text: str) -> list[str]:
    return _WORD.findall(text.casefold())


def _question_terms(question: str) -> tuple[list[str], dict[str, float]]:
    raw = [term for term in _terms(question) if term not in _STOPWORDS and (len(term) > 1 or term.isdigit())]
    # fy2022 -> 2022 so fiscal years match page text.
    normalized: list[str] = []
    for term in raw:
        match = re.fullmatch(r"(?:fy|q[1-4])(\d{2,4})", term)
        normalized.append(match.group(1) if match else term)
    weights: dict[str, float] = {}
    for term in normalized:
        weights[term] = max(weights.get(term, 0.0), 1.0)
        if term.endswith("s") and len(term) > 3:
            weights.setdefault(term[:-1], 0.6)
        for extra in _EXPANSIONS.get(term, ()):
            weights.setdefault(extra, 0.45)
    return list(dict.fromkeys(normalized)), weights


def _page_key(text: str, fallback: str) -> str:
    # The persisted entry text is "<document_id>\n<page text>"; group candidates by exact text so a
    # page duplicated across entries is never cited twice.
    return text or fallback


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[Any, ...]:
    """Return at most ``limit`` citations of distinct PDF pages relevant to ``question``.

    Uses only the public Memory search API: several lexical searches (the full question plus one
    per evidence term) build a candidate pool, which is reranked with a local BM25 estimate whose
    document frequencies come from the per-term searches.
    """

    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import SearchMemoryRequest

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not isinstance(question, str) or not question.strip():
        return ()

    core_terms, weights = _question_terms(question)
    candidates: dict[str, tuple[Any, str]] = {}
    fused: dict[str, float] = {}
    document_frequency: dict[str, int] = {}

    async def run(query: str, weight: float, term: str | None = None) -> None:
        try:
            result = await memory.search(SearchMemoryRequest(query=query, limit=_PER_QUERY_LIMIT, mode="fts"))
        except Exception:
            return
        hits = tuple(getattr(result, "hits", ()) or ())
        if term is not None:
            document_frequency[term] = len(hits)
        for rank, hit in enumerate(hits):
            key = _page_key(hit.text, f"{hit.entry_id}")
            candidates.setdefault(key, (hit, hit.text))
            fused[key] = fused.get(key, 0.0) + weight / (rank + 10)

    await run(question, 2.0)
    content_query = " ".join(core_terms)
    if content_query and content_query != question:
        await run(content_query, 2.0)
    ranked_terms = sorted(weights, key=lambda term: (-weights[term], term))[:_MAX_TERM_QUERIES]
    for term in ranked_terms:
        await run(term, weights[term], term)
    for index in range(len(core_terms) - 1):
        await run(f"{core_terms[index]} {core_terms[index + 1]}", 1.0)

    if not candidates:
        return ()

    # Local BM25 over the pooled candidates. A term whose single-term search saturated the page
    # limit is common, so its df is extrapolated upward.
    analyzed = {key: _terms(text) for key, (_hit, text) in candidates.items()}
    average_length = sum(len(tokens) for tokens in analyzed.values()) / max(1, len(analyzed)) or 1.0
    collection = max(len(candidates), 4 * _PER_QUERY_LIMIT)

    def idf(term: str) -> float:
        df = document_frequency.get(term)
        if df is None:
            df = sum(1 for tokens in analyzed.values() if term in tokens)
        if df >= _PER_QUERY_LIMIT:
            df = 3 * _PER_QUERY_LIMIT
        return math.log(1.0 + (collection - df + 0.5) / (df + 0.5))

    idfs = {term: idf(term) for term in weights}
    lowered = question.casefold()
    question_words = set(_terms(lowered))
    statement_phrases = [
        phrase
        for kind, phrases in _STATEMENT_HINTS.items()
        if question_words & _STATEMENT_TRIGGERS[kind]
        for phrase in phrases
    ]
    explicit_phrases = [
        phrase
        for kind, mentions in _EXPLICIT_STATEMENTS.items()
        if any(mention in lowered for mention in mentions)
        for phrase in _STATEMENT_HINTS[kind]
    ]
    scores: dict[str, float] = {}
    for key, tokens in analyzed.items():
        counts: dict[str, int] = {}
        for token in tokens:
            if token in weights:
                counts[token] = counts.get(token, 0) + 1
        length_norm = 1 - _BM25_B + _BM25_B * len(tokens) / average_length
        bm25 = 0.0
        for term, tf in counts.items():
            bm25 += weights[term] * idfs[term] * tf * (_BM25_K1 + 1) / (tf + _BM25_K1 * length_norm)
        coverage = sum(1 for term in core_terms if term in counts) / max(1, len(core_terms))
        text = candidates[key][1].casefold()
        header = text[:400]
        document_line = text.split("\n", 1)[0]
        bonus = 0.0
        if statement_phrases and any(phrase in header for phrase in statement_phrases):
            bonus += 2.5
        if explicit_phrases and any(phrase in header for phrase in explicit_phrases):
            bonus += 12.0
        if any(term in document_line for term in core_terms if len(term) > 2):
            bonus += 0.5
        scores[key] = bm25 * (0.5 + coverage) + bonus + 20.0 * fused.get(key, 0.0)

    ordered = sorted(scores, key=lambda key: (-scores[key], key))[:limit]
    citations: list[Any] = []
    for key in ordered:
        hit = candidates[key][0]
        citations.append(
            MemoryCitation(memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id)
        )
    return tuple(citations)


__all__ = [
    "PDF_PAGE_SOURCE_ADAPTER",
    "PDF_PAGE_SOURCE_DEFINITION",
    "PDF_PAGE_SOURCE_NAME",
    "PDFDocumentInput",
    "PDFPageInput",
    "PDFPageSource",
    "PDFPageSourceAdapter",
    "PDFPageTextEvidenceProjection",
    "parse_pdf_document",
    "pdf_page_source_id",
    "search_pdf_pages",
]
