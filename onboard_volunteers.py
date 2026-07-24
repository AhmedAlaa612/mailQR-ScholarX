"""One-shot volunteer onboarding.

Creates each volunteer through the backend's own /api/admin/volunteers endpoint
(auth user + volunteers row + phone cleaning all reuse the server logic).

Password scheme: lowercase initials of the volunteer's names + random digits,
padded to Supabase's 6-char minimum (3 names -> 3 letters + 3 digits,
2 names -> 2 letters + 4 digits).

Usage:
    python onboard_volunteers.py --dry-run   # show what would be created
    python onboard_volunteers.py             # actually create accounts

Reads ADMIN_KEY from the .env in this folder (same one the backend uses).
Writes volunteers-credentials.csv — hand each row to its volunteer, then
delete the file.
"""
import argparse
import csv
import json
import os
import random
import sys
import urllib.error
import urllib.request


def load_env(path: str = ".env") -> None:
    """Tiny stdlib .env loader (KEY=VALUE lines; quotes stripped) so this
    script has zero dependencies — the machine's python may have no pip."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            os.environ.setdefault(key, value)


load_env()

API_BASE = os.getenv("ONBOARD_API_BASE", "https://mail-qr-scholar-x.vercel.app")
ADMIN_KEY = os.getenv("ADMIN_KEY")

# (display_name, phone, email) — checkpoints get assigned later in the dashboard.
VOLUNTEERS = [
    ("Ahmed Rady Mohamed",    "01146899407", "ahmdshy08@gmail.com"),        # team leader
    ("Mariam Sayed Abdallah", "01552376973", "mariamsayed2259@gmail.com"),
    ("Reem Diab",             "01024750972", "reemdiab211411@gmail.com"),
    ("Farida Mohamed Hassan", "01141555799", "476900860@cairo7.moe.edu.eg"),
    ("Mahmoud Ahmed Salem",   "01068547177", "mahmoudahmedsalem2@gmail.com"),
    ("Fayrouz Amr Fouad",     "01207584505", "fayrozamr94@gmail.com"),
    ("Fatma Ashraf Hassan",   "01023367297", "fatmaaashraf32@gmail.com"),
    ("Sama Alaa Mohamed",     "01024837539", "samaalaamz@gmail.com"),
]


def make_password(display_name: str) -> str:
    initials = "".join(w[0] for w in display_name.split()[:3]).lower()
    n_digits = max(3, 6 - len(initials))  # Supabase minimum password length is 6
    digits = "".join(str(random.randint(0, 9)) for _ in range(n_digits))
    return initials + digits


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="print what would be created without calling the API")
    args = parser.parse_args()

    if not ADMIN_KEY and not args.dry_run:
        print("ADMIN_KEY not set — put it in .env next to this script.")
        return 1

    rows = []
    failures = 0
    for display_name, phone, email in VOLUNTEERS:
        password = make_password(display_name)
        if args.dry_run:
            print(f"would create  {display_name:<24} {email:<32} pw={password}")
            rows.append((display_name, phone, email, password))
            continue

        req = urllib.request.Request(
            f"{API_BASE}/api/admin/volunteers",
            method="POST",
            headers={"X-Admin-Key": ADMIN_KEY, "Content-Type": "application/json"},
            data=json.dumps({
                "email": email,
                "password": password,
                "display_name": display_name,
                "phone": phone,
                "checkpoint_id": None,
                "role": "scanner",
            }).encode(),
        )
        try:
            with urllib.request.urlopen(req, timeout=30):
                pass
            print(f"created  {display_name:<24} {email:<32} pw={password}")
            rows.append((display_name, phone, email, password))
        except urllib.error.HTTPError as e:
            # 422 with an "already registered" message just means a re-run;
            # the existing account keeps its original password.
            failures += 1
            body = e.read().decode(errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except ValueError:
                detail = body
            print(f"FAILED   {display_name:<24} {email:<32} -> {e.code}: {detail}")
        except urllib.error.URLError as e:
            failures += 1
            print(f"FAILED   {display_name:<24} {email:<32} -> network error: {e.reason}")

    if rows:
        out = "volunteers-credentials.csv"
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["name", "phone", "email", "password"])
            w.writerows(rows)
        print(f"\n{len(rows)} credential(s) written to {out} — distribute, then delete the file.")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
