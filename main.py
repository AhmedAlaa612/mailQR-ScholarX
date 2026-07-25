from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
import qrcode
from PIL import Image, ImageDraw
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer
import io, os, re, uuid, smtplib, ssl, base64, time, itertools
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.application import MIMEApplication
from supabase import create_client, Client
from dotenv import load_dotenv
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
import arabic_reshaper
from bidi.algorithm import get_display

load_dotenv()

def env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1].strip()
    return value

def env_bool(name: str, default: bool) -> bool:
    raw = env_str(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}

# ── Supabase ──────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Config ────────────────────────────────────────────────────
LOGO_PATH    = env_str("LOGO_PATH", "logo.jpg")
SMTP_HOST    = env_str("SMTP_HOST")
SMTP_PORT    = int(env_str("SMTP_PORT", "465") or "465")
SMTP_USER    = env_str("SMTP_USER")
SMTP_PASS    = env_str("SMTP_PASS")
SMTP_FROM    = env_str("SMTP_FROM")
FROM_NAME    = env_str("FROM_NAME", "ScholarX") or "ScholarX"
SMTP_TIMEOUT = float(env_str("SMTP_TIMEOUT", "20") or "20")
SMTP_SECURE  = env_bool("SMTP_SECURE", SMTP_PORT == 465)
EVENT_ID     = env_str("EVENT_ID")   # V1 event uuid from Supabase
ADMIN_KEY    = env_str("ADMIN_KEY")

# ── Certificates ──────────────────────────────────────────────
CERT_SECRET       = env_str("CERT_SECRET")               # shared secret with the Apps Script
CERT_TEMPLATE_PATH = env_str("CERT_TEMPLATE_PATH", "certificate_template.pdf")
CERT_FONT_PATH     = env_str("CERT_FONT_PATH", "NotoSansArabic-Regular.ttf")
CERT_FONT_NAME     = "NotoSansArabic"
CERT_NAME_X        = env_str("CERT_NAME_X")              # None = horizontally centered
CERT_NAME_Y        = float(env_str("CERT_NAME_Y", "340") or "340")
CERT_FONT_SIZE     = float(env_str("CERT_FONT_SIZE", "30") or "30")

if os.path.exists(CERT_FONT_PATH):
    pdfmetrics.registerFont(TTFont(CERT_FONT_NAME, CERT_FONT_PATH))

app = FastAPI(title="ScholarX Registration API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Input schema ──────────────────────────────────────────────
class RegistrationPayload(BaseModel):
    full_name:   str
    email:       str
    phone:       str
    national_id: Optional[str] = None
    institution: Optional[str] = None
    governorate: Optional[str] = None

# ── Cleaners ──────────────────────────────────────────────────

EMAIL_DOMAIN_FIXES = {
    "gamil.com": "gmail.com", "gimail.com": "gmail.com", "gmai.com": "gmail.com",
    "gmial.com": "gmail.com", "gmil.com": "gmail.com", "gmal.com": "gmail.com",
    "gemail.com": "gmail.com", "gmaill.com": "gmail.com", "gmail.co": "gmail.com",
    "gmail.cm": "gmail.com",
    "yhoo.com": "yahoo.com", "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com",
    "yahoo.co": "yahoo.com",
    "hotmial.com": "hotmail.com", "hotmal.com": "hotmail.com",
    "outloook.com": "outlook.com", "outlok.com": "outlook.com",
}

def clean_email(raw: str) -> str:
    email = raw.strip().lower()
    for bad, good in {".con": ".com", ".cpm": ".com", ".ocm": ".com"}.items():
        if email.endswith(bad):
            email = email[:-len(bad)] + good
    if "@" in email:
        local, domain = email.rsplit("@", 1)
        domain = EMAIL_DOMAIN_FIXES.get(domain, domain)
        email = f"{local}@{domain}"
    return email

def clean_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw.strip())
    if len(digits) == 13 and digits.startswith("2001"):
        digits = "20" + digits[3:]
    if len(digits) == 11 and digits.startswith("01"):
        digits = "20" + digits[1:]
    if len(digits) == 10 and digits.startswith("1"):
        digits = "20" + digits
    return digits if len(digits) == 12 and digits.startswith("20") else None

def clean_national_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw.strip())
    return digits if len(digits) == 14 else None

def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    first = parts[0].capitalize() if parts else ""
    last  = " ".join(p.capitalize() for p in parts[1:]) if len(parts) > 1 else ""
    return first, last

# ── DB ────────────────────────────────────────────────────────

def upsert_participant(first_name: str, last_name: str, email: str,
                       phone: Optional[str], national_id: Optional[str],
                       institution: Optional[str], governorate: Optional[str]) -> str:
    """Upsert participant by email. Returns participant id."""
    data = {
        "first_name":  first_name,
        "last_name":   last_name,
        "email":       email,
        "phone":       phone,
        "national_id": national_id,
        "affiliation": institution,
        "city":        governorate,
    }
    # Remove None values so we don't overwrite existing data with nulls
    data = {k: v for k, v in data.items() if v is not None}

    existing = supabase.table("participants").select("id").eq("email", email).execute()
    if existing.data:
        participant_id = existing.data[0]["id"]
        supabase.table("participants").update(data).eq("id", participant_id).execute()
    else:
        data["email"] = email  # ensure email is always set on insert
        res = supabase.table("participants").insert(data).execute()
        participant_id = res.data[0]["id"]

    return participant_id

def create_event_participant(participant_id: str, event_id: str, source: str = "google_form") -> str:
    """Insert event_participant row, returning existing id if already registered."""
    res = supabase.table("event_participants").upsert({
        "id":             str(uuid.uuid4()),
        "event_id":       event_id,
        "participant_id": participant_id,
        "role":           "attendee",
        "status":         "registered",
        "source":         source,
        "qr_sent":        False,
    }, on_conflict="event_id,participant_id", ignore_duplicates=True).execute()

    if res.data:
        return res.data[0]["id"]

    # Already existed — fetch the existing ep_id
    existing = (
        supabase.table("event_participants")
        .select("id")
        .eq("event_id", event_id)
        .eq("participant_id", participant_id)
        .single()
        .execute()
    )
    return existing.data["id"]

def mark_qr_sent(ep_id: str):
    supabase.table("event_participants").update({"qr_sent": True}).eq("id", ep_id).execute()

# ── QR Generator ──────────────────────────────────────────────

