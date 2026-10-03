import io
import pytest
from openpyxl import Workbook
from app.services.lead_sheets import SheetError, read_sheet, normalize_rows, suggested_mapping, validate_mapping, MAX_BYTES, fingerprint


def test_csv_normalizes_phone_and_preserves_extra_fields():
    sheet = read_sheet('Full Name,Mobile,Email,City\r\nഅനു,9876543210,anu@example.com,Kochi\r\n'.encode(), 'leads.csv')
    mapping = suggested_mapping(sheet.headers)
    valid, issues = normalize_rows(sheet, mapping, 'IN')
    assert not issues
    assert valid[0]['name'] == 'അനു'
    assert valid[0]['phone'] == '+919876543210'
    assert valid[0]['extra_fields'] == {'City': 'Kochi'}


def test_xlsx_first_sheet_numeric_phone_and_formula_rejection():
    wb = Workbook(); ws = wb.active; ws.title = 'Contacts'
    ws.append(['Name', 'Phone', 'Notes']); ws.append(['Anu', 9876543210, 'Interested']); ws.append(['Formula', 9123456789, '=SUM(1,2)'])
    other = wb.create_sheet('Ignore'); other.append(['not imported'])
    buffer = io.BytesIO(); wb.save(buffer)
    sheet = read_sheet(buffer.getvalue(), 'leads.xlsx')
    valid, issues = normalize_rows(sheet, suggested_mapping(sheet.headers), 'IN')
    assert sheet.worksheet == 'Contacts' and len(sheet.rows) == 2
    assert valid[0]['phone'] == '+919876543210'
    assert issues[0]['row'] == 3 and 'Formula' in issues[0]['message']


@pytest.mark.parametrize('content,filename', [(b'', 'a.csv'), (b'Phone,phone\n1,2', 'a.csv'), (b'Name,\nAnu,123', 'a.csv'), (b'Name,Phone\n', 'a.csv'), (b'fake', 'a.xlsx'), (b'fake', 'a.xls'), (b'a' * (MAX_BYTES + 1), 'a.csv')])
def test_bad_files_are_rejected(content, filename):
    with pytest.raises(SheetError): read_sheet(content, filename)


def test_validation_errors_and_mapping():
    sheet = read_sheet(b'Name,Phone,Email\nA,abc,a@example.com\nB,9876543210,bad\nC,9123456789,c@example.com\n', 'a.csv')
    valid, issues = normalize_rows(sheet, suggested_mapping(sheet.headers), 'IN')
    assert len(valid) == 1 and [i['row'] for i in issues] == [2, 3]
    with pytest.raises(SheetError): normalize_rows(sheet, {'name': 'Name'}, 'IN')
    with pytest.raises(SheetError): normalize_rows(sheet, {'phone': 'Phone', 'name': 'Phone'}, 'IN')
    with pytest.raises(SheetError): normalize_rows(sheet, {'phone': 'Phone'}, 'XX')


def test_blank_rows_and_semicolon_csv():
    sheet = read_sheet(b'Name;Phone\n\nAnu;9876543210\n', 'leads.csv')
    assert len(sheet.rows) == 1 and sheet.rows[0][0] == 3


def test_fingerprint_normalizes_optional_mappings():
    assert fingerprint(b'file', {'phone': 'Phone'}, 'IN') == fingerprint(b'file', {'phone': 'Phone', 'name': ''}, 'IN')
    assert fingerprint(b'file', {'phone': 'Phone'}, 'IN') != fingerprint(b'file', {'phone': 'Phone'}, 'US')


async def test_inspection_endpoint_and_request_size_limit():
    from app.main import app
    from httpx import AsyncClient, ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
        response = await client.post('/api/imports/inspect', files={'file': ('a.csv', b'Name,Phone\nAnu,9876543210\n')})
        assert response.status_code == 200 and response.json()['mapping']['phone'] == 'Phone'
        response = await client.post('/api/imports/inspect', content=b'x', headers={'content-length': str(MAX_BYTES + 100000)})
        assert response.status_code == 413
        async def chunks():
            for _ in range(7): yield b'x' * 1024 * 1024
        response = await client.post('/api/imports/inspect', content=chunks(), headers={'content-type': 'multipart/form-data; boundary=test'})
        assert response.status_code in (400, 413)  # malformed multipart can fail before the body limit


def test_missing_phone_column_is_reported_for_mapping():
    from app.services.lead_sheets import missing_required
    sheet = read_sheet(b'Name,Mobile Digits\nAnu,9876543210\n', 'leads.csv')
    mapping = suggested_mapping(sheet.headers)
    assert missing_required(mapping) == ['phone']
    with pytest.raises(SheetError, match='Map the phone column'): validate_mapping(sheet, mapping, 'IN')
    with pytest.raises(SheetError, match='Map the phone column'): validate_mapping(sheet, {'phone': '', 'name': 'Name'}, 'IN')
    assert missing_required({'phone': 'Mobile Digits'}) == []


@pytest.mark.parametrize('mapping,message', [
    ({'phone': 'Nope'}, 'not in this file'),
    ({'phone': 'Phone', 'email': 'Phone'}, 'only one field'),
    ({'phone': 'Phone', 'name': 7}, 'valid column mappings'),
    ({'phone': 'Phone', 'bogus': 'Name'}, 'valid column mappings'),
    (['Phone'], 'valid column mappings'),
])
def test_invalid_mappings_raise_user_messages(mapping, message):
    sheet = read_sheet(b'Name,Phone\nAnu,9876543210\n', 'a.csv')
    with pytest.raises(SheetError, match=message): validate_mapping(sheet, mapping, 'IN')


def test_invalid_country_and_non_string_country_are_rejected():
    sheet = read_sheet(b'Name,Phone\nAnu,9876543210\n', 'a.csv')
    for country in ('XX', None, 91):
        with pytest.raises(SheetError, match='phone country'): validate_mapping(sheet, {'phone': 'Phone'}, country)
