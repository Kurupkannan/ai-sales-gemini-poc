/** Paste this entire file into Extensions > Apps Script > Code.gs.
 * Run setupTracker once, then deploy as a web app: Execute as Me / Anyone.
 * This endpoint exports app data to Sheets; sheet edits do not start calls.
 */
const TRACKER = {
  headers: [
    'Lead ID', 'Name', 'Phone', 'Email', 'Source', 'Language',
    'Lead Status', 'Result', 'Call Status', 'Provider Status', 'Call Summary',
    'Preferred Callback', 'Webinar Response', 'Send Webinar Link', 'Genuineness',
    'Duration (seconds)', 'Last Call ID', 'Last Call At (IST)', 'Updated At (IST)',
    'Owner', 'Follow-up At (IST)', 'Team Notes'
  ],
  fields: [
    'lead_id', 'name', 'phone', 'email', 'source', 'language',
    'lead_status', 'result', 'call_status', 'provider_status', 'summary',
    'preferred_callback_time', 'webinar_response', 'send_webinar_link', 'genuineness',
    'duration_seconds', 'call_id', 'last_call_at', 'updated_at'
  ],
  choices: {
    lead_status: ['new', 'contacted', 'qualified', 'unqualified', 'closed'],
    result: ['HOT', 'WARM', 'COLD', 'NOT_INTERESTED', 'CALLBACK', 'WRONG_NUMBER',
      'DO_NOT_CALL', 'NOT_GENUINE', 'NOT_ANSWERED', 'NO_RESPONSE', 'UNKNOWN'],
    call_status: ['pending', 'initiated', 'active', 'completed', 'failed', 'unknown'],
    webinar_response: ['YES', 'NO', 'BUSY', 'NO_RESPONSE', 'UNKNOWN'],
    genuineness: ['CONFIRMED', 'DENIED', 'UNCONFIRMED']
  }
};

function onOpen() {
  SpreadsheetApp.getUi().createMenu('AI Sales')
    .addItem('Set up / refresh formatting', 'setupTracker')
    .addItem('Show connection code', 'showConnectionCode')
    .addToUi();
}

