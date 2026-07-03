"""Tests for `python llamacag.py prepare` (F6).

Collected by `pytest api` (under api/tests/); the shim imports the stdlib-only
root llamacag module. pypdf is monkeypatched the way test_extract.py does it.
"""

import argparse
import io
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import llamacag  # noqa: E402


def _blank_pdf(path: pathlib.Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    path.write_bytes(buf.getvalue())


def _ns(file, out=None, force=False):
    return argparse.Namespace(file=str(file), out=out, force=force)


def _forbid_subprocess(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("prepare must not shell out on this path")

    monkeypatch.setattr(llamacag.subprocess, "run", boom)


def test_prepare_text_layer_pdf_writes_markdown(monkeypatch, tmp_path):
    from pypdf._page import PageObject

    pdf = tmp_path / "policy.pdf"
    _blank_pdf(pdf)
    monkeypatch.setattr(
        PageObject, "extract_text", lambda self, *a, **k: "Clause 4: refunds within 30 days"
    )
    monkeypatch.setattr(llamacag, "read_env", lambda: {"PREPARE_OUT_FOLDER": str(tmp_path)})
    _forbid_subprocess(monkeypatch)  # the text-layer path is offline, never a converter

    assert llamacag.cmd_prepare(_ns(pdf)) == 0
    out = tmp_path / "policy.md"
    assert "refunds within 30 days" in out.read_text(encoding="utf-8")


def test_prepare_image_pdf_without_converter_is_guided_error(monkeypatch, tmp_path, capsys):
    from pypdf._page import PageObject

    pdf = tmp_path / "scan.pdf"
    _blank_pdf(pdf)
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *a, **k: "")
    monkeypatch.setattr(llamacag, "read_env", lambda: {"PREPARE_OUT_FOLDER": str(tmp_path)})
    _forbid_subprocess(monkeypatch)

    assert llamacag.cmd_prepare(_ns(pdf)) == 1
    out = capsys.readouterr().out
    for needle in ("marker", "docling", "vision", "PREPARE_CMD"):
        assert needle in out
    assert not (tmp_path / "scan.md").exists()  # nothing written


def test_prepare_image_pdf_with_converter_runs_it(monkeypatch, tmp_path):
    from pypdf._page import PageObject

    pdf = tmp_path / "scan.pdf"
    _blank_pdf(pdf)
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *a, **k: "")
    monkeypatch.setattr(
        llamacag, "read_env",
        lambda: {"PREPARE_CMD": "fakeconv {in} {out}", "PREPARE_OUT_FOLDER": str(tmp_path)},
    )
    monkeypatch.setattr(llamacag.shutil, "which", lambda name: "/usr/bin/" + name)

    captured = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        pathlib.Path(argv[2]).write_text("# Converted\nbody", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(llamacag.subprocess, "run", fake_run)

    assert llamacag.cmd_prepare(_ns(pdf)) == 0
    assert (tmp_path / "scan.md").read_text(encoding="utf-8") == "# Converted\nbody"
    # argv is a real list with {in}/{out} replaced by resolved absolute paths.
    argv = captured["argv"]
    assert isinstance(argv, list)
    assert "{in}" not in argv and "{out}" not in argv
    assert argv[1] == str(pdf.resolve())
    assert argv[2].endswith("scan.md.partial")


def test_prepare_converter_missing_on_path(monkeypatch, tmp_path, capsys):
    from pypdf._page import PageObject

    pdf = tmp_path / "scan.pdf"
    _blank_pdf(pdf)
    monkeypatch.setattr(PageObject, "extract_text", lambda self, *a, **k: "")
    monkeypatch.setattr(
        llamacag, "read_env",
        lambda: {"PREPARE_CMD": "missingconv {in} {out}", "PREPARE_OUT_FOLDER": str(tmp_path)},
    )
    monkeypatch.setattr(llamacag.shutil, "which", lambda name: None)

    assert llamacag.cmd_prepare(_ns(pdf)) == 1
    out = capsys.readouterr().out
    assert "missingconv" in out
    assert "PATH" in out


def test_prepare_refuses_existing_dest_without_force(monkeypatch, tmp_path, capsys):
    src = tmp_path / "notes.txt"
    src.write_text("already text", encoding="utf-8")
    monkeypatch.setattr(llamacag, "read_env", lambda: {"PREPARE_OUT_FOLDER": str(tmp_path)})
    dest = tmp_path / "notes.md"
    dest.write_text("previous conversion", encoding="utf-8")

    assert llamacag.cmd_prepare(_ns(src)) == 1
    assert "--force" in capsys.readouterr().out

    assert llamacag.cmd_prepare(_ns(src, force=True)) == 0
    assert dest.read_text(encoding="utf-8") == "already text"


def test_prepare_passthrough_text_file(monkeypatch, tmp_path):
    src = tmp_path / "page.html"
    src.write_text("<h1>Facts</h1>", encoding="utf-8")
    monkeypatch.setattr(llamacag, "read_env", lambda: {"PREPARE_OUT_FOLDER": str(tmp_path)})
    _forbid_subprocess(monkeypatch)  # passthrough copies text, never shells out

    assert llamacag.cmd_prepare(_ns(src)) == 0
    assert (tmp_path / "page.md").read_text(encoding="utf-8") == "<h1>Facts</h1>"
