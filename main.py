from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel
from typing import Optional
import qrcode
from PIL import Image, ImageDraw
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer
import io, os, re, uuid, smtplib, ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from supabase import create_client, Client
from dotenv import load_dotenv

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

def create_event_participant(participant_id: str, event_id: str) -> str:
    """Insert event_participant row, returning existing id if already registered."""
    res = supabase.table("event_participants").upsert({
        "id":             str(uuid.uuid4()),
        "event_id":       event_id,
        "participant_id": participant_id,
        "role":           "attendee",
        "status":         "registered",
        "source":         "google_form",
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

def send_email(to_email: str, first_name: str, qr_bytes: bytes):
    body = (
        f"Hey {first_name}, thanks for registering for the Next Scholar Summit!\n\n"
        f"Your QR code is attached — keep it ready at the event entrance.\n\n"
        f"Don't forget to join the event's WhatsApp group for updates and announcements:\n"
        f"https://chat.whatsapp.com/DZtFtlvxBQxBu0drWdjzws\n\n"
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

    day_counts: dict = {}
    type_counts: dict = {}
    session_actions: dict = {}

    for r in rows:
        day = (r.get("downloaded_at") or "")[:10]
        if day:
            day_counts[day] = day_counts.get(day, 0) + 1

        t = r.get("type") or "download"
        type_counts[t] = type_counts.get(t, 0) + 1

        sid = r.get("session_id")
        if sid:
            if sid not in session_actions:
                session_actions[sid] = []
            session_actions[sid].append(t)

    unique_sessions = len(session_actions)
    avg_actions = round(total / unique_sessions, 1) if unique_sessions > 0 else 0

    by_day  = [{"date": k, "count": v} for k, v in sorted(day_counts.items())]
    by_type = [{"type": k, "count": v} for k, v in sorted(type_counts.items(), key=lambda x: -x[1])]

    return {
        "total": total,
        "unique_sessions": unique_sessions,
        "avg_actions_per_session": avg_actions,
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