function setupTracker() {
  const lock = LockService.getScriptLock();
  lock.waitLock(20000);
  try {
    const props = PropertiesService.getScriptProperties();
    const book = SpreadsheetApp.getActiveSpreadsheet();
    if (!book) throw new Error('Open Apps Script from your Google Sheet.');
    if (props.getProperty('SPREADSHEET_ID') && props.getProperty('SPREADSHEET_ID') !== book.getId()) {
      throw new Error('This script is already connected to another spreadsheet.');
    }
    let sheet = props.getProperty('TRACKER_SHEET_ID')
      ? book.getSheets().find(s => String(s.getSheetId()) === props.getProperty('TRACKER_SHEET_ID'))
      : book.getSheetByName('Leads');
    if (!sheet) {
      const first = book.getSheets()[0];
      sheet = book.getSheets().length === 1 && first.getLastRow() === 0
        ? first.setName('Leads') : book.insertSheet('Leads');
    }
    if (sheet.getLastRow() > 0) {
      // Upgrade the earlier tracker while keeping every other column aligned.
      const legacyHeaders = TRACKER.headers.slice();
      legacyHeaders.splice(1, 0, 'Organization ID');
      if (sheet.getMaxColumns() >= legacyHeaders.length &&
          JSON.stringify(sheet.getRange(1, 1, 1, legacyHeaders.length).getValues()[0]) === JSON.stringify(legacyHeaders)) {
        sheet.deleteColumn(2);
      }
      checkHeaders_(sheet);
    }
    compactTrackerRows_(sheet);
    book.setSpreadsheetTimeZone('Asia/Kolkata');
    if (sheet.getMaxColumns() < TRACKER.headers.length) {
      sheet.insertColumnsAfter(sheet.getMaxColumns(), TRACKER.headers.length - sheet.getMaxColumns());
    }
    if (sheet.getMaxRows() < 1001) sheet.insertRowsAfter(sheet.getMaxRows(), 1001 - sheet.getMaxRows());
    sheet.getRange(1, 1, 1, TRACKER.headers.length).setValues([TRACKER.headers])
      .setBackground('#15324f').setFontColor('#ffffff').setFontWeight('bold')
      .setWrap(true).setVerticalAlignment('middle');
    sheet.setRowHeight(1, 44);
    sheet.setFrozenRows(1);
    sheet.setFrozenColumns(2);
    sheet.setHiddenGridlines(true);
    sheet.setTabColor('#2563eb');
    sheet.setColumnWidths(1, TRACKER.headers.length, 155);
    sheet.setColumnWidth(1, 270);
    sheet.setColumnWidth(2, 200);
    sheet.setColumnWidth(11, 440);
    sheet.setColumnWidth(12, 240);
    sheet.setColumnWidth(17, 270);
    sheet.setColumnWidths(18, 2, 190);
    sheet.setColumnWidth(21, 190);
    sheet.setColumnWidth(22, 360);
    const all = sheet.getRange(1, 1, sheet.getMaxRows(), TRACKER.headers.length);
    all.setFontFamily('Arial').setFontSize(10);
    if (!sheet.getFilter()) all.createFilter();
    const rules = [];
    formatRows_(sheet, 2, sheet.getMaxRows() - 1);
    Object.keys(TRACKER.choices).forEach(field => {
      const col = TRACKER.fields.indexOf(field) + 1;
      const range = sheet.getRange(2, col, sheet.getMaxRows() - 1, 1);
      TRACKER.choices[field].forEach(value => {
        let bg = '#e8eef5', fg = '#334155';
        if (['HOT', 'qualified', 'completed', 'YES', 'CONFIRMED'].includes(value)) { bg = '#dcfce7'; fg = '#166534'; }
        if (['WARM', 'CALLBACK', 'pending', 'initiated', 'BUSY'].includes(value)) { bg = '#fef3c7'; fg = '#92400e'; }
        if (['DO_NOT_CALL', 'WRONG_NUMBER', 'NOT_GENUINE', 'failed', 'DENIED'].includes(value)) { bg = '#fee2e2'; fg = '#991b1b'; }
        if (value === 'active') { bg = '#dbeafe'; fg = '#1e40af'; }
        rules.push(SpreadsheetApp.newConditionalFormatRule().whenTextEqualTo(value)
          .setBackground(bg).setFontColor(fg).setRanges([range]).build());
      });
    });
    sheet.setConditionalFormatRules(rules);
    props.setProperties({SPREADSHEET_ID: book.getId(), TRACKER_SHEET_ID: String(sheet.getSheetId())});
    if (!props.getProperty('ACCESS_CODE')) {
      props.setProperty('ACCESS_CODE', Utilities.getUuid().replace(/-/g, '') + Utilities.getUuid().replace(/-/g, ''));
    }
    SpreadsheetApp.flush();
  } finally {
    lock.releaseLock();
  }
  showConnectionCode();
}

function showConnectionCode() {
  const code = PropertiesService.getScriptProperties().getProperty('ACCESS_CODE');
  if (!code) throw new Error('Run setupTracker first.');
  SpreadsheetApp.getUi().alert('AI Sales connection code',
    'Copy this code for the backend connection:\n\n' + code +
    '\n\nKeep the sheet private. Deploy this script as a web app and share its /exec URL.',
    SpreadsheetApp.getUi().ButtonSet.OK);
}

function doGet() {
  // Public health only: never expose lead data or the connection code.
  return json_({ok: true, service: 'ai-sales-sheets', version: 3});
}