def make_rounded_logo(path: str, corner_radius_ratio: float = 0.2) -> Image.Image:
    logo = Image.open(path).convert("RGBA")
    mask = Image.new("L", logo.size, 0)
    draw = ImageDraw.Draw(mask)
    radius = max(1, int(min(logo.size) * corner_radius_ratio))
    draw.rounded_rectangle((0, 0, logo.size[0], logo.size[1]), radius=radius, fill=255)
    logo.putalpha(mask)
    return logo

def generate_qr_bytes(ep_id: str, first_name: str, last_name: str,
                      email: str, phone: Optional[str]) -> bytes:
    # QR encodes: event_participant_id, full_name, email, phone
    data = f"{ep_id},{first_name} {last_name},{email},{phone or ''}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=5,
    )
    qr.add_data(data)
    qr.make(fit=True)

    img = None
    if os.path.exists(LOGO_PATH):
        try:
            rounded_logo = make_rounded_logo(LOGO_PATH)
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name
            rounded_logo.save(tmp_path)
            img = qr.make_image(
                image_factory=StyledPilImage,
                module_drawer=RoundedModuleDrawer(),
                embeded_image_path=tmp_path,
                embedded_image_ratio=0.25,
            )
            os.unlink(tmp_path)
        except Exception as e:
            print(f"Logo embedding failed: {e}")
            img = None

    if img is None:
        img = qr.make_image(fill_color="black", back_color="white")

    out = io.BytesIO()
    pil_img = img.get_image() if hasattr(img, "get_image") else img
    pil_img.save(out, format="PNG")
    return out.getvalue()

# ── Email ─────────────────────────────────────────────────────

def smtp_send_message(msg: MIMEMultipart, to_email: str):
    if not SMTP_HOST or not SMTP_USER or not SMTP_PASS:
        raise RuntimeError("Missing SMTP config")

    if SMTP_SECURE:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        return

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
        server.ehlo()
        server.starttls(context=ssl.create_default_context())
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())

def send_email(to_email: str, first_name: str, qr_bytes: bytes):
    body = (
        f"Hey {first_name}, thanks for registering for the Next Scholar Summit!\n\n"
        f"Your QR code is attached — keep it ready at the event entrance.\n\n"
        f"Don't forget to join the event's WhatsApp group for updates and announcements:\n"
        f"https://chat.whatsapp.com/ISiThrqvB4b5jW868Bg9Vh\n\n\n"
        f"See you there,\n"
        f"ScholarX Team"
    )

    msg = MIMEMultipart()
    msg["From"]    = SMTP_FROM or f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to_email
    msg["Subject"] = "Your QR Code Ticket — Next Scholar Summit"
    msg.attach(MIMEText(body, "plain"))

    img_part = MIMEImage(qr_bytes, name="ticket.png")
    img_part.add_header("Content-Disposition", "attachment", filename="ticket.png")
    msg.attach(img_part)

    smtp_send_message(msg, to_email)

def send_certificate_email(to_email: str, first_name: str, cert_bytes: bytes):
    body = (
        f"Dear {first_name},\n\n"
        "Thank you for attending the Next Scholar Summit, organized by ScholarX as part of "
        "our mission to empower youth and expand access to scholarship opportunities.\n\n"
        "Please find your Certificate of Attendance attached to this email.\n\n"
        "We wish you continued success in your academic and professional journey.\n\n"
        "Warm regards,\n"
        "ScholarX Team"
    )

    msg = MIMEMultipart()
    msg["From"]    = SMTP_FROM or f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to_email
    msg["Subject"] = "Your Certificate — Next Scholar Summit"
    msg.attach(MIMEText(body, "plain"))

    pdf_part = MIMEApplication(cert_bytes, _subtype="pdf")
    pdf_part.add_header("Content-Disposition", "attachment", filename="certificate.pdf")
    msg.attach(pdf_part)

    smtp_send_message(msg, to_email)

# ── Certificate Generator ────────────────────────────────────

def is_arabic(text: str) -> bool:
    return bool(re.search(r'[؀-ۿ]', text))

def _cert_overlay(name: str, page_width: float, page_height: float) -> PdfReader:
    if is_arabic(name):
        name = get_display(arabic_reshaper.reshape(name))

    x = page_width / 2 if not CERT_NAME_X else float(CERT_NAME_X)

    packet = io.BytesIO()
    c = pdf_canvas.Canvas(packet, pagesize=(page_width, page_height))
    c.setFont(CERT_FONT_NAME, CERT_FONT_SIZE)
    c.drawCentredString(x, CERT_NAME_Y, name)
    c.save()
    packet.seek(0)
    return PdfReader(packet)

def generate_certificate_bytes(name: str) -> bytes:
    """Overlay `name` onto the certificate template and return PDF bytes."""
    template = PdfReader(CERT_TEMPLATE_PATH)
    page = template.pages[0]

    overlay = _cert_overlay(name, page.mediabox.width, page.mediabox.height)
    page.merge_page(overlay.pages[0])

    writer = PdfWriter()
    writer.add_page(page)

    out = io.BytesIO()
    writer.write(out)
    out.seek(0)
    return out.getvalue()

def mark_cert_sent(ep_id: str):
    supabase.table("event_participants").update({"cert_sent": True}).eq("id", ep_id).execute()

def has_attended(ep_id: str) -> bool:
    rows = (supabase.table("checkins")
        .select("id")
        .eq("event_id", EVENT_ID)
        .eq("event_participant_id", ep_id)
        .limit(1)
        .execute().data)
    return bool(rows)

def log_certificate_request(email: str):
    """Records that the certificate form was submitted for this email —
    powers the admin 'form responses' view. Best-effort: never blocks the
    actual send if the table is missing or the insert fails."""
    try:
        supabase.table("certificate_requests").upsert(
            {"email": email, "last_seen_at": datetime.now(timezone.utc).isoformat()},
            on_conflict="email",
        ).execute()
    except Exception as e:
        print(f"certificate_requests log failed for {email}: {e}")

def send_certificate_now(ep_id: str, email: str, first_name: str, full_name: str):
    """Generate + email the certificate and mark it sent. Raises on failure."""
    cert_bytes = generate_certificate_bytes(full_name)
    send_certificate_email(email, first_name or full_name, cert_bytes)
    mark_cert_sent(ep_id)

def require_cert_secret(x_cert_secret: Optional[str] = Header(None)):
    if not CERT_SECRET or x_cert_secret != CERT_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ── Endpoints ─────────────────────────────────────────────────

