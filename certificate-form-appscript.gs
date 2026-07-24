/**
 * Bind this script to the certificate Google Form (Extensions > Apps Script).
 * On every submit it calls the backend, which checks attendance and emails
 * the certificate — this script does no PDF work itself.
 *
 * One-time setup:
 *   1. Extensions > Apps Script, paste this file's contents into Code.gs.
 *   2. Project Settings > Script Properties, add:
 *        BACKEND_URL  = https://<your-vercel-app>.vercel.app/api/certificate/send
 *        CERT_SECRET  = <same value as CERT_SECRET in the backend's env>
 *   3. Triggers (clock icon, left sidebar) > Add Trigger:
 *        Function: onFormSubmit
 *        Event source: From form
 *        Event type: On form submit
 *      (Installable trigger — required for UrlFetchApp to run on submit.)
 *   4. Update EMAIL_QUESTION_TITLE below to match your form's email question
 *      exactly (case-sensitive).
 */

var EMAIL_QUESTION_TITLE = "Email"; // must match your form's email question title exactly

function onFormSubmit(e) {
  if (!e || (!e.response && !e.namedValues)) {
    // No usable event object — this is a manual "Run" from the editor, not a
    // real submission (Apps Script passes {} for those). Nothing to do.
    console.log("onFormSubmit called without a form-submit event — skipping (manual test run?).");
    return;
  }

  var props = PropertiesService.getScriptProperties();
  var backendUrl = props.getProperty("BACKEND_URL");
  var certSecret = props.getProperty("CERT_SECRET");

  if (!backendUrl || !certSecret) {
    console.error("Missing BACKEND_URL or CERT_SECRET script property.");
    return;
  }

  var email = extractEmail(e);
  if (!email) {
    console.error("Could not find an email in this form response.");
    return;
  }

  var response = UrlFetchApp.fetch(backendUrl, {
    method: "post",
    contentType: "application/json",
    headers: { "x-cert-secret": certSecret },
    payload: JSON.stringify({ email: email }),
    muteHttpExceptions: true,
  });

  var code = response.getResponseCode();
  var body = response.getContentText();
  console.log("certificate/send -> " + code + " " + body);

  if (code >= 400) {
    logFailure(email, code, body);
  } else {
    try {
      var parsed = JSON.parse(body);
      if (parsed.status && parsed.status !== "sent") {
        logFailure(email, code, body); // not_attended / not_registered / already_sent
      }
    } catch (err) {
      logFailure(email, code, "unparseable response: " + body);
    }
  }
}

function extractEmail(e) {
  // Form-bound trigger (script created via the Form's own Extensions > Apps
  // Script) — this is the expected setup.
  if (e.response) {
    var items = e.response.getItemResponses();
    for (var i = 0; i < items.length; i++) {
      var title = items[i].getItem().getTitle();
      if (title === EMAIL_QUESTION_TITLE) {
        return items[i].getResponse();
      }
    }
    return null;
  }

  // Sheet-bound trigger (script created via the linked response
  // spreadsheet's Extensions > Apps Script instead) — different event shape.
  if (e.namedValues) {
    var values = e.namedValues[EMAIL_QUESTION_TITLE];
    return values && values[0] ? values[0] : null;
  }

  return null;
}

function logFailure(email, code, body) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet()
    ? SpreadsheetApp.getActiveSpreadsheet().getSheetByName("cert-failures") ||
      SpreadsheetApp.getActiveSpreadsheet().insertSheet("cert-failures")
    : null;
  if (sheet) {
    sheet.appendRow([new Date(), email, code, body]);
  }
}
