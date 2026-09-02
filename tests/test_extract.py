import io

from kb_helper.connectors.extract import extract_text, html_to_text, is_supported


def test_plain_text_and_bom():
    assert extract_text("﻿hello\r\nworld".encode("utf-8"), "a.txt") == "hello\nworld"


def test_html_strips_script_and_style():
    text = html_to_text("<div><style>x{}</style><script>alert(1)</script><p>Hi <b>there</b></p><p>Two</p></div>")
    assert text == "Hi there\n\nTwo"


def test_docx(kb_dir):
    text = extract_text((kb_dir / "hr" / "leave-policy.docx").read_bytes(), "leave-policy.docx")
    assert "# Leave Policy" in text
    assert "Workday" in text
    assert "Type | Days" in text


def test_pdf_blank_page_does_not_crash():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert extract_text(buffer.getvalue(), "x.pdf") == ""


def test_xlsx_and_csv():
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "SLA"
    sheet.append(["Type", "Hours"])
    sheet.append(["P1", 1])
    buffer = io.BytesIO()
    workbook.save(buffer)
    text = extract_text(buffer.getvalue(), "sla.xlsx")
    assert "## Sheet: SLA" in text and "P1 | 1" in text
    assert extract_text(b"a,b\n1,2\n", "t.csv") == "a | b\n1 | 2"


def test_unsupported_returns_empty():
    assert extract_text(b"\x00", "photo.png") == ""
    assert not is_supported("photo.png")
    assert is_supported("Page.ASPX")


def test_corrupt_docx_reports_error_instead_of_raising():
    text = extract_text(b"not a docx", "broken.docx")
    assert text.startswith("[could not extract text from broken.docx")