@app.post("/api/register")
async def register(payload: RegistrationPayload):
    if not EVENT_ID:
        return {"status": "error", "message": "EVENT_ID not configured"}

    # Clean inputs
    email       = clean_email(payload.email)
    phone       = clean_phone(payload.phone)
    national_id = clean_national_id(payload.national_id)
    first_name, last_name = split_name(payload.full_name)
    institution = payload.institution.strip() if payload.institution else None
    governorate = payload.governorate.strip() if payload.governorate else None

    # Upsert participant
    participant_id = upsert_participant(
        first_name, last_name, email, phone, national_id, institution, governorate
    )

    # Create event_participant row for this registration
    ep_id = create_event_participant(participant_id, EVENT_ID)

    # Generate QR
    qr_bytes = generate_qr_bytes(ep_id, first_name, last_name, email, phone)

    # Send email
    try:
        send_email(email, first_name, qr_bytes)
        mark_qr_sent(ep_id)
        email_status = "sent"
    except Exception as e:
        email_status = f"failed: {e}"

    return {
        "status":  "registered",
        "message": f"Registered. Email: {email_status}",
        "id":      ep_id,
        "cleaned": {
            "first_name":  first_name,
            "last_name":   last_name,
            "email":       email,
            "phone":       phone,
            "national_id": national_id,
        }
    }

@app.get("/health")
def health():
    return {"status": "ok"}


class TrackPayload(BaseModel):
    type: str = "download"  # download | share_facebook | share_instagram | share_linkedin | share_whatsapp | copy_linkedin | copy_social
    session_id: Optional[str] = None

@app.post("/api/card/track")
def card_track(payload: TrackPayload = None):
    """Called by the card generator app on each action. No auth required."""
    event_type = (payload.type if payload else None) or "download"
    session_id = (payload.session_id if payload else None) or None
    supabase.table("card_downloads").insert({"type": event_type, "session_id": session_id}).execute()
    return {"status": "ok"}

# ── Ticket self-service (public) ──────────────────────────────

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# Best-effort in-memory rate limit. Vercel serverless = per-instance only,
# but it's enough to stop casual phone→name enumeration from one client.
_rate_hits: dict = {}
RATE_LIMIT  = 15     # requests
RATE_WINDOW = 60     # seconds

def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"

def check_rate_limit(request: Request):
    ip  = _client_ip(request)
    now = time.time()
    hits = [t for t in _rate_hits.get(ip, []) if now - t < RATE_WINDOW]
    if len(hits) >= RATE_LIMIT:
        raise HTTPException(429, "Too many attempts — wait a minute and try again")
    hits.append(now)
    _rate_hits[ip] = hits

def _full_name(p: dict) -> str:
    return f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip()

def _batch_in_filter(table, column: str, values: list, select: str = "*",
                     eq: Optional[tuple] = None, batch_size: int = 100) -> list:
    """Run a select with .in_(column, chunk) in batches to avoid URL length limits."""
    if not values:
        return []
    it = iter(values)
    results = []
    while chunk := list(itertools.islice(it, batch_size)):
        q = table.select(select).in_(column, chunk)
        if eq:
            q = q.eq(eq[0], eq[1])
        results.extend(q.execute().data)
    return results


def _qr_b64(ep_id: str, p: dict) -> str:
    qr = generate_qr_bytes(ep_id, p.get("first_name") or "", p.get("last_name") or "",
                           p.get("email") or "", p.get("phone"))
    return base64.b64encode(qr).decode()

def _eps_for_participants(participant_ids: list) -> list:
    """event_participants rows for this event, newest first."""
    rows = _batch_in_filter(
        supabase.table("event_participants"),
        "participant_id", participant_ids,
        select="id, participant_id, registered_at",
        eq=("event_id", EVENT_ID),
    )
    rows.sort(key=lambda r: r.get("registered_at") or "", reverse=True)
    return rows


class TicketLookup(BaseModel):
    email: Optional[str] = None
    phone: Optional[str] = None


@app.post("/api/ticket/lookup")
def ticket_lookup(body: TicketLookup, request: Request):
    """Public. Email hit → returns the QR directly. Phone hit → returns up
    to 2 name candidates (never the QR — that comes from /claim after the
    person confirms their name and their email)."""
    check_rate_limit(request)
    if not EVENT_ID:
        raise HTTPException(500, "EVENT_ID not configured")

    # ── Email path ──
    if body.email and body.email.strip():
        email = clean_email(body.email)
        parts = (supabase.table("participants")
            .select("id, first_name, last_name, email, phone")
            .eq("email", email).execute().data)
        if parts:
            eps = _eps_for_participants([p["id"] for p in parts])
            if eps:
                ep  = eps[0]
                p   = next(x for x in parts if x["id"] == ep["participant_id"])
                return {"status": "found", "name": _full_name(p), "qr": _qr_b64(ep["id"], p)}
        return {"status": "not_found"}

    # ── Phone path ──
    if body.phone and body.phone.strip():
        phone = clean_phone(body.phone)
        if not phone:
            return {"status": "not_found"}
        parts = (supabase.table("participants")
            .select("id, first_name, last_name, email, phone")
            .eq("phone", phone).execute().data)
        if not parts:
            return {"status": "not_found"}
        eps = _eps_for_participants([p["id"] for p in parts])
        if not eps:
            return {"status": "not_found"}
        pmap = {p["id"]: p for p in parts}
        candidates = [
            {"ep_id": ep["id"], "name": _full_name(pmap.get(ep["participant_id"], {}))}
            for ep in eps[:2]
        ]
        return {"status": "candidates", "candidates": candidates}

    raise HTTPException(422, "Provide email or phone")


class TicketClaim(BaseModel):
    ep_id: str
    phone: str
    email: str