function doPost(e) {
  const lock = LockService.getScriptLock();
  let locked = false;
  try {
    if (!e || !e.postData || e.postData.contents.length > 262144) return json_({ok: false, error: 'invalid_request'});
    let body;
    try { body = JSON.parse(e.postData.contents); }
    catch (_) { return json_({ok: false, error: 'invalid_json'}); }
    const props = PropertiesService.getScriptProperties();
    const code = props.getProperty('ACCESS_CODE');
    if (!body || !code || typeof body.access_code !== 'string' || body.access_code !== code) {
      return json_({ok: false, error: 'unauthorized'});
    }
    if (body.action === 'ping') return json_({ok: true, service: 'ai-sales-sheets', version: 3});
    if (body.action !== 'upsert_lead') return json_({ok: false, error: 'unsupported_action'});
    const lead = validateLead_(body.lead);
    if (!lock.tryLock(20000)) return json_({ok: false, error: 'busy', retryable: true});
    locked = true;
    const book = SpreadsheetApp.openById(props.getProperty('SPREADSHEET_ID'));
    const sheet = book.getSheets().find(s => String(s.getSheetId()) === props.getProperty('TRACKER_SHEET_ID'));
    if (!sheet) return json_({ok: false, error: 'tracker_missing'});
    checkHeaders_(sheet);
    const last = sheet.getLastRow();
    const rows = last > 1 ? sheet.getRange(2, 1, last - 1, TRACKER.headers.length).getValues() : [];
    const matches = rows.map((row, i) => row[0] === lead.lead_id ? i + 2 : 0).filter(Boolean);
    if (matches.length > 1) return json_({ok: false, error: 'duplicate_lead_ids'});
    // Checkbox FALSE values do not make a row a lead. Preserve manual-only rows.
    const vacant = rows.findIndex(rowIsEmpty_);
    const row = matches[0] || (vacant >= 0 ? vacant + 2 : Math.max(2, last + 1));
    if (row > sheet.getMaxRows()) {
      sheet.insertRowsAfter(sheet.getMaxRows(), 500);
      // Refresh once to extend dropdowns, conditional colors and filter to new rows.
      formatRows_(sheet, row, sheet.getMaxRows() - row + 1);
      const rules = sheet.getConditionalFormatRules();
      sheet.setConditionalFormatRules(rules.map(rule => rule.copy().setRanges(
        rule.getRanges().map(r => sheet.getRange(2, r.getColumn(), sheet.getMaxRows() - 1, r.getNumColumns()))
      ).build()));
      if (sheet.getFilter()) sheet.getFilter().remove();
      sheet.getRange(1, 1, sheet.getMaxRows(), TRACKER.headers.length).createFilter();
    }
    const values = matches.length ? sheet.getRange(row, 1, 1, TRACKER.fields.length).getValues()[0]
      : TRACKER.fields.map(() => '');
    const previous = values[TRACKER.fields.indexOf('updated_at')];
    if (previous && new Date(previous).getTime() >= lead.updated_at.getTime()) {
      return json_({ok: true, result: 'skipped_stale', row: row});
    }
    TRACKER.fields.forEach((key, i) => {
      if (Object.prototype.hasOwnProperty.call(lead, key)) values[i] = safeCell_(lead[key]);
    });
    if (!matches.length && !values[TRACKER.fields.indexOf('lead_status')]) values[TRACKER.fields.indexOf('lead_status')] = 'new';
    const checkbox = TRACKER.fields.indexOf('send_webinar_link');
    values[checkbox] = values[checkbox] === true || values[checkbox] === 'TRUE';
    // Checkbox cells must use General, even when an earlier setup used plain text.
    sheet.getRange(row, checkbox + 1).setNumberFormat('General');
    // Only A:S are app-owned; Owner, Follow-up At and Team Notes are preserved.
    sheet.getRange(row, 1, 1, values.length).setValues([values]);
    SpreadsheetApp.flush();
    return json_({ok: true, result: matches.length ? 'updated' : 'created', row: row});
  } catch (err) {
    return json_({ok: false, error: err.name === 'InputError' ? err.message : 'sheet_operation_failed'});
  } finally {
    if (locked) lock.releaseLock();
  }
}

function checkHeaders_(sheet) {
  if (sheet.getMaxColumns() < TRACKER.headers.length ||
      JSON.stringify(sheet.getRange(1, 1, 1, TRACKER.headers.length).getValues()[0]) !== JSON.stringify(TRACKER.headers)) {
    throw new Error('Tracker headers changed. Restore the original headers or use a blank Leads tab.');
  }
}

