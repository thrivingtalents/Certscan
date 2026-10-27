# CertQR: QR certificate registration

Participants scan a QR code, type their name and email, check a preview, and get their certificate as a PDF by email within seconds. Organisers run as many sessions as they like, each with its own certificate design, QR code and participant list.

## What's in this MVP

**Admin panel** (`/admin`)
- Password login
- Sessions: create, edit, open/close registration, duplicate (reuse a design), delete
- Upload a certificate design as PNG, JPG or a one-page PDF
- Drag-and-drop editor to place the `[FULL NAME]` box, with font, size, colour, alignment and letter case (As typed / Title Case / CAPS). Long names shrink automatically to fit the box.
- Preview a sample PDF before saving
- QR code page: printable poster, PNG and SVG downloads, copy link
- Participants table: search, filter by email status, open PDF, resend, fix a typo (regenerates the PDF and re-sends it), remove
- CSV export (opens correctly in Excel)
- Email setup page with a "send test email" button
- Optional attendance code per session, so a photo of the QR alone isn't enough to register

**Participant side** (mobile-first)
1. Scan QR → `/r/<session-link>`
2. Enter full name and email (plus attendance code if the session has one)
3. See a preview of their actual certificate and confirm the spelling
4. The PDF is generated immediately; the page shows a download button and a live "emailing… / sent" status
5. The email arrives with the PDF attached and a download link

**Built in**
- Each certificate gets an ID (e.g. `8BJDCWXS`) printed in the corner, and anyone can check it at `/v/<ID>`
- One certificate per email per session. If someone registers again, it is re-sent to that inbox and the download link isn't shown on screen, so typing someone else's email doesn't reveal their certificate.
- Emails go through a background queue with automatic retries (4 attempts with back-off). It's safe to run several app processes.
- CSRF protection on every form, rate limits on registration, login and verify
- No external services needed to try it: without a mail server configured, emails are saved as `.eml` files in `data/outbox`

## Run it on your computer (5 minutes)

Needs Python 3.10 or newer.

```bash
cd certqr
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
python app.py
```

Open http://localhost:5000/admin and log in with `change-me`, or with the `ADMIN_PASSWORD` you set in `.env`.

To try the whole flow, create a session using `samples/sample-template.png`, place the name, then open the registration link from the QR page. Emails appear in `data/outbox/` (open the `.eml` files with any mail app).

To test on your phone over the same Wi-Fi, set `BASE_URL=http://<your-computer's-LAN-IP>:5000` in `.env` and restart.

Run the automated tests with `python tests/test_flow.py` (or `pytest -q tests`).

## Configuration (`.env`)

| Setting | What it does |
|---|---|
| `SECRET_KEY` | **Required in production.** Long random string: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_PASSWORD` | Admin login password |
| `BASE_URL` | Public address, e.g. `https://cert.yourdomain.com`. Used inside QR codes and email links, so set it **before printing QR codes**. |
| `APP_NAME` | Name shown on pages ("Powered by …") |
| `DISPLAY_TZ` | Time zone for times in the admin panel and CSV, e.g. `Asia/Kuala_Lumpur` |
| `SMTP_*`, `MAIL_FROM` | Email sending (below). Leave `SMTP_HOST` empty for outbox mode. |
| `MAIL_SEND_INTERVAL` | Seconds between emails (default 0.5). Raise it if your provider rate-limits you. |
| `REGISTER_LIMIT_PER_MIN` | Registrations allowed per IP per minute (default 10). Raise it if a whole hall shares one Wi-Fi. |
| `MAX_UPLOAD_MB` | Largest template upload (default 15) |
| `DATA_DIR` | Where the database, templates and PDFs live (default `./data`) |

## Email

Gmail works for small tests only: it needs an App Password, it caps you at roughly 500 emails a day, and it often lands in spam. For events, use a transactional email service with your own domain and set up its SPF/DKIM records so certificates don't land in spam.