@app.post("/api/ticket/claim")
def ticket_claim(body: TicketClaim, request: Request):
    """Public. Reached from the phone path after name confirmation. The
    phone must match the stored one (a leaked ep_id alone gets nothing).
    Corrects the email if changed, re-sends the ticket, returns the QR."""
    check_rate_limit(request)
    if not EVENT_ID:
        raise HTTPException(500, "EVENT_ID not configured")

    ep_rows = (supabase.table("event_participants")
        .select("id, participant_id, event_id")
        .eq("id", body.ep_id).limit(1).execute().data)
    if not ep_rows or ep_rows[0].get("event_id") != EVENT_ID:
        raise HTTPException(404, "Registration not found")
    pid = ep_rows[0]["participant_id"]

    p_rows = (supabase.table("participants")
        .select("id, first_name, last_name, email, phone")
        .eq("id", pid).limit(1).execute().data)
    if not p_rows:
        raise HTTPException(404, "Participant not found")
    d = p_rows[0]

    # Verify the phone — proves this claim came from a real phone lookup.
    phone = clean_phone(body.phone)
    if not phone or phone != d.get("phone"):
        raise HTTPException(403, "Phone does not match this registration")

    new_email = clean_email(body.email)
    if not EMAIL_RE.match(new_email):
        raise HTTPException(422, "Invalid email")

    first = d.get("first_name") or ""

    # Correct the email if it changed, keeping an audit trail.
    if new_email != (d.get("email") or ""):
        try:
            supabase.table("email_changes").insert({
                "participant_id": pid,
                "old_email": d.get("email"),
                "new_email": new_email,
            }).execute()
        except Exception as e:
            print(f"email_changes log failed: {e}")   # never blocks the claim
        supabase.table("participants").update({"email": new_email}).eq("id", pid).execute()
        d["email"] = new_email

    qr_bytes   = generate_qr_bytes(body.ep_id, first, d.get("last_name") or "", new_email, phone)
    email_sent = False
    try:
        send_email(new_email, first, qr_bytes)
        mark_qr_sent(body.ep_id)
        email_sent = True
    except Exception as e:
        print(f"ticket claim email failed for {new_email}: {e}")   # QR still returned

    return {
        "status": "ok",
        "name": _full_name(d),
        "qr": base64.b64encode(qr_bytes).decode(),
        "email_sent": email_sent,
    }

# ── Certificate self-service (called from the Apps Script) ─────

class CertificateSend(BaseModel):
    email: str


@app.post("/api/certificate/send")
def certificate_send(body: CertificateSend, _=Depends(require_cert_secret)):
    """Called by the certificate Google Form's Apps Script on every submit.
    Sends the certificate only if the email is a registered participant of
    this event AND has at least one checkin recorded (i.e. actually attended).
    Idempotent — repeat submissions from the same email won't re-send."""
    if not EVENT_ID:
        raise HTTPException(500, "EVENT_ID not configured")

    email = clean_email(body.email)
    log_certificate_request(email)

    parts = (supabase.table("participants")
        .select("id, first_name, last_name, email")
        .eq("email", email).limit(1).execute().data)
    if not parts:
        return {"status": "not_registered"}
    p = parts[0]

    ep_rows = (supabase.table("event_participants")
        .select("id, cert_sent")
        .eq("event_id", EVENT_ID)
        .eq("participant_id", p["id"])
        .limit(1).execute().data)
    if not ep_rows:
        return {"status": "not_registered"}
    ep = ep_rows[0]

    if not has_attended(ep["id"]):
        return {"status": "not_attended"}

    if ep.get("cert_sent"):
        return {"status": "already_sent"}

    full_name = _full_name(p)
    try:
        send_certificate_now(ep["id"], email, p.get("first_name"), full_name)
    except Exception as e:
        raise HTTPException(500, f"certificate send failed: {e}")

    return {"status": "sent", "name": full_name}

# ── Admin auth ────────────────────────────────────────────────

def require_admin(x_admin_key: Optional[str] = Header(None)):
    if not ADMIN_KEY or x_admin_key != ADMIN_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")

# ── Admin endpoints ───────────────────────────────────────────

@app.get("/api/admin/stats")
def admin_stats(_=Depends(require_admin)):
    rows = (
        supabase.table("event_participants")
        .select("id, qr_sent, registered_at, participants(city)")
        .eq("event_id", EVENT_ID)
        .limit(10000)
        .execute()
        .data
    )

    total   = len(rows)
    sent    = sum(1 for r in rows if r.get("qr_sent"))
    pending = total - sent

    city_counts: dict = {}
    for r in rows:
        city = ((r.get("participants") or {}).get("city") or "Unknown").strip() or "Unknown"
        city_counts[city] = city_counts.get(city, 0) + 1

    by_city = sorted(
        [{"city": k, "count": v} for k, v in city_counts.items()],
        key=lambda x: -x["count"]
    )

    day_counts: dict = {}
    for r in rows:
        day = (r.get("registered_at") or "")[:10]
        if day:
            day_counts[day] = day_counts.get(day, 0) + 1

    by_day = [
        {"date": k, "count": v}
        for k, v in sorted(day_counts.items())
    ]

    return {
        "total": total,
        "qr_sent": sent,
        "pending": pending,
        "by_city": by_city,
        "by_day": by_day,
    }


@app.get("/api/admin/card-stats")
def admin_card_stats(_=Depends(require_admin)):
    rows = (
        supabase.table("card_downloads")
        .select("downloaded_at, type, session_id")
        .order("downloaded_at", desc=False)
        .limit(100000)
        .execute()
        .data
    )

    total = len(rows)

    # Build per-session action list and first-seen day
    session_actions: dict[str, list] = {}
    session_first_day: dict[str, str] = {}

    for r in rows:
        sid = r.get("session_id")
        t   = r.get("type") or "download"
        day = (r.get("downloaded_at") or "")[:10]

        if sid:
            if sid not in session_actions:
                session_actions[sid] = []
                if day:
                    session_first_day[sid] = day
            session_actions[sid].append(t)

    unique_sessions = len(session_actions)

    # Sessions that did at least one copy AND at least one share
    copy_and_share = sum(
        1 for actions in session_actions.values()
        if any(a.startswith("copy_") for a in actions)
        and any(a.startswith("share_") for a in actions)
    )

    # Unique sessions per day (keyed by first action date)
    day_counts: dict = {}
    for day in session_first_day.values():
        day_counts[day] = day_counts.get(day, 0) + 1

    # Unique sessions per action type (each session counted once per type)
    type_session_counts: dict = {}
    for actions in session_actions.values():
        for t in set(actions):
            type_session_counts[t] = type_session_counts.get(t, 0) + 1

    by_day  = [{"date": k, "count": v} for k, v in sorted(day_counts.items())]
    by_type = [{"type": k, "count": v} for k, v in sorted(type_session_counts.items(), key=lambda x: -x[1])]

    return {
        "total": total,
        "unique_sessions": unique_sessions,
        "copy_and_share": copy_and_share,
        "by_day": by_day,
        "by_type": by_type,
    }


