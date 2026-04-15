from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import qrcode
from PIL import Image, ImageDraw
from qrcode.image.styledpil import StyledPilImage
from qrcode.image.styles.moduledrawers.pil import RoundedModuleDrawer
import io, os, re, uuid, smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()

# ── Supabase ──────────────────────────────────────────────────────────────────
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ── Config ────────────────────────────────────────────────────────────────────
LOGO_PATH   = os.getenv("LOGO_PATH", "logo.jpg")
SMTP_HOST   = os.getenv("SMTP_HOST")
SMTP_PORT   = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER   = os.getenv("SMTP_USER")
SMTP_PASS   = os.getenv("SMTP_PASS")
FROM_NAME   = os.getenv("FROM_NAME", "ScholarX")
SMTP_TIMEOUT = float(os.getenv("SMTP_TIMEOUT", "20"))

app = FastAPI(title="ScholarX Registration API")

# ── Input schema ──────────────────────────────────────────────────────────────
class RegistrationPayload(BaseModel):
    full_name:   str
    email:       str
    phone:       str
    national_id: Optional[str] = None

# ── Cleaners ──────────────────────────────────────────────────────────────────

EMAIL_DOMAIN_FIXES = {
    # Gmail variants
    "gamil.com": "gmail.com", "gimail.com": "gmail.com", "gmai.com": "gmail.com",
    "gmial.com": "gmail.com", "gmil.com": "gmail.com", "gmal.com": "gmail.com",
    "gemail.com": "gmail.com", "gmaill.com": "gmail.com", "gmail.co": "gmail.com",
    "gmail.cm": "gmail.com",
    # Yahoo variants
    "yhoo.com": "yahoo.com", "yaho.com": "yahoo.com", "yahooo.com": "yahoo.com",
    "yahoo.co": "yahoo.com",
    # Hotmail / Outlook variants
    "hotmial.com": "hotmail.com", "hotmal.com": "hotmail.com",
    "outloook.com": "outlook.com", "outlok.com": "outlook.com",
    # TLD typos (apply after domain fix)
    ".con": ".com", ".cpm": ".com", ".ocm": ".com",
}

def clean_email(raw: str) -> str:
    email = raw.strip().lower()
    # Fix common TLD typos first
    for bad, good in {".con": ".com", ".cpm": ".com", ".ocm": ".com"}.items():
        if email.endswith(bad):
            email = email[:-len(bad)] + good
    # Fix domain
    if "@" in email:
        local, domain = email.rsplit("@", 1)
        domain = EMAIL_DOMAIN_FIXES.get(domain, domain)
        email = f"{local}@{domain}"
    return email

def clean_phone(raw: str) -> Optional[str]:
    digits = re.sub(r"\D", "", raw.strip())
    # 2001xxxxxxxxx → 201xxxxxxxxx (13 digits, extra leading 0)
    if len(digits) == 13 and digits.startswith("2001"):
        digits = "20" + digits[3:]
    # 01xxxxxxxxx → 201xxxxxxxxx
    if len(digits) == 11 and digits.startswith("01"):
        digits = "20" + digits[1:]
    # 1xxxxxxxxx (10 digits, missing country code)
    if len(digits) == 10 and digits.startswith("1"):
        digits = "20" + digits
    return digits if len(digits) == 12 and digits.startswith("20") else None

def clean_name(raw: str) -> str:
    return " ".join(raw.strip().split()).title()

def clean_national_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw.strip())
    return digits if len(digits) == 14 else None

def find_existing_registration(email: str, national_id: Optional[str], phone: Optional[str]):
    res = supabase.table("registrations").select("id").eq("email", email).execute()
    if res.data:
        return res.data[0]

    if national_id:
        res = supabase.table("registrations").select("id").eq("national_id", national_id).execute()
        if res.data:
            return res.data[0]

    if phone:
        res = supabase.table("registrations").select("id").eq("phone", phone).execute()
        if res.data:
            return res.data[0]

    return None

# ── QR Generator ─────────────────────────────────────────────────────────────

def make_rounded_logo(path: str, corner_radius_ratio: float = 0.2) -> Image.Image:
    logo = Image.open(path).convert("RGBA")
    mask = Image.new("L", logo.size, 0)
    draw = ImageDraw.Draw(mask)
    radius = max(1, int(min(logo.size) * corner_radius_ratio))
    draw.rounded_rectangle((0, 0, logo.size[0], logo.size[1]), radius=radius, fill=255)
    logo.putalpha(mask)
    return logo

def generate_qr_bytes(row_id: str, name: str, email: str, phone: Optional[str]) -> bytes:
    data = f"{row_id},{name},{email},{phone or ''}"

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=5,
    )
    qr.add_data(data)
    qr.make(fit=True)

    # Prefer styled QR, but gracefully fall back to plain QR if rendering fails.
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

# ── Email Sender ──────────────────────────────────────────────────────────────

def send_email(to_email: str, first_name: str, qr_bytes: bytes):
    body = (
        f"Hey {first_name}, thanks for registering for the Next Scholar Summit!\n\n"
        f"We'll see you on May 1st at Nile University. Your QR code is attached — "
        f"have it ready at the event.\n\n"
        f"Join the summit's WhatsApp group:\n"
        f"https://chat.whatsapp.com/FCbd5QvoiCu6UDv6u6rJ3W?mode=gi_t"
    )

    msg = MIMEMultipart()
    msg["From"]    = f"{FROM_NAME} <{SMTP_USER}>"
    msg["To"]      = to_email
    msg["Subject"] = "Your QR Code Ticket for Next Scholar Summit"
    msg.attach(MIMEText(body, "plain"))

    img_part = MIMEImage(qr_bytes, name="qrcode.png")
    img_part.add_header("Content-Disposition", "attachment", filename="qrcode.png")
    msg.attach(img_part)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, to_email, msg.as_string())

# ── Main endpoint ─────────────────────────────────────────────────────────────

@app.post("/api/register")
async def register(payload: RegistrationPayload):
    # 1. Clean inputs
    name        = clean_name(payload.full_name)
    email       = clean_email(payload.email)
    phone       = clean_phone(payload.phone)
    national_id = clean_national_id(payload.national_id)
    first_name  = name.split()[0]

    # 2. Insert or update Supabase
    existing = find_existing_registration(email, national_id, phone)
    is_update = existing is not None
    row_id = existing["id"] if is_update else str(uuid.uuid4())

    payload_data = {
        "id":          row_id,
        "name":        name,
        "email":       email,
        "phone":       phone,
        "national_id": national_id,
        "qr_sent":     False,
    }

    if is_update:
        supabase.table("registrations").update(payload_data).eq("id", row_id).execute()
    else:
        supabase.table("registrations").insert(payload_data).execute()

    # 4. Generate QR
    qr_bytes = generate_qr_bytes(row_id, name, email, phone)

    # 5. Send email
    try:
        send_email(email, first_name, qr_bytes)
        supabase.table("registrations").update({"qr_sent": True}).eq("id", row_id).execute()
        email_status = "sent"
    except Exception as e:
        email_status = f"failed: {str(e)}"

    return {
        "status":       "updated" if is_update else "registered",
        "message":      f"Registered successfully. Email: {email_status}",
        "id":           row_id,
        "cleaned": {
            "name":        name,
            "email":       email,
            "phone":       phone,
            "national_id": national_id,
        }
    }

# ── Health check ──────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok"}