| Provider | Settings |
|---|---|
| Brevo (free tier ~300/day) | `SMTP_HOST=smtp-relay.brevo.com` `SMTP_PORT=587` `SMTP_USER=<your Brevo login>` `SMTP_PASSWORD=<SMTP key>` |
| Resend | `SMTP_HOST=smtp.resend.com` `SMTP_PORT=465` `SMTP_SSL=true` `SMTP_STARTTLS=false` `SMTP_USER=resend` `SMTP_PASSWORD=<API key>` |
| Gmail (testing) | `SMTP_HOST=smtp.gmail.com` `SMTP_PORT=587` `SMTP_USER=you@gmail.com` `SMTP_PASSWORD=<16-char app password>` |

`MAIL_FROM` must be an address your provider has verified, e.g. `MAIL_FROM=Digital Skills Workshop <certificates@yourdomain.com>`.

The sender name, sender email, reply-to address and mail server can also be changed at any time in the admin panel under **Email setup**, with no restart. Values saved there override `.env`, and the mail password is stored encrypted with `SECRET_KEY` (if `SECRET_KEY` changes, enter the password again). After changing anything, use **Email setup → Send test**. Each session's email subject and message can be edited under **Settings & email**, using `{name}`, `{session}`, `{date}`, `{download_link}` and `{cert_id}`.

## Deploying

The app is a normal Flask app with a SQLite database, so any small server works. The one rule is that **the `data/` folder must be on persistent storage**: it holds the database, templates and every issued PDF.

**A small VPS (e.g. DigitalOcean, Lightsail, Hetzner; about USD 5/month):**

```bash
pip install -r requirements.txt
gunicorn -w 2 --threads 4 -b 127.0.0.1:8000 "app:create_app()"
```

Put Nginx or Caddy in front for HTTPS. Caddy is the simplest: `cert.yourdomain.com { reverse_proxy 127.0.0.1:8000 }`. Run gunicorn under systemd so it restarts on reboot. Two workers comfortably handle a hall of several hundred people scanning at once; in testing, 40 simultaneous registrations completed in under 7 seconds on a single development-server process.

**Render:** the repository includes `render.yaml`. In Render choose *New → Blueprint*, pick the repository, and enter an `ADMIN_PASSWORD` when asked. It creates a Starter web service with a 1 GB persistent disk at `/var/data`, a generated `SECRET_KEY`, and a health check at `/healthz`. Without `BASE_URL` the app uses its `https://<name>.onrender.com` address; after adding a custom domain, set `BASE_URL` to it before printing QR codes. Then set up email in the admin panel.

**Railway or similar:** start command `gunicorn -w 2 --threads 4 -b 0.0.0.0:$PORT "app:create_app()"`. Attach a persistent disk and set `DATA_DIR` to its mount path, or every certificate is lost on redeploy.

**Backups:** copy the `data/` folder. For a live copy of the database, use `sqlite3 data/certqr.db ".backup backup.db"`.

## Designing templates

- Export the certificate **without** the name. Leave the name area empty (a line under it is fine).
- PNG/JPG: at least 2000 px wide; 3508 × 2480 px is A4 at 300 dpi. PDFs are used as-is (first page only), which keeps vector artwork sharp.
- Canva: *Share → Download → PDF Standard*, or PNG at the largest size.
- The bundled fonts are Poppins and DejaVu Serif, which cover Latin, accented and Malay names. For Chinese, Tamil or Arabic names, add a suitable `.ttf` file to `static/fonts/` and an entry to `FONTS` in `render.py`.

## Project layout

```
app.py            routes: public registration, downloads, verify, admin panel
render.py         template processing, name stamping, PDF → PNG previews, QR codes
mailer.py         email building + background queue worker with retries
db.py             SQLite schema and connection helpers
config.py         settings from .env
templates/        Jinja pages (admin/ and public/)
static/           CSS, the drag-and-drop editor script, fonts
samples/          sample certificate template for testing
tests/            end-to-end test of the full flow
data/             created on first run: database, templates, PDFs, outbox
```

## Known limits and next steps

- A single admin password. Next step: named admin accounts with roles.
- Rate limits are counted per app process and trust the `X-Forwarded-For` header, so run the app behind your own reverse proxy.
- Only the name is placed on the certificate. Next step: extra fields such as IC number, organisation or custom text, each as its own draggable box.
- Possible later additions: bulk import attendees from CSV and send certificates without scanning, a check-in list so only registered attendees can claim, and delivery through WhatsApp.