@app.get("/api/admin/registrations")
def admin_registrations(_=Depends(require_admin)):
    data = (
        supabase.table("event_participants")
        .select("id, qr_sent, registered_at, source, participants(id, first_name, last_name, email, phone, national_id, city, affiliation)")
        .eq("event_id", EVENT_ID)
        .order("registered_at", desc=True)
        .limit(10000)
        .execute()
        .data
    )
    return {"data": data, "count": len(data)}


@app.get("/api/admin/qr/{ep_id}")
def admin_qr(ep_id: str, _=Depends(require_admin)):
    ep = supabase.table("event_participants").select("participant_id").eq("id", ep_id).single().execute()
    if not ep.data:
        raise HTTPException(status_code=404, detail="Registration not found")

    p = supabase.table("participants").select("first_name, last_name, email, phone").eq("id", ep.data["participant_id"]).single().execute()
    if not p.data:
        raise HTTPException(status_code=404, detail="Participant not found")

    d = p.data
    qr_bytes = generate_qr_bytes(
        ep_id,
        d.get("first_name") or "",
        d.get("last_name")  or "",
        d.get("email")      or "",
        d.get("phone"),
    )
    return Response(
        content=qr_bytes,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="ticket-{ep_id[:8]}.png"'},
    )


@app.get("/api/admin/certificate-preview")
def admin_certificate_preview(name: str = "Preview Name", _=Depends(require_admin)):
    """Render the certificate template with a given name, without sending
    anything. Use this to tune CERT_NAME_X/CERT_NAME_Y/CERT_FONT_SIZE against
    the real Canva export before wiring up the form."""
    if not os.path.exists(CERT_TEMPLATE_PATH):
        raise HTTPException(500, f"Template not found at {CERT_TEMPLATE_PATH}")
    cert_bytes = generate_certificate_bytes(name)
    return Response(
        content=cert_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'inline; filename="certificate-preview.pdf"'},
    )


@app.get("/api/admin/certificate-stats")
def admin_certificate_stats(_=Depends(require_admin)):
    """Form-response view: everyone who has submitted the certificate Google
    Form, whether they turned out to have attended, and whether they've
    been sent a certificate."""
    if not EVENT_ID:
        raise HTTPException(500, "EVENT_ID not configured")

    try:
        requests_rows = (supabase.table("certificate_requests")
            .select("email, first_seen_at")
            .order("first_seen_at", desc=True)
            .limit(10000).execute().data)
    except PostgrestAPIError as e:
        raise HTTPException(500, f"certificate_requests table query failed (did you run "
                                 f"the migration?): {getattr(e, 'message', str(e))}")

    emails = [r["email"] for r in requests_rows]
    participants = _batch_in_filter(
        supabase.table("participants"),
        "email", emails,
        select="id, first_name, last_name, email, phone",
    ) if emails else []
    email_to_p = {p["email"]: p for p in participants}

    pids = [p["id"] for p in participants]
    eps = _batch_in_filter(
        supabase.table("event_participants"),
        "participant_id", pids,
        select="id, participant_id, cert_sent",
        eq=("event_id", EVENT_ID),
    ) if pids else []
    pid_to_ep = {e["participant_id"]: e for e in eps}

    ep_ids = [e["id"] for e in eps]
    checkins = _batch_in_filter(
        supabase.table("checkins"),
        "event_participant_id", ep_ids,
        select="event_participant_id",
        eq=("event_id", EVENT_ID),
    ) if ep_ids else []
    attended_ep_ids = {c["event_participant_id"] for c in checkins if c.get("event_participant_id")}

    responses = []
    for r in requests_rows:
        p = email_to_p.get(r["email"])
        ep = pid_to_ep.get(p["id"]) if p else None
        attended = bool(ep and ep["id"] in attended_ep_ids)
        responses.append({
            "email": r["email"],
            "name": _full_name(p) if p else None,
            "ep_id": ep["id"] if ep else None,
            "registered": bool(p),
            "attended": attended,
            "cert_sent": bool(ep and ep.get("cert_sent")),
            "requested_at": r.get("first_seen_at"),
        })

    return {
        "total_responses": len(responses),
        "attended": sum(1 for r in responses if r["attended"]),
        "cert_sent": sum(1 for r in responses if r["cert_sent"]),
        "responses": responses,
    }


@app.get("/api/admin/attendees")
def admin_attendees(_=Depends(require_admin)):
    """Everyone recorded as having attended the event (>=1 checkin), with
    certificate status — independent of whether they filled the form."""
    if not EVENT_ID:
        raise HTTPException(500, "EVENT_ID not configured")

    eps = (supabase.table("event_participants")
        .select("id, cert_sent, participants(id, first_name, last_name, email, phone)")
        .eq("event_id", EVENT_ID)
        .limit(10000).execute().data)

    checkins = (supabase.table("checkins")
        .select("event_participant_id")
        .eq("event_id", EVENT_ID)
        .limit(20000).execute().data)
    attended_ep_ids = {c["event_participant_id"] for c in checkins if c.get("event_participant_id")}

    attendees = []
    for ep in eps:
        if ep["id"] not in attended_ep_ids:
            continue
        p = ep.get("participants") or {}
        attendees.append({
            "ep_id": ep["id"],
            "name": _full_name(p),
            "email": p.get("email"),
            "phone": p.get("phone"),
            "cert_sent": bool(ep.get("cert_sent")),
        })
    attendees.sort(key=lambda a: a["name"] or "")

    return {"data": attendees, "count": len(attendees)}


@app.post("/api/admin/certificate/send/{ep_id}")
def admin_certificate_send(ep_id: str, _=Depends(require_admin)):
    """Manual send/resend, triggered from the admin table. This is an
    explicit admin override — unlike the public /api/certificate/send used
    by the form, it does NOT require a recorded check-in."""
    ep_rows = (supabase.table("event_participants")
        .select("id, participant_id, event_id")
        .eq("id", ep_id).limit(1).execute().data)
    if not ep_rows or ep_rows[0].get("event_id") != EVENT_ID:
        raise HTTPException(404, "Registration not found")

    p_rows = (supabase.table("participants")
        .select("first_name, last_name, email")
        .eq("id", ep_rows[0]["participant_id"]).limit(1).execute().data)
    if not p_rows:
        raise HTTPException(404, "Participant not found")
    d = p_rows[0]

    if not d.get("email"):
        raise HTTPException(422, "This participant has no email on file")

    full_name = _full_name(d)
    try:
        send_certificate_now(ep_id, d["email"], d.get("first_name"), full_name)
    except Exception as e:
        raise HTTPException(500, f"certificate send failed: {e}")

    return {"status": "sent", "name": full_name}


