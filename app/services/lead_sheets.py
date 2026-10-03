"""Bounded CSV/XLSX parsing and deterministic lead validation; no database I/O."""
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from zipfile import ZipFile

import phonenumbers
from openpyxl import load_workbook

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
MAX_COLUMNS = 100
FIELDS = ("name", "phone", "email", "source", "preferred_language", "notes")
ALIASES = {
    "name": ("name", "fullname", "leadname", "customername"),
    "phone": ("phone", "phonenumber", "mobile", "mobilenumber", "contactnumber", "telephone"),
    "email": ("email", "emailaddress"),
    "source": ("source", "leadsource"),
    "preferred_language": ("language", "preferredlanguage"),
    "notes": ("notes", "remarks", "comments"),
}


class SheetError(ValueError):
    pass


@dataclass
class Sheet:
    headers: list[str]
    rows: list[tuple[int, list[str]]]
    worksheet: str | None = None


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def read_sheet(data: bytes, filename: str) -> Sheet:
    if not data:
        raise SheetError("The file is empty.")
    if len(data) > MAX_BYTES:
        raise SheetError("Choose a file smaller than 5 MB.")
    suffix = Path(filename).suffix.lower()
    workbook = None
    worksheet = None
    try:
        if suffix == ".csv":
            try:
                content = data.decode("utf-8-sig")
            except UnicodeDecodeError:
                raise SheetError("Save the CSV with UTF-8 encoding and upload it again.") from None
            try:
                dialect = csv.Sniffer().sniff(content[:8192], delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            iterator = csv.reader(io.StringIO(content, newline=""), dialect, strict=True)
        elif suffix == ".xlsx":
            with ZipFile(io.BytesIO(data)) as archive:
                if len(archive.infolist()) > 1000 or sum(i.file_size for i in archive.infolist()) > 50 * 1024 * 1024:
                    raise SheetError("This workbook expands beyond the supported size. Export a smaller sheet.")
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=False, keep_links=False)
            if not workbook.worksheets:
                raise SheetError("The workbook has no worksheets.")
            ws = workbook.worksheets[0]
            worksheet = ws.title
            if ws.max_column and ws.max_column > MAX_COLUMNS:
                raise SheetError("Use a sheet with at most 100 columns.")
            iterator = ws.iter_rows(values_only=True)
        else:
            raise SheetError("Choose a .csv or .xlsx file. Export old .xls files as .xlsx first.")
        header = next(iterator, None)
        if not header:
            raise SheetError("Add a header row with column names.")
        headers = [cell_text(v) for v in header]
        # Ignore trailing empty Excel header cells, but not unnamed middle columns.
        while headers and not headers[-1]:
            headers.pop()
        if not headers or len(headers) > MAX_COLUMNS:
            raise SheetError("Use a header row with between 1 and 100 columns.")
        if any(not h or len(h) > 100 for h in headers):
            raise SheetError("Each column needs a name of 1–100 characters.")
        if len(set(h.casefold() for h in headers)) != len(headers):
            raise SheetError("Column names must be unique.")
        rows = []
        for number, row in enumerate(iterator, start=2):
            if number > 20001:
                raise SheetError("Remove excess blank rows; a sheet can contain at most 20,000 physical rows.")
            values = [cell_text(v) for v in row]
            if not any(values):
                continue
            if any(values[len(headers):]):
                raise SheetError(f"Row {number} contains data in a column without a header.")
            values = (values + [""] * len(headers))[:len(headers)]
            if any(len(v) > 10000 for v in values):
                raise SheetError(f"Row {number} contains a cell longer than 10,000 characters.")
            rows.append((number, values))
            if len(rows) > MAX_ROWS:
                raise SheetError("Import at most 5,000 leads at a time.")
        if not rows:
            raise SheetError("The sheet has headers but no lead rows.")
        return Sheet(headers, rows, worksheet)
    except SheetError:
        raise
    except Exception:
        # Never expose file content or parser internals in API errors.
        raise SheetError("The file could not be read. Export a fresh CSV or XLSX and try again.") from None
    finally:
        if workbook:
            workbook.close()


REQUIRED_FIELDS = ("phone",)


def suggested_mapping(headers):
    normalize = lambda value: re.sub(r"[^a-z0-9]", "", value.lower())
    return {field: next((h for h in headers if normalize(h) in aliases), "") for field, aliases in ALIASES.items()}


def missing_required(mapping: dict) -> list[str]:
    return [field for field in REQUIRED_FIELDS if not mapping.get(field)]


def validate_mapping(sheet: Sheet, mapping: dict, country: str):
    """Raise SheetError with a user-facing message for any mapping the UI could not have produced."""
    if not isinstance(mapping, dict) or any(k not in FIELDS or not isinstance(v, str) for k, v in mapping.items()):
        raise SheetError("Choose valid column mappings.")
    if missing_required(mapping):
        raise SheetError("Map the phone column before continuing.")
    selected = [v for v in mapping.values() if v]
    if any(v not in sheet.headers for v in selected):
        raise SheetError("A mapped column is not in this file. Choose the columns again.")
    if len(selected) != len(set(selected)):
        raise SheetError("Map each spreadsheet column to only one field.")
    if not isinstance(country, str) or country not in phonenumbers.SUPPORTED_REGIONS:
        raise SheetError("Choose a valid default phone country.")


def normalize_rows(sheet: Sheet, mapping: dict, country: str):
    validate_mapping(sheet, mapping, country)
    valid, issues = [], []
    for number, cells in sheet.rows:
        original = dict(zip(sheet.headers, cells))
        values = {field: original.get(mapping.get(field, ""), "") for field in FIELDS}
        error = None
        if any(v.startswith("=") for v in cells):
            error = "Formula cells are unsupported. Paste values and upload again."
        try:
            phone = phonenumbers.parse(values["phone"], country)
            if phone.extension or not phonenumbers.is_valid_number(phone):
                raise ValueError()
            values["phone"] = phonenumbers.format_number(phone, phonenumbers.PhoneNumberFormat.E164)
        except (phonenumbers.NumberParseException, ValueError):
            error = error or "Enter a valid phone number, including country code for international numbers."
        limits = {"name": 200, "email": 254, "source": 100, "preferred_language": 20, "notes": 2000}
        for field, limit in limits.items():
            if len(values[field]) > limit:
                error = error or f"{field.replace('_', ' ').title()} must be at most {limit} characters."
        if values["email"] and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["email"]):
            error = error or "Enter a valid email address or leave it blank."
        if any("\x00" in v for v in cells):
            error = "Remove null characters from this row."
        if error:
            issues.append({"row": number, "kind": "invalid", "message": error})
        else:
            values["extra_fields"] = {k: v for k, v in original.items() if k not in mapping.values() and v}
            if len(json.dumps(values["extra_fields"]).encode()) > 20000:
                issues.append({"row": number, "kind": "invalid", "message": "Additional fields exceed 20 KB for this row."})
                continue
            values["row"] = number
            valid.append(values)
    return valid, issues


def fingerprint(data, mapping, country):
    # Ignore blank optional mappings so equivalent submissions have one identity.
    normalized = {k: v for k, v in mapping.items() if v}
    return hashlib.sha256(data + json.dumps([normalized, country], sort_keys=True).encode()).hexdigest()
