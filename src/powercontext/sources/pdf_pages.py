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
import logging
import math
import re
import threading
import unicodedata
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
        for number, text in enumerate((_fit_page_text(document_id, raw) for raw in texts), start=1)
    )
    for page in pages:
        pdf_page_source_id(page.document_id, page.document_sha256, page.page_number)
    return pages


# --------------------------------------------------------------------------- page text fitting

# The product stores one Memory entry per page and bounds an entry body at 8192 UTF-8 bytes after
# NFC normalization and stripping. A dense page (curly quotes and dashes take three bytes each)
# can exceed that, so page text is compacted without changing its [a-z]+|\d+ word content.
_MAX_ENTRY_BYTES = 8192
_WORD_CHAR = re.compile(r"[a-z]|\d")
_TYPOGRAPHY = str.maketrans(
    {
        "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'", "\u2032": "'",
        "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u2033": '"',
        "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-", "\u2015": "-",
        "\u2212": "-", "\u2022": "*", "\u25cf": "*", "\u25aa": "*", "\u00b7": "*", "\u2026": "...",
        "\u00a0": " ", "\u2002": " ", "\u2003": " ", "\u2009": " ", "\u200a": " ", "\u202f": " ",
        "\u200b": " ", "\u00ad": " ", "\ufeff": " ",
    }
)
_HORIZONTAL_SPACE = re.compile(r"[^\S\n]+")
_BLANK_LINES = re.compile(r"\n(?: ?\n)+")
_REPEATED_PUNCTUATION = re.compile(r"([^\w\s])\1{2,}|(_)\2{2,}")
_ALPHANUMERIC_RUN = re.compile(r"[^\W_]+", re.UNICODE)


def _is_word_free(character: str) -> bool:
    return _WORD_CHAR.search(character.casefold()) is None and _WORD_CHAR.search(character.lower()) is None


def _entry_bytes(document_id: str, text: str) -> int:
    return len(unicodedata.normalize("NFC", f"{document_id}\n{text}").strip().encode("utf-8"))


def _clean_page_text(text: str) -> str:
    """NFC text without control characters or lone surrogates; word content is unchanged."""

    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "".join(
        " " if (unicodedata.category(c) in {"Cc", "Cs", "Co", "Cn"} and c not in "\n\t") else c for c in text
    )


def _collapse_layout(text: str) -> str:
    text = _HORIZONTAL_SPACE.sub(" ", text)
    text = "\n".join(line.strip(" ") for line in text.split("\n"))
    return _BLANK_LINES.sub("\n", text).strip()


def _drop_word_free_symbols(text: str) -> str:
    # Symbols that carry no [a-z0-9] content become one separator so neighbouring words stay apart.
    keep = ".,%$()-/&:;'\"*"
    return "".join(
        c if (c.isspace() or c in keep or not _is_word_free(c) or (c.isascii() and c.isalnum())) else " " for c in text
    )


def _words_only(text: str) -> str:
    return "".join(c if not _is_word_free(c) else " " for c in text)


def _fit_page_text(document_id: str, text: str) -> str:
    """Return the page text, compacted only as far as needed to fit one Memory entry."""

    text = _clean_page_text(text)
    if _entry_bytes(document_id, text) <= _MAX_ENTRY_BYTES:
        return text
    steps = (
        _collapse_layout,
        lambda value: _collapse_layout(value.translate(_TYPOGRAPHY)),
        lambda value: _collapse_layout(_REPEATED_PUNCTUATION.sub(lambda m: m.group(1) or m.group(2), value)),
        lambda value: _collapse_layout(_drop_word_free_symbols(value)),
        lambda value: _collapse_layout(_words_only(value)),
    )
    for step in steps:
        text = step(text)
        if _entry_bytes(document_id, text) <= _MAX_ENTRY_BYTES:
            return text
    # Last resort for a page whose words alone exceed an entry: keep the leading words.
    budget = _MAX_ENTRY_BYTES - (_entry_bytes(document_id, "a") - 1)
    kept = text.encode("utf-8")[: max(0, budget)].decode("utf-8", errors="ignore")
    if len(kept) < len(text) and not text[len(kept)].isspace() and any(c.isspace() for c in kept):
        kept = kept.rsplit(None, 1)[0]
    return kept.rstrip()