# ── Check-in app additions (developer-plan.md) ───────────────────

from collections import defaultdict
from postgrest.exceptions import APIError as PostgrestAPIError
from supabase_auth.errors import AuthApiError


class CheckpointCreate(BaseModel):
    label: str
    mode: str                      # must be 'gate' or 'room' — reject otherwise (422)


class VolunteerCreate(BaseModel):
    email: str
    password: str
    display_name: str
    phone: Optional[str] = None
    checkpoint_id: Optional[str] = None
    role: str = "scanner"


class VolunteerUpdate(BaseModel):  # PATCH semantics: only provided fields change
    display_name: Optional[str] = None
    phone: Optional[str] = None
    checkpoint_id: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None


class PasswordBody(BaseModel):
    password: str


class SessionCreate(BaseModel):
    checkpoint_id: str
    label: str
    capacity: Optional[int] = None
    carryover: Optional[int] = None  # people who stayed from the previous session


class SessionUpdate(BaseModel):  # PATCH semantics: only provided fields change
    label: Optional[str] = None
    capacity: Optional[int] = None
    carryover: Optional[int] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None  # explicit null re-opens the session


@app.get("/api/admin/checkpoints")
def list_checkpoints(_=Depends(require_admin)):
    return supabase.table("checkpoints").select("*").order("label").execute().data


@app.post("/api/admin/checkpoints")
def create_checkpoint(body: CheckpointCreate, _=Depends(require_admin)):
    if body.mode not in ("gate", "room"):
        raise HTTPException(422, "mode must be 'gate' or 'room'")
    try:
        res = supabase.table("checkpoints").insert(
            {"label": body.label, "mode": body.mode}).execute()
    except PostgrestAPIError as e:
        raise HTTPException(422, getattr(e, "message", str(e)))
    return res.data[0]
# NOTE: no update/delete endpoints for checkpoints. Intentional. Do not add them.


# ── Room sessions ─────────────────────────────────────────────
# A session is a time window over the immutable checkins log. Attribution and
# occupancy are computed by bucketing scans into windows, so editing a window
# (admin forgot to tap start/end) recomputes every count correctly.

def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Timestamps arrive as both '...Z' (phone clocks) and '...+00:00'
    (Postgres) — normalize before comparing, never compare strings."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@app.get("/api/admin/sessions")
def list_sessions(_=Depends(require_admin)):
    try:
        sessions = (supabase.table("sessions").select("*")
                    .order("started_at", desc=True).execute().data)
    except PostgrestAPIError as e:
        # Raise through HTTPException so the response carries CORS headers and a
        # readable cause — an unhandled 500 reaches the browser as a bare CORS
        # error. Most likely cause: migration-sessions.sql not run yet.
        raise HTTPException(500, f"sessions table query failed (did you run "
                                 f"migration-sessions.sql?): {getattr(e, 'message', str(e))}")
    checkpoints = {c["id"]: c for c in
                   supabase.table("checkpoints").select("*").execute().data}
    rows = (
        supabase.table("checkins")
        .select("event_participant_id, checkpoint_id, scanned_at, kind")
        .eq("event_id", EVENT_ID)
        .limit(20000)
        .execute()
        .data
    )

    now = datetime.now(timezone.utc)
    out = []
    for s in sessions:
        start = _parse_ts(s.get("started_at"))
        end = _parse_ts(s.get("ended_at")) or now
        unique_eps = set()
        noid = 0   # ep-less admits (unlisted let-ins, walk-ins): +1 person each
        scans = 0
        if start:
            for r in rows:
                if r.get("checkpoint_id") != s["checkpoint_id"]:
                    continue
                ts = _parse_ts(r.get("scanned_at"))
                if ts is None or ts < start or ts > end:
                    continue
                scans += 1
                ep = r.get("event_participant_id")
                if ep is not None:
                    unique_eps.add(ep)
                elif r.get("kind") in ("qr_unlisted", "walkin"):
                    noid += 1
        cp = checkpoints.get(s["checkpoint_id"], {})
        unique_count = len(unique_eps) + noid
        carryover = s.get("carryover") or 0
        out.append({
            **s,
            "checkpoint_label": cp.get("label"),
            "checkpoint_mode": cp.get("mode"),
            "unique_count": unique_count,       # scanned in during this window
            "carryover": carryover,             # admin estimate: stayed from last session
            "occupancy": carryover + unique_count,
            "scans": scans,
            "open": s.get("ended_at") is None,
        })
    return out


@app.post("/api/admin/sessions")
def create_session(body: SessionCreate, _=Depends(require_admin)):
    cps = (supabase.table("checkpoints").select("*")
           .eq("id", body.checkpoint_id).limit(1).execute().data)
    if not cps:
        raise HTTPException(422, "Unknown checkpoint")
    if cps[0].get("mode") != "room":
        raise HTTPException(422, "Sessions can only run at room checkpoints")
    if not body.label.strip():
        raise HTTPException(422, "Label is required")
    if body.capacity is not None and body.capacity <= 0:
        raise HTTPException(422, "Capacity must be a positive number")
    if body.carryover is not None and body.carryover < 0:
        raise HTTPException(422, "Carryover cannot be negative")

    now_iso = datetime.now(timezone.utc).isoformat()
    # Starting a new session auto-ends any open one at this room — the partial
    # unique index forbids two open sessions, and on event day one tap beats two.
    supabase.table("sessions").update({"ended_at": now_iso}) \
        .eq("checkpoint_id", body.checkpoint_id).is_("ended_at", "null").execute()
    try:
        res = supabase.table("sessions").insert({
            "checkpoint_id": body.checkpoint_id,
            "label": body.label.strip(),
            "capacity": body.capacity,
            "carryover": body.carryover or 0,
            "started_at": now_iso,
        }).execute()
    except PostgrestAPIError as e:
        raise HTTPException(422, getattr(e, "message", str(e)))
    return res.data[0]


@app.post("/api/admin/sessions/{sid}/end")
def end_session(sid: str, _=Depends(require_admin)):
    res = (supabase.table("sessions")
           .update({"ended_at": datetime.now(timezone.utc).isoformat()})
           .eq("id", sid).is_("ended_at", "null").execute())
    if not res.data:
        raise HTTPException(404, "Session not found or already ended")
    return res.data[0]


