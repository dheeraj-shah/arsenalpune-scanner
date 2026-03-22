/**
 * Arsenal Pune SC - Check-in Backend (Google Apps Script)
 *
 * Deploy as Web App:
 *   1. Extensions > Apps Script in your Google Sheet
 *   2. Paste this code
 *   3. Deploy > New deployment > Web app
 *   4. Execute as: Me, Who has access: Anyone
 *   5. Copy the URL and pass it to generate_checkin.py --sync-url
 *
 * Sheet setup:
 *   "Log" sheet: timestamp | match_slug | guest_id | count | device_id | action
 *   "Guests" sheet: guest_id | name | email | phone | amount | quantity | status | screenings | match_slug
 */

var SHEET_NAME = 'Log';
var GUESTS_SHEET = 'Guests';

function getLogSheet() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
    sheet.appendRow(['timestamp', 'match_slug', 'guest_id', 'count', 'device_id', 'action']);
  }
  return sheet;
}

/**
 * GET - returns aggregated check-in state for a match slug.
 * Usage: ?slug=2026_03_22_carabao_cup_final_arsenal_v_mancity
 * Returns: { "guest_id_1": { "count": 2, "times": ["21:04", "21:05"] }, ... }
 */
function doGet(e) {
  var slug = (e && e.parameter && e.parameter.slug) || '';
  var type = (e && e.parameter && e.parameter.type) || 'checkins';

  var result;
  if (type === 'guests') {
    result = getGuests(slug);
  } else {
    result = aggregateLog(slug);
  }

  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Read guests from Guests sheet for a match slug.
 * Returns: { "guest_id": { name, email, phone, amount, quantity, status, screenings } }
 */
function getGuests(slug) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(GUESTS_SHEET);
  if (!sheet) {
    sheet = ss.insertSheet(GUESTS_SHEET);
    sheet.appendRow(['guest_id', 'name', 'email', 'phone', 'amount', 'quantity', 'status', 'screenings', 'match_slug']);
    return {};
  }

  var data = sheet.getDataRange().getValues();
  var result = {};

  // Headers: guest_id | name | email | phone | amount | quantity | status | screenings | match_slug
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var rowSlug = String(row[8]);
    if (slug && rowSlug !== slug) continue;

    var guestId = String(row[0]);
    result[guestId] = {
      name: String(row[1]),
      email: String(row[2]),
      phone: String(row[3]),
      amount: Number(row[4]) || 0,
      quantity: Number(row[5]) || 1,
      status: String(row[6]),
      screenings: Number(row[7]) || 0
    };
  }

  return result;
}

/**
 * POST - accepts check-in events and appends to log.
 * Body: { "events": [{ "slug", "guest_id", "count", "device_id", "action", "time" }] }
 * action: "checkin" or "reset"
 */
function doPost(e) {
  var body = JSON.parse(e.postData.contents);
  var events = body.events || [];
  var sheet = getLogSheet();
  var slug = '';

  for (var i = 0; i < events.length; i++) {
    var ev = events[i];
    slug = ev.slug || slug;

    if (ev.action === 'reset') {
      clearSlug(sheet, ev.slug);
      continue;
    }

    sheet.appendRow([
      new Date().toISOString(),
      ev.slug || '',
      ev.guest_id || '',
      ev.count || 1,
      ev.device_id || '',
      ev.action || 'checkin'
    ]);
  }

  var result = aggregateLog(slug);
  return ContentService
    .createTextOutput(JSON.stringify(result))
    .setMimeType(ContentService.MimeType.JSON);
}

/**
 * Aggregate log rows for a slug into { guest_id: { count, times } }
 */
function aggregateLog(slug) {
  var sheet = getLogSheet();
  var data = sheet.getDataRange().getValues();
  var result = {};

  // Skip header row
  for (var i = 1; i < data.length; i++) {
    var row = data[i];
    var rowSlug = String(row[1]);
    var guestId = String(row[2]);
    var count = Number(row[3]) || 0;
    var timestamp = row[0];
    var action = String(row[5]);

    if (rowSlug !== slug) continue;
    if (action === 'reset') continue;

    if (!result[guestId]) {
      result[guestId] = { count: 0, times: [] };
    }
    result[guestId].count += count;

    // Format time from timestamp
    var d = new Date(timestamp);
    var hrs = String(d.getHours()).replace(/^(\d)$/, '0$1');
    var mins = String(d.getMinutes()).replace(/^(\d)$/, '0$1');
    result[guestId].times.push(hrs + ':' + mins);
  }

  return result;
}

/**
 * Delete all log rows for a given slug (used by reset action)
 */
function clearSlug(sheet, slug) {
  var data = sheet.getDataRange().getValues();
  // Delete from bottom up to avoid index shifting
  for (var i = data.length - 1; i >= 1; i--) {
    if (String(data[i][1]) === slug) {
      sheet.deleteRow(i + 1);
    }
  }
}