# pypdf recovers from a damaged Flate stream by keeping only the bytes it could inflate and
# logging the zlib error. Such a page would silently lose text, so it fails the whole document.
_DAMAGED_STREAM = re.compile(r"Error -?\d+ while (?:decompressing|flushing)")


class _DamagedStreamMonitor(logging.Handler):
    """Record pypdf stream-recovery warnings raised by the calling thread."""

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self._thread = threading.get_ident()
        self.damaged = False

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self._thread:
            return
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed third-party log record
            return
        if _DAMAGED_STREAM.search(message):
            self.damaged = True


def _extract_page_texts(content: bytes) -> list[str]:
    # PDF readers accept a header within the first 1024 bytes.
    if b"%PDF-" not in content[:1024]:
        raise ValueError("content is not a PDF document")
    logger = logging.getLogger("pypdf")
    monitor = _DamagedStreamMonitor()
    previous_level = logger.level
    if logger.getEffectiveLevel() > logging.WARNING:
        logger.setLevel(logging.WARNING)
    logger.addHandler(monitor)
    try:
        texts = _read_page_texts(content)
    finally:
        logger.removeHandler(monitor)
        logger.setLevel(previous_level)
    if monitor.damaged:
        raise ValueError("PDF contains a damaged content stream")
    return texts


def _read_page_texts(content: bytes) -> list[str]:
    from pypdf import PdfReader

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
    as of fy year years fiscal company companies company's s
    useful metric metrics state explain answer answers question following assume analyst public equities
    judgement judgment judgements judgments information basing base drawing conclusions stated
    calculate calculation compute computed defined define definition formula approximation reasonable exact
    possible round rounded decimal places place units unit usd millions billions help need want like
    roughly approximately respectively shown""".split()
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
               "cogs", "sales", "operating", "gross", "growth", "turnover", "dpo", "inventory", "sold"},
    "cash": {"cash", "capex", "ebitda", "depreciation", "amortization", "dividends", "flow", "flows", "buyback",
             "capital", "intensive", "free", "cashflow", "conversion", "dividend", "payout"},
    "balance": {"assets", "liabilities", "equity", "inventory", "inventories", "debt", "balance", "receivables",
                "working", "quick", "liquidity", "current", "ppne", "ppe", "payable", "payables", "turnover", "dpo",
                "dso", "solvency", "leverage"},
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


_STEM_LENGTH = 8


def _stem(term: str) -> str:
    """Light stemming for ranking: long words share a fixed-length prefix (customers ~ customer)."""

    return term[:_STEM_LENGTH] if len(term) > _STEM_LENGTH and term.isalpha() else term


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


_DOCUMENT_KIND_PARTS = frozenset(
    {"10k", "10q", "8k", "20f", "40f", "6k", "earnings", "dated", "annual", "report", "quarterly", "transcript"}
)
_DOCUMENT_FETCH_LIMIT = 5000
_DOCUMENT_FUSED_WEIGHT = 0.0
_UNNAMED_DOCUMENT_LIMIT = 8
_YEAR = re.compile(r"(?:19|20)\d{2}")


def _document_line(text: str) -> str:
    return text.split("\n", 1)[0]


def _document_parts(document_id: str) -> list[str]:
    return [part for part in re.split(r"[^0-9a-z]+", document_id.casefold()) if part]


def _document_years(parts: list[str]) -> set[str]:
    return {match for part in parts for match in _YEAR.findall(part)}


def _question_years(words: list[str]) -> set[str]:
    years: set[str] = set()
    for word in words:
        match = re.fullmatch(r"(?:fy|cy|q[1-4]|h[12])?((?:19|20)\d{2}|\d{2})", word)
        if match is None:
            continue
        value = match.group(1)
        if len(value) == 2:
            if word == value:
                continue  # a bare two-digit number is not a year
            value = "20" + value
        years.add(value)
    return years


def _document_match(document_id: str, words: list[str], phrases: set[str]) -> tuple[int, int]:
    """Score how explicitly a question names a document: (company match, period match)."""

    parts = _document_parts(document_id)
    company = [
        part
        for part in parts
        if part not in _DOCUMENT_KIND_PARTS and not part.isdigit() and re.fullmatch(r"(?:19|20)\d{2}q[1-4]", part) is None
    ]
    company_score = 0
    if company:
        name = "".join(company)
        if name in phrases:
            company_score = 3
        elif len(company[0]) >= 3 and company[0] in phrases:
            company_score = 2
        elif any(
            len(phrase) >= 4 and name.startswith(phrase) and 5 * len(phrase) >= 3 * len(name) for phrase in phrases
        ) or any(len(word) >= 3 and word not in _STOPWORDS and name.startswith(word) for word in words):
            company_score = 1
    period_score = 0
    question_years = _question_years(words)
    if question_years and question_years & _document_years(parts):
        period_score += 1
    lowered = set(words)
    quarterly = bool({"q1", "q2", "q3", "q4", "quarter", "quarterly", "10q"} & lowered)
    if quarterly == any(re.search(r"q[1-4]$", part) or part == "10q" for part in parts):
        period_score += 1
    return company_score, period_score


def _company_name(document_id: str) -> str:
    return "".join(part for part in _document_parts(document_id) if part.isalpha() and part not in _DOCUMENT_KIND_PARTS)


def _focus_terms(
    core_terms: list[str], weights: dict[str, float], targets: list[str]
) -> tuple[list[str], dict[str, float]]:
    """Drop words that only name the targeted document's company: every page of it shares them."""

    names = [name for name in (_company_name(target) for target in targets) if name]

    def naming(term: str) -> bool:
        return term.isalpha() and len(term) >= 2 and term not in _EXPANSIONS and any(term in name for name in names)

    focused_core = [term for term in core_terms if not naming(term)]
    focused_weights = {term: weight for term, weight in weights.items() if not naming(term)}
    if not focused_core or not focused_weights:
        return core_terms, weights
    return focused_core, focused_weights


