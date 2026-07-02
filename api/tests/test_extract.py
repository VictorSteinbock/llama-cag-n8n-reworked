import pytest

from app.extract import UnsupportedDocumentError, extract_text, file_suffix


def test_plain_text_passthrough():
    assert extract_text("notes.txt", b"hello world\n") == "hello world"


def test_markdown_passthrough():
    assert extract_text("doc.md", b"# Title\n\ncontent") == "# Title\n\ncontent"


def test_html_strips_tags_and_scripts():
    html = b"""<html><head><title>skip me</title><style>p{color:red}</style></head>
    <body><h1>Heading</h1><script>alert('skip')</script><p>Body   text</p></body></html>"""
    text = extract_text("page.html", html)
    assert "Heading" in text
    assert "Body text" in text
    assert "skip" not in text
    assert "color:red" not in text


def test_unsupported_extension_rejected():
    with pytest.raises(UnsupportedDocumentError, match="Unsupported file type"):
        extract_text("archive.zip", b"PK\x03\x04")


def test_binary_masquerading_as_text_rejected():
    with pytest.raises(UnsupportedDocumentError, match="looks binary"):
        extract_text("weird.txt", b"abc\x00def")


def test_empty_document_rejected():
    with pytest.raises(UnsupportedDocumentError, match="No text"):
        extract_text("empty.txt", b"   \n  ")


def test_invalid_pdf_rejected():
    with pytest.raises(UnsupportedDocumentError):
        extract_text("fake.pdf", b"not a pdf at all")


def test_pdf_extraction_non_pypdf_error_becomes_415(monkeypatch):
    # A valid PDF that PdfReader opens, but whose page extraction raises a
    # non-PyPdfError (e.g. DependencyError, or a bare KeyError/TypeError on a
    # corrupt content stream). This must surface as UnsupportedDocumentError
    # (-> 415), not an unhandled exception (-> 500).
    import io

    from pypdf import PdfWriter
    from pypdf.errors import DependencyError

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    from pypdf._page import PageObject

    def boom(self, *args, **kwargs):
        raise DependencyError("PyCryptodome is required for this PDF")

    monkeypatch.setattr(PageObject, "extract_text", boom)

    with pytest.raises(UnsupportedDocumentError, match="Could not read PDF"):
        extract_text("needs-dep.pdf", pdf_bytes)


def test_pdf_with_some_empty_pages_keeps_the_text(monkeypatch):
    # A multi-page PDF where some pages have no extractable text must still
    # return the text from the pages that do, not error.
    import io

    from pypdf import PdfWriter
    from pypdf._page import PageObject

    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    pdf_bytes = buf.getvalue()

    calls = {"n": 0}

    def sometimes_empty(self, *args, **kwargs):
        calls["n"] += 1  # only the second of three pages has text
        return "page two body" if calls["n"] == 2 else ""

    monkeypatch.setattr(PageObject, "extract_text", sometimes_empty)
    assert extract_text("mixed.pdf", pdf_bytes) == "page two body"


def test_file_suffix_handles_windows_paths():
    assert file_suffix(r"C:\Users\vp\Documents\Report.PDF") == ".pdf"
    assert file_suffix("/data/documents/readme.md") == ".md"
