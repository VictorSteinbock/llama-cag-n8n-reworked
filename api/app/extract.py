"""Text extraction for supported document types. Pure functions, no I/O."""

import io
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath, PureWindowsPath

SUPPORTED_EXTENSIONS = {".txt", ".text", ".md", ".markdown", ".html", ".htm", ".pdf"}


class UnsupportedDocumentError(ValueError):
    """Raised when a file cannot be turned into text."""


def file_suffix(file_name: str) -> str:
    # Handle both path flavours: uploads may carry Windows-style names.
    name = PureWindowsPath(file_name).name
    return PurePosixPath(name).suffix.lower()


def extract_text(file_name: str, data: bytes) -> str:
    ext = file_suffix(file_name)
    if ext not in SUPPORTED_EXTENSIONS:
        raise UnsupportedDocumentError(
            f"Unsupported file type '{ext or file_name}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if ext == ".pdf":
        text = _from_pdf(data)
    else:
        if b"\x00" in data:
            raise UnsupportedDocumentError(f"'{file_name}' looks binary, not text")
        text = data.decode("utf-8", errors="replace")
        if ext in {".html", ".htm"}:
            text = _from_html(text)

    text = text.strip()
    if not text:
        raise UnsupportedDocumentError(f"No text could be extracted from '{file_name}'")
    return text


def _from_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except PyPdfError as exc:
        raise UnsupportedDocumentError(f"Could not read PDF: {exc}") from exc
    text = "\n\n".join(pages).strip()
    if not text:
        raise UnsupportedDocumentError(
            "PDF contains no extractable text (scanned document? OCR is out of scope)"
        )
    return text


class _HTMLTextExtractor(HTMLParser):
    _SKIP = {"script", "style", "head", "title", "template", "noscript"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._chunks.append(data)

    def text(self) -> str:
        raw = "".join(self._chunks)
        # Collapse runs of blank space while keeping paragraph breaks readable.
        raw = re.sub(r"[ \t]+", " ", raw)
        return re.sub(r"\n\s*\n\s*", "\n\n", raw).strip()


def _from_html(markup: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(markup)
    return parser.text()