def _question_phrases(words: list[str]) -> set[str]:
    phrases: set[str] = set()
    for start in range(len(words)):
        for size in range(1, 6):
            if start + size <= len(words):
                phrases.add("".join(words[start : start + size]))
    return phrases


def _score_pages(
    texts: dict[str, str],
    *,
    question: str,
    core_terms: list[str],
    weights: dict[str, float],
    fused: dict[str, float],
    document_frequency: dict[str, int] | None,
    collection_size: int,
    fused_weight: float = 20.0,
) -> dict[str, float]:
    stemmed_weights: dict[str, float] = {}
    for term, weight in weights.items():
        stem = _stem(term)
        stemmed_weights[stem] = max(stemmed_weights.get(stem, 0.0), weight)
    weights = stemmed_weights
    core_terms = list(dict.fromkeys(_stem(term) for term in core_terms))
    if document_frequency is not None:
        stemmed_frequency: dict[str, int] = {}
        for term, frequency in document_frequency.items():
            stem = _stem(term)
            stemmed_frequency[stem] = max(stemmed_frequency.get(stem, 0), frequency)
        document_frequency = stemmed_frequency
    analyzed = {key: [_stem(token) for token in _terms(text)] for key, text in texts.items()}
    average_length = sum(len(tokens) for tokens in analyzed.values()) / max(1, len(analyzed)) or 1.0

    def idf(term: str) -> float:
        df = None if document_frequency is None else document_frequency.get(term)
        if df is None:
            df = sum(1 for tokens in analyzed.values() if term in tokens)
        elif df >= _PER_QUERY_LIMIT:
            df = 3 * _PER_QUERY_LIMIT
        return math.log(1.0 + (collection_size - df + 0.5) / (df + 0.5))

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
        text = texts[key].casefold()
        header = text[:400]
        bonus = 0.0
        if statement_phrases and any(phrase in header for phrase in statement_phrases):
            bonus += 2.5
        if explicit_phrases and any(phrase in header for phrase in explicit_phrases):
            bonus += 12.0
        scores[key] = bm25 * (0.5 + coverage) + bonus + fused_weight * fused.get(key, 0.0)
    return scores


