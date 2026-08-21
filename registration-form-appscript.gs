/**
 * Google Apps Script — "on form submit" trigger for the Scholarships Session
 * responses sheet. Posts each new registration to the mailQR-ScholarX API.
 *
 * Columns are resolved by HEADER TEXT, not by position. Google Forms
 * renumbers `e.values` whenever a question is added, removed or reordered,
 * so hard-coded indices silently start writing the wrong answer into the
 * wrong database field.
 */

const API_URL = "https://mail-qr-scholar-x.vercel.app/api/register";

// Substring matched (case-insensitive) against the sheet's header row.
const COLUMNS = {
  full_name:   "full name",
  email:       "email",
  phone:       "phone",
  governorate: "governorate",
  university:  "university name",
  college:     "collage name",   // matches the form's spelling
  highschool:  "high school",
};

// Columns we cannot run without. A form edit that drops one of these should
// fail loudly instead of posting half a registration.
const REQUIRED = ["full_name", "email", "phone", "governorate"];

function normalize(value) {
  return String(value == null ? "" : value).replace(/\s+/g, " ").trim().toLowerCase();
}

/** Map each logical field onto its column index in the header row. */
function resolveColumns(headers) {
  const normalized = headers.map(normalize);
  const index = {};
  const missing = [];

  Object.keys(COLUMNS).forEach(function (field) {
    const needle = COLUMNS[field];
    const found = normalized.indexOf(needle) !== -1
      ? normalized.indexOf(needle)
      : indexOfContaining(normalized, needle);
    if (found === -1) {
      missing.push(field + ' ("' + needle + '")');
    } else {
      index[field] = found;
    }
  });

  const fatal = missing.filter(function (m) {
    return REQUIRED.indexOf(m.split(" ")[0]) !== -1;
  });
  if (fatal.length) {
    throw new Error(
      "Form columns changed — cannot find: " + fatal.join(", ") +
      ". Headers seen: " + headers.join(" | ")
    );
  }
  if (missing.length) {
    Logger.log("Optional columns not found: " + missing.join(", "));
  }
  return index;
}

function indexOfContaining(normalizedHeaders, needle) {
  for (var i = 0; i < normalizedHeaders.length; i++) {
    if (normalizedHeaders[i].indexOf(needle) !== -1) return i;
  }
  return -1;
}

function onFormSubmit(e) {
  if (!e || !e.values) {
    Logger.log("No event object — make sure the trigger is 'On form submit' from the spreadsheet.");
    return;
  }

  const sheet = e.range ? e.range.getSheet() : SpreadsheetApp.getActiveSheet();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const col = resolveColumns(headers);

  const row = e.values;
  // e.values drops trailing blanks, so index into it defensively.
  function value(field) {
    const i = col[field];
    if (i === undefined || i >= row.length) return "";
    return String(row[i] == null ? "" : row[i]).trim();
  }

  const university = value("university");
  const college    = value("college");
  const highschool = value("highschool");

  // The form branches: university students answer university + college,
  // school students answer high school, professionals answer neither.
  var institution = "";
  if (university) {
    institution = college ? university + " - " + college : university;
  } else if (highschool) {
    institution = highschool;
  }

  const payload = {
    full_name:   value("full_name"),
    email:       value("email"),
    phone:       value("phone"),
    governorate: value("governorate"),
    institution: institution,
  };
  // National ID is no longer asked on this form; the API treats it as optional.

  if (!payload.full_name && !payload.email) {
    Logger.log("Empty submission — skipping.");
    return;
  }

  try {
    const options = {
      method: "post",
      contentType: "application/json",
      payload: JSON.stringify(payload),
      muteHttpExceptions: true,
    };

    const response = UrlFetchApp.fetch(API_URL, options);
    const text = response.getContentText();
    Logger.log("Raw response: " + text);

    const result = JSON.parse(text);
    Logger.log("Status: " + result.status);
    Logger.log("Message: " + result.message);
  } catch (err) {
    Logger.log("Error calling API: " + err.message);
  }
}

/**
 * Run manually after any form edit. Prints the resolved mapping and a dry-run
 * payload built from the most recent response, without calling the API.
 */
function verifyMapping() {
  const sheet = SpreadsheetApp.getActiveSheet();
  const headers = sheet.getRange(1, 1, 1, sheet.getLastColumn()).getValues()[0];
  const col = resolveColumns(headers);
  Object.keys(col).forEach(function (field) {
    Logger.log(field + " -> [" + col[field] + "] " + headers[col[field]]);
  });

  const last = sheet.getLastRow();
  if (last < 2) {
    Logger.log("No responses to dry-run.");
    return;
  }
  const values = sheet.getRange(last, 1, 1, sheet.getLastColumn()).getValues()[0];
  onFormSubmitDryRun(values, col);
}

function onFormSubmitDryRun(row, col) {
  function value(field) {
    const i = col[field];
    if (i === undefined || i >= row.length) return "";
    return String(row[i] == null ? "" : row[i]).trim();
  }
  const university = value("university");
  const college    = value("college");
  const highschool = value("highschool");
  var institution = university
    ? (college ? university + " - " + college : university)
    : highschool;
  Logger.log("Dry-run payload: " + JSON.stringify({
    full_name:   value("full_name"),
    email:       value("email"),
    phone:       value("phone"),
    governorate: value("governorate"),
    institution: institution,
  }, null, 2));
}