@app.patch("/api/admin/sessions/{sid}")
def update_session(sid: str, body: SessionUpdate, _=Depends(require_admin)):
    # Recovery hatch: fix a forgotten start/end after the fact and every count
    # recomputes from the scan log. exclude_unset so ended_at=null can re-open.
    updates = body.dict(exclude_unset=True)
    if not updates:
        return {"status": "ok"}
    for k in ("started_at", "ended_at"):
        if k in updates and updates[k] is not None and _parse_ts(updates[k]) is None:
            raise HTTPException(422, f"{k} must be an ISO timestamp")
    if "capacity" in updates and updates["capacity"] is not None and updates["capacity"] <= 0:
        raise HTTPException(422, "Capacity must be a positive number")
    if "carryover" in updates and (updates["carryover"] is None or updates["carryover"] < 0):
        raise HTTPException(422, "Carryover must be zero or more")
    try:
        supabase.table("sessions").update(updates).eq("id", sid).execute()
    except PostgrestAPIError as e:
        raise HTTPException(422, getattr(e, "message", str(e)))
    return {"status": "ok"}


@app.get("/api/admin/volunteers")
def list_volunteers(_=Depends(require_admin)):
    vols = supabase.table("volunteers").select("*").order("display_name").execute().data
    cps  = {c["id"]: c for c in supabase.table("checkpoints").select("*").execute().data}
    for v in vols:
        v["checkpoint"] = cps.get(v["checkpoint_id"])   # {id,label,mode} or None
    return vols


@app.post("/api/admin/volunteers")
def create_volunteer(body: VolunteerCreate, _=Depends(require_admin)):
    phone = None
    if body.phone and body.phone.strip():
        phone = clean_phone(body.phone)
        if not phone:
            raise HTTPException(422, "Invalid phone number")
    try:
        u = supabase.auth.admin.create_user({
            "email": body.email, "password": body.password, "email_confirm": True})
    except AuthApiError as e:
        raise HTTPException(422, getattr(e, "message", str(e)))
    try:
        supabase.table("volunteers").insert({
            "id": u.user.id, "email": body.email, "display_name": body.display_name,
            "phone": phone, "checkpoint_id": body.checkpoint_id, "role": body.role}).execute()
    except PostgrestAPIError as e:
        raise HTTPException(422, getattr(e, "message", str(e)))
    return {"id": u.user.id}


@app.patch("/api/admin/volunteers/{vid}")
def update_volunteer(vid: str, body: VolunteerUpdate, _=Depends(require_admin)):
    # exclude_unset (not exclude_none): a PATCH must be able to explicitly
    # clear checkpoint_id (unassign a gate) by sending null — exclude_none
    # silently dropped that key and the update became a no-op.
    updates = body.dict(exclude_unset=True)
    if "phone" in updates and updates["phone"] and updates["phone"].strip():
        phone = clean_phone(updates["phone"])
        if not phone:
            raise HTTPException(422, "Invalid phone number")
        updates["phone"] = phone
    try:
        supabase.table("volunteers").update(updates).eq("id", vid).execute()
    except PostgrestAPIError as e:
        raise HTTPException(422, getattr(e, "message", str(e)))
    return {"status": "ok"}


@app.post("/api/admin/volunteers/{vid}/password")
def reset_password(vid: str, body: PasswordBody, _=Depends(require_admin)):
    try:
        supabase.auth.admin.update_user_by_id(vid, {"password": body.password})
    except AuthApiError as e:
        raise HTTPException(422, getattr(e, "message", str(e)))
    return {"status": "ok"}