async def search_pdf_pages(memory: Any, question: str, limit: int = 10) -> tuple[Any, ...]:
    """Return at most ``limit`` citations of distinct PDF pages relevant to ``question``.

    Uses only the public Memory search API. Lexical searches (the full question plus one per
    evidence term) build a candidate pool and reveal which documents are stored. When the
    question names one of those documents (company and period read from its document ID), every
    page of that document is retrieved through Memory search and ranked with BM25 statistics
    from that document; otherwise the pooled candidates are ranked. Every returned citation is
    read back so each one names exactly one page and no page is cited twice.
    """

    from powercontext.builtin.artifacts.memory.models import MemoryCitation
    from powercontext.builtin.runtime import GetMemoryEntryRequest, SearchMemoryRequest

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer")
    if not isinstance(question, str) or not question.strip():
        return ()

    core_terms, weights = _question_terms(question)
    candidates: dict[str, Any] = {}
    fused: dict[str, float] = {}
    document_frequency: dict[str, int] = {}

    async def search(query: str, limit: int) -> tuple[Any, ...]:
        try:
            result = await memory.search(SearchMemoryRequest(query=query, limit=limit, mode="fts"))
        except Exception:
            return ()
        return tuple(getattr(result, "hits", ()) or ())

    async def run(query: str, weight: float, term: str | None = None) -> None:
        hits = await search(query, _PER_QUERY_LIMIT)
        if term is not None:
            document_frequency[term] = len(hits)
        for rank, hit in enumerate(hits):
            key = hit.entry_id
            candidates.setdefault(key, hit)
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

    # Which stored document does the question name?
    words = _terms(question)
    phrases = _question_phrases(words)
    documents = sorted({_document_line(hit.text) for hit in candidates.values()})
    matches = {document: _document_match(document, words, phrases) for document in documents}
    best_company = max((score[0] for score in matches.values()), default=0)
    targets: list[str] = []
    if best_company > 0:
        named = [document for document in documents if matches[document][0] == best_company]
        best_period = max(matches[document][1] for document in named)
        targets = [document for document in named if matches[document][1] == best_period]
    else:
        # The question names no stored document: rank the pages of the documents its lexical
        # matches favour most, all of them when only a few are stored.
        votes: dict[str, float] = {}
        for key, hit in candidates.items():
            document = _document_line(hit.text)
            votes[document] = votes.get(document, 0.0) + fused.get(key, 0.0)
        targets = sorted(votes, key=lambda document: (-votes[document], document))[:_UNNAMED_DOCUMENT_LIMIT]

    pool: dict[str, Any] = {}
    if targets:
        if best_company > 0:
            core_terms, weights = _focus_terms(core_terms, weights, targets)
        for document in targets:
            hits = await search(document, _DOCUMENT_FETCH_LIMIT)
            for hit in hits:
                if _document_line(hit.text) == document:
                    pool.setdefault(hit.entry_id, hit)
        for key, hit in candidates.items():
            if _document_line(hit.text) in targets:
                pool.setdefault(key, hit)
    if pool:
        scores = _score_pages(
            {key: hit.text for key, hit in pool.items()},
            question=question,
            core_terms=core_terms,
            weights=weights,
            fused=fused,
            document_frequency=None,
            collection_size=len(pool),
            fused_weight=_DOCUMENT_FUSED_WEIGHT,
        )
    else:
        pool = candidates
        scores = _score_pages(
            {key: hit.text for key, hit in pool.items()},
            question=question,
            core_terms=core_terms,
            weights=weights,
            fused=fused,
            document_frequency=document_frequency,
            collection_size=max(len(candidates), 4 * _PER_QUERY_LIMIT),
        )

    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    citations: list[Any] = []
    cited_pages: set[str] = set()
    for key in ordered:
        if len(citations) >= limit:
            break
        hit = pool[key]
        citation = MemoryCitation(memory_ref=hit.memory_ref, entry_id=hit.entry_id, entry_version_id=hit.entry_version_id)
        try:
            entry = (await memory.get(GetMemoryEntryRequest(citation=citation))).entry
        except Exception:
            continue
        pages = {reference.source_id for reference in entry.sources}
        # An entry shared by several identical pages cannot name one page exactly.
        if len(pages) != 1 or pages & cited_pages:
            continue
        cited_pages |= pages
        citations.append(citation)
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
