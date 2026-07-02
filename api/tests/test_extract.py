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


def test_file_suffix_handles_windows_paths():
    assert file_suffix(r"C:\Users\vp\Documents\Report.PDF") == ".pdf"
    assert file_suffix("/data/documents/readme.md") == ".md"
