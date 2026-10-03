"""Generate lead sheet samples covering valid, mapping and rejection cases.

Run from be/: .venv/bin/python -m scripts.make_sample_sheets
"""

import io
from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).resolve().parents[1] / "samples" / "lead_sheets"
HEADER = "Name,Phone,Email,Source,Language,Notes"


def write_text(name: str, content: str, encoding: str = "utf-8") -> None:
    (OUT / name).write_bytes(content.encode(encoding))


def write_bytes(name: str, content: bytes) -> None:
    (OUT / name).write_bytes(content)


def xlsx(name: str, sheets: list[tuple[str, list[list]]]) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in sheets:
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buffer = io.BytesIO()
    wb.save(buffer)
    write_bytes(name, buffer.getvalue())


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # --- Valid imports ---
    write_text("01_valid_basic.csv", f"{HEADER}\r\n"
               "Anu Menon,9876543210,anu@example.com,Website,English,Asked for pricing\r\n"
               "Ravi Kumar,9123456789,ravi@example.com,Referral,Hindi,\r\n"
               "Priya Shah,9988776655,,Campaign,Gujarati,Call after 5pm\r\n")
    write_text("02_international_numbers.csv", "Name,Phone\n"
               "US Lead,+1 202 555 0123\n"
               "UK Lead,+44 7400 123456\n"
               "India with code,+91 98765 43210\n"
               "Local India,098765-43210\n")
    write_text("03_semicolon_delimiter.csv", "Name;Phone;Email\n"
               "Semi Colon;9876543210;semi@example.com\n")
    write_text("04_tab_delimited.csv", "Name\tPhone\tSource\n"
               "Tab Lead\t9876543210\tTradeshow\n")
    write_text("05_utf8_unicode_names.csv", f"{HEADER}\r\n"
               "അനു മേനോൻ,9876543210,,,Malayalam,ഫോൺ വിളിക്കുക\r\n"
               "रवि कुमार,9123456789,,,Hindi,दोबारा संपर्क करें\r\n"
               "李伟,+8613800138000,,,Chinese,\r\n")
    write_text("06_bom_and_blank_rows.csv", "﻿Name,Phone,City\n"
               "\n"
               "BOM Lead,9876543210,Kochi\n"
               ",,\n"
               "Second Lead,9123456789,Pune\n")
    write_text("07_extra_columns_kept.csv", "Name,Phone,Budget,Industry,Company Size\n"
               "Extra Fields,9876543210,50000,Retail,120\n")

    # --- Mapping needed: phone column not auto-detected (UI must ask) ---
    write_text("10_missing_phone_header.csv", "Name,Email,City\n"
               "No Phone Column,nophone@example.com,Chennai\n")
    write_text("11_phone_custom_header.csv", "Full Name,Contact Number Primary,City\n"
               "Custom Header,9876543210,Delhi\n")
    xlsx("12_xlsx_custom_headers.xlsx", [("Leads", [
        ["Client", "WhatsApp No", "Mail"],
        ["Xlsx Custom", 9876543210, "xlsx@example.com"],
    ])])

    # --- Row-level issues (imports the good rows, lists the bad ones) ---
    write_text("20_row_issues.csv", f"{HEADER}\r\n"
               "Good Lead,9876543210,good@example.com,Website,,\r\n"
               "Bad Phone,abc123,,Website,,\r\n"
               "Short Number,12345,,Website,,\r\n"
               "Bad Email,9123456789,not-an-email,Website,,\r\n"
               "Long Name," + "N" * 201 + ",9988776655,,,,\r\n"
               "Formula Cell,9876501234,,=SUM(1+2),,\r\n"
               "Notes Too Long,9765432100,,,,\"" + "x" * 2001 + "\"\r\n")
    write_text("21_duplicates_in_file.csv", f"{HEADER}\r\n"
               "First,9876543210,,,,\r\n"
               "Same Phone Again,9876543210,,,,\r\n"
               "Different,9123456789,,,,\r\n")

    # --- Rejected files (whole upload refused with a message) ---
    write_text("30_header_only.csv", f"{HEADER}\n")
    write_text("31_empty.csv", "")
    write_text("32_duplicate_headers.csv", "Name,Phone,phone\nDup,9876543210,9123456789\n")
    write_text("33_row_wider_than_header.csv", "Name,Phone\nWide,9876543210,extra value\n")
    write_text("34_latin1_encoding.csv", "Name,Phone\nJosé,9876543210\n", encoding="latin-1")
    write_text("35_no_header_name_gap.csv", "Name,,Phone\nGap,x,9876543210\n")
    write_bytes("36_legacy_xls_renamed.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64)
    write_bytes("37_fake_xlsx.xlsx", b"this is not really a workbook")
    write_text("38_too_many_rows.csv", "Name,Phone\n" + "".join(
        f"Lead {i},{9000000000 + i}\n" for i in range(5001)))
    write_text("39_wrong_extension.txt", "Name,Phone\nText,9876543210\n")

    # --- Excel workbooks ---
    xlsx("40_xlsx_first_sheet_used.xlsx", [
        ("Contacts", [["Name", "Phone", "Notes"], ["Sheet One", 9876543210, "Imported"]]),
        ("Ignored", [["Name", "Phone"], ["Should Not Import", 9123456789]]),
    ])
    xlsx("41_xlsx_numeric_and_formula.xlsx", [("Leads", [
        ["Name", "Phone", "Notes"],
        ["Numeric Phone", 9876543210, "Interested"],
        ["Formula Row", 9123456789, "=SUM(1,2)"],
    ])])
    xlsx("42_xlsx_missing_phone.xlsx", [("Leads", [
        ["Name", "Email"],
        ["No Phone", "nophone@example.com"],
    ])])
    xlsx("43_xlsx_header_only.xlsx", [("Leads", [["Name", "Phone"]])])

    print(f"Wrote sample sheets to {OUT}")


if __name__ == "__main__":
    main()
