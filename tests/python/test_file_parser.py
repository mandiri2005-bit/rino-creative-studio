"""
Tests for laozhang_api.parse_uploaded_file()
"""
import pytest
import io
from fastapi import HTTPException


def get_parser():
    from laozhang_api import parse_uploaded_file
    return parse_uploaded_file


# ─── Plain text / code formats ────────────────────────────────────────────────
class TestTextFormats:
    def test_txt(self):
        p = get_parser()
        assert p("readme.txt", b"Hello world") == "Hello world"

    def test_md(self):
        p = get_parser()
        content = b"# Title\n\nParagraph text."
        assert p("notes.md", content) == "# Title\n\nParagraph text."

    def test_py(self):
        p = get_parser()
        code = b"def foo():\n    return 42\n"
        assert p("script.py", code) == "def foo():\n    return 42\n"

    def test_js(self):
        p = get_parser()
        assert p("app.js", b"console.log('hi')") == "console.log('hi')"

    def test_json(self):
        p = get_parser()
        raw = b'{"a": 1}'
        assert p("data.json", raw) == '{"a": 1}'

    def test_csv(self):
        p = get_parser()
        raw = b"col1,col2\n1,2\n"
        assert p("data.csv", raw) == "col1,col2\n1,2\n"

    def test_yaml(self):
        p = get_parser()
        raw = b"key: value\nlist:\n  - a\n"
        assert p("config.yaml", raw) == "key: value\nlist:\n  - a\n"

    def test_sql(self):
        p = get_parser()
        assert p("query.sql", b"SELECT * FROM users;") == "SELECT * FROM users;"

    def test_srt_subtitle(self):
        p = get_parser()
        raw = b"1\n00:00:01,000 --> 00:00:04,000\nHello!\n"
        result = p("sub.srt", raw)
        assert "Hello!" in result

    def test_utf8_with_non_ascii(self):
        p = get_parser()
        raw = "Café, résumé, naïve".encode("utf-8")
        result = p("test.txt", raw)
        assert "Café" in result

    def test_unknown_text_extension_fallback(self):
        """Unknown extension that is valid UTF-8 should decode successfully."""
        p = get_parser()
        result = p("file.unknown_ext", b"plain text content")
        assert "plain text" in result


# ─── Binary/unsupported ───────────────────────────────────────────────────────
class TestUnsupportedFormats:
    def test_binary_exe_raises(self):
        p = get_parser()
        with pytest.raises(HTTPException) as exc:
            p("prog.exe", bytes(range(256)))
        assert exc.value.status_code == 400

    def test_binary_zip_raises(self):
        p = get_parser()
        with pytest.raises(HTTPException):
            p("archive.zip", b"PK\x03\x04" + bytes(100))


# ─── PDF (graceful degradation if pdfplumber not installed) ──────────────────
class TestPdfParsing:
    def test_pdf_raises_400_if_not_installed(self):
        """If pdfplumber isn't installed, should raise 400 with clear message."""
        from laozhang_api import PDF_OK
        if PDF_OK:
            pytest.skip("pdfplumber installed — PDF path tested separately")
        p = get_parser()
        with pytest.raises(HTTPException) as exc:
            p("doc.pdf", b"%PDF-1.4 fake content")
        assert exc.value.status_code == 400
        assert "pdfplumber" in str(exc.value.detail).lower()


# ─── DOCX (graceful degradation) ─────────────────────────────────────────────
class TestDocxParsing:
    def test_docx_raises_400_if_not_installed(self):
        from laozhang_api import DOCX_OK
        if DOCX_OK:
            pytest.skip("python-docx installed — DOCX path tested separately")
        p = get_parser()
        with pytest.raises(HTTPException) as exc:
            p("doc.docx", b"PK\x03\x04 fake docx")
        assert exc.value.status_code == 400
        assert "python-docx" in str(exc.value.detail).lower()


# ─── XLSX (graceful degradation) ─────────────────────────────────────────────
class TestXlsxParsing:
    def test_xlsx_raises_400_if_not_installed(self):
        from laozhang_api import XLSX_OK
        if XLSX_OK:
            pytest.skip("openpyxl installed — XLSX tested separately")
        p = get_parser()
        with pytest.raises(HTTPException) as exc:
            p("data.xlsx", b"PK\x03\x04 fake xlsx")
        assert exc.value.status_code == 400
        assert "openpyxl" in str(exc.value.detail).lower()