function formatRows_(sheet, start, count) {
  sheet.getRange(start, 1, count, TRACKER.headers.length).setVerticalAlignment('top').setWrap(true);
  sheet.getRange(start, 1, count, TRACKER.fields.length).setNumberFormat('@');
  sheet.getRange(start, 16, count, 1).setNumberFormat('0');
  sheet.getRange(start, 18, count, 2).setNumberFormat('dd mmm yyyy hh:mm');
  sheet.getRange(start, 21, count, 1).setNumberFormat('dd mmm yyyy hh:mm');
  sheet.getRange(start, 20, count, 3).setBackground('#eff6ff');
  Object.keys(TRACKER.choices).forEach(field => {
    sheet.getRange(start, TRACKER.fields.indexOf(field) + 1, count, 1)
      .setDataValidation(SpreadsheetApp.newDataValidation()
        .requireValueInList(TRACKER.choices[field], true).setAllowInvalid(false).build());
  });
  sheet.getRange(start, 14, count, 1).setNumberFormat('General').setDataValidation(
    SpreadsheetApp.newDataValidation().requireCheckbox().setAllowInvalid(false).build());
}

function rowIsEmpty_(row) {
  const checkbox = TRACKER.fields.indexOf('send_webinar_link');
  return row.every((value, i) => value === '' || value === null ||
    (i === checkbox && (value === false || value === 'FALSE')));
}

function compactTrackerRows_(sheet) {
  const last = sheet.getLastRow();
  if (last < 2) return;
  const range = sheet.getRange(2, 1, last - 1, TRACKER.headers.length);
  const rows = range.getValues();
  const formulas = range.getFormulas();
  let target = 0;
  rows.forEach((row, i) => {
    // Keep rows with human notes or formulas too. Only unused checkbox rows vanish.
    if (rowIsEmpty_(row) && formulas[i].every(value => value === '')) return;
    if (i !== target) {
      sheet.getRange(i + 2, 1, 1, TRACKER.headers.length)
        .moveTo(sheet.getRange(target + 2, 1, 1, TRACKER.headers.length));
    }
    target += 1;
  });
}

function validateLead_(input) {
  function invalid(message) { const e = new Error(message); e.name = 'InputError'; throw e; }
  if (!input || typeof input !== 'object' || Array.isArray(input)) invalid('invalid_lead');
  const lead = {};
  Object.keys(input).forEach(key => {
    if (!TRACKER.fields.includes(key)) invalid('unsupported_field');
    const value = input[key];
    if (key === 'lead_id') {
      if (typeof value !== 'string' || !/^[a-zA-Z0-9_-]{1,200}$/.test(value)) invalid('invalid_' + key);
    } else if (['last_call_at', 'updated_at'].includes(key)) {
      if (value === null && key === 'last_call_at') { lead[key] = ''; return; }
      if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/.test(value) || !Number.isFinite(Date.parse(value))) invalid('invalid_' + key);
      lead[key] = new Date(value); return;
    } else if (key === 'duration_seconds') {
      if (value !== null && (!Number.isInteger(value) || value < 0)) invalid('invalid_duration_seconds');
    } else if (key === 'send_webinar_link') {
      if (typeof value !== 'boolean') invalid('invalid_send_webinar_link');
    } else if (value !== null && (typeof value !== 'string' || value.length > 10000)) {
      invalid('invalid_' + key);
    }
    if (TRACKER.choices[key] && value !== null && value !== '' && !TRACKER.choices[key].includes(value)) invalid('invalid_' + key);
    lead[key] = value === null ? '' : value;
  });
  ['lead_id', 'updated_at'].forEach(key => {
    if (!lead[key]) invalid('missing_' + key);
  });
  return lead;
}

function safeCell_(value) {
  // Prevent contact names, notes and phone numbers from becoming formulas.
  return typeof value === 'string' && /^\s*[=+@-]/.test(value) ? "'" + value : value;
}

function json_(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}