@app.get("/api/admin/checkin-stats")
def checkin_stats(_=Depends(require_admin)):
    if not EVENT_ID:
        return {"status": "error", "message": "EVENT_ID not configured"}

    rows = (
        supabase.table("checkins")
        .select("*")
        .eq("event_id", EVENT_ID)
        .limit(20000)
        .execute()
        .data
    )
    checkpoints = {c["id"]: c for c in supabase.table("checkpoints").select("*").execute().data}
    volunteers  = {v["id"]: v for v in supabase.table("volunteers").select("*").execute().data}

    ep_rows = (
        supabase.table("event_participants")
        .select("id, participants(city)")
        .eq("event_id", EVENT_ID)
        .limit(20000).execute().data
    )
    total_registered = len(ep_rows)
    ep_city = {r["id"]: (r.get("participants") or {}).get("city") for r in ep_rows}
    registered_ids = set(ep_city.keys())

    gate_unique_eps = set()
    walkin_unpromoted = 0
    duplicates = 0
    walkin_count = 0
    search_count = 0

    per_checkpoint_scans: dict = defaultdict(int)
    per_checkpoint_unique: dict = defaultdict(set)
    per_checkpoint_pace: dict = defaultdict(int)
    per_volunteer_scans: dict = defaultdict(int)
    per_volunteer_last_sync: dict = {}
    arrivals_buckets: dict = defaultdict(int)
    walkins_feed = []
    unlisted_feed = []
    unlisted_noid = 0

    pace_threshold = (datetime.now(timezone.utc) - timedelta(minutes=15)) \
        .strftime("%Y-%m-%dT%H:%M:%S")
    pace_15min = 0

    for r in rows:
        cp = checkpoints.get(r.get("checkpoint_id"))
        ep_id = r.get("event_participant_id")
        kind = r.get("kind")

        if kind == "duplicate_override":
            duplicates += 1
        if kind == "walkin":
            walkin_count += 1
            if ep_id is None:
                walkin_unpromoted += 1
        if kind == "search":
            search_count += 1

        if cp and cp.get("mode") == "gate" and ep_id is not None:
            gate_unique_eps.add(ep_id)

        if r.get("checkpoint_id"):
            cpid = r["checkpoint_id"]
            per_checkpoint_scans[cpid] += 1
            per_checkpoint_unique[cpid].add(ep_id)

        vid = r.get("volunteer_id")
        if vid:
            per_volunteer_scans[vid] += 1
            received_at = r.get("received_at")
            if received_at and (vid not in per_volunteer_last_sync
                                 or received_at > per_volunteer_last_sync[vid]):
                per_volunteer_last_sync[vid] = received_at

        if cp and cp.get("mode") == "gate" and r.get("scanned_at"):
            ts = r["scanned_at"]
            # Hourly buckets. scanned_at is UTC; keep the Z so the dashboard
            # parses it as UTC instead of browser-local time.
            bucket_key = f"{ts[:13]}:00:00Z"
            arrivals_buckets[bucket_key] += 1
            if ts[:19] >= pace_threshold:
                pace_15min += 1
                per_checkpoint_pace[r["checkpoint_id"]] += 1

        if kind == "walkin":
            volunteer = volunteers.get(vid)
            walkins_feed.append({
                "name": r.get("walkin_name"),
                "by": volunteer.get("display_name") if volunteer else None,
                "checkpoint": cp.get("label") if cp else None,
                "at": r.get("scanned_at"),
                "promoted": ep_id is not None,
            })

        if kind == "qr_unlisted":
            volunteer = volunteers.get(vid)
            unlisted_feed.append({
                "name": r.get("scanned_name"),
                "by": volunteer.get("display_name") if volunteer else None,
                "checkpoint": cp.get("label") if cp else None,
                "at": r.get("scanned_at"),
                "raw_payload": r.get("raw_payload"),
            })
            # Malformed-QR let-ins carry no ep_id, so the gate-unique sets
            # can't count them; each override is treated as one person.
            if ep_id is None and cp and cp.get("mode") == "gate":
                unlisted_noid += 1

    unique_inside = len(gate_unique_eps) + walkin_unpromoted + unlisted_noid

    # Admission mix: gate uniques split into roster members vs unknown-ticket
    # let-ins (qr_unlisted with an ep_id not in this event, e.g. old-event QRs).
    registered_inside = gate_unique_eps & registered_ids
    unlisted_inside = gate_unique_eps - registered_ids

    city_counts_in: dict = defaultdict(int)
    for ep in registered_inside:
        city = (ep_city.get(ep) or "Unknown").strip() or "Unknown"
        city_counts_in[city] += 1
    checkin_by_city = sorted(
        [{"city": k, "count": v} for k, v in city_counts_in.items()],
        key=lambda x: x["count"], reverse=True,
    )

    per_checkpoint = [
        {
            "checkpoint_id": cpid,
            "label": checkpoints.get(cpid, {}).get("label"),
            "mode": checkpoints.get(cpid, {}).get("mode"),
            "scans": per_checkpoint_scans[cpid],
            "unique": len(per_checkpoint_unique[cpid]),
            "pace_15min": per_checkpoint_pace.get(cpid, 0),
        }
        for cpid in per_checkpoint_scans
    ]

    # All active volunteers, not just those with scans — the heartbeat
    # (volunteers.last_seen_at) tells "device offline" apart from "no new
    # scans"; max(received_at) is the fallback for app builds without it.
    per_volunteer = sorted(
        [
            {
                "volunteer_id": vid,
                "display_name": v.get("display_name"),
                "phone": v.get("phone"),
                "checkpoint_label": checkpoints.get(v.get("checkpoint_id"), {}).get("label"),
                "scans": per_volunteer_scans.get(vid, 0),
                "last_sync": v.get("last_seen_at") or per_volunteer_last_sync.get(vid),
            }
            for vid, v in volunteers.items()
            if v.get("active", True)
        ],
        key=lambda x: x["scans"],
        reverse=True,
    )

    arrivals_curve = [
        {"t": t, "count": c} for t, c in sorted(arrivals_buckets.items())
    ]

    walkins_feed.sort(key=lambda w: w["at"] or "", reverse=True)
    unlisted_feed.sort(key=lambda u: u["at"] or "", reverse=True)

    return {
        "unique_inside": unique_inside,
        "total_scans": len(rows),
        "duplicates": duplicates,
        "walkin_count": walkin_count,
        "search_count": search_count,
        "registered_inside": len(registered_inside),
        "unlisted_inside": len(unlisted_inside) + unlisted_noid,
        "pace_15min": pace_15min,
        "attendance_rate": (unique_inside / total_registered) if total_registered else 0,
        "per_checkpoint": per_checkpoint,
        "per_volunteer": per_volunteer,
        "arrivals_curve": arrivals_curve,
        "walkins": walkins_feed,
        "unlisted": unlisted_feed,
        "checkin_by_city": checkin_by_city,
    }


def promote_walkin(row: dict) -> Optional[str]:
    """Promote one walk-in checkins row to a real participant + send the QR
    email. Returns the new event_participant_id, or None if this row was
    already promoted (idempotent — safe if a webhook delivery repeats)."""
    if row.get("event_participant_id") is not None:
        return None

    email = clean_email(row["walkin_email"] or "")
    phone = clean_phone(row["walkin_phone"] or "")
    nid   = clean_national_id(row["walkin_national_id"])
    first, last = split_name(row["walkin_name"] or "")
    pid   = upsert_participant(first, last, email, phone, nid, None, None)
    ep_id = create_event_participant(pid, EVENT_ID, source="walkin")
    supabase.table("checkins").update(
        {"event_participant_id": ep_id}).eq("id", row["id"]).execute()
    try:
        qr = generate_qr_bytes(ep_id, first, last, email, phone)
        send_email(email, first, qr)
        mark_qr_sent(ep_id)
    except Exception as e:
        print(f"walk-in QR email failed for {email}: {e}")   # never blocks promotion
    return ep_id


def process_walkins() -> int:
    """Manual/backstop sweep — promotes anything the webhook missed."""
    pending = (supabase.table("checkins").select("*")
        .eq("event_id", EVENT_ID).eq("kind", "walkin")
        .is_("event_participant_id", "null").execute().data)
    for w in pending:
        promote_walkin(w)
    return len(pending)


@app.post("/api/admin/process-walkins")
def process_walkins_now(_=Depends(require_admin)):
    return {"processed": process_walkins()}


WEBHOOK_SECRET = env_str("WEBHOOK_SECRET")

@app.post("/api/webhooks/checkin-walkin")
async def checkin_walkin_webhook(request: Request):
    """Supabase Database Webhook target: fires on every INSERT into
    checkins. Promotes walk-in rows the instant they land — this replaces
    the old in-process polling loop, which never actually ran on Vercel's
    serverless (no persistent process to hold the timer)."""
    if not WEBHOOK_SECRET or request.headers.get("x-webhook-secret") != WEBHOOK_SECRET:
        raise HTTPException(401, "bad secret")
    body = await request.json()
    row = body.get("record") or {}
    if body.get("type") != "INSERT" or row.get("kind") != "walkin":
        return {"status": "skipped"}
    ep_id = promote_walkin(row)
    return {"status": "promoted" if ep_id else "already_promoted"}
