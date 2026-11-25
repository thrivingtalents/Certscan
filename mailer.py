"""Background work: making certificates and emailing them.

Bulk-imported names are saved without a PDF; the worker thread makes those one at a
time. Registrations are written with email_status='queued'; the worker claims queued
rows one at a time (safe with several app processes), sends the certificate, and
retries failures with back-off.
"""
import base64
import hashlib
import html
import logging
import re
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import render
from db import connect, now_iso

log = logging.getLogger("certqr.mail")

DEFAULT_SUBJECT = "Your certificate — {session}"
DEFAULT_BODY = (
    "Hi {name},\n\n"
    "Thank you for joining {session}. Your certificate of participation is attached to this email.\n\n"
    "You can also download it any time here:\n{download_link}\n"
)
PLACEHOLDERS = ("{name}", "{session}", "{client}", "{date}", "{download_link}")
NO_REPLY_NOTE = "This is an automated email. Please don't reply to it."

# in the message an admin writes: [text](https://...) is a link, {picture:<key>} is a picture they uploaded,
# and any other web address becomes clickable
LINK_RE = re.compile(r"\[([^\[\]\n]+)\]\((https?://[^\s()<>]+)\)|(https?://[^\s<>\"'\[\]]+)|\{picture:([0-9a-f]{12})\}")
PICTURE_DIR = "email-pictures"
PICTURE_TYPES = {"jpg": ("image", "jpeg"), "png": ("image", "png"), "gif": ("image", "gif")}


def picture_file(pictures_dir, key):
    """The stored file for a picture key, or None if it doesn't exist."""
    if not pictures_dir or not re.fullmatch(r"[0-9a-f]{12}", key or ""):
        return None
    for ext in PICTURE_TYPES:
        path = Path(pictures_dir) / f"{key}.{ext}"
        if path.exists():
            return path
    return None


def body_text(body):
    """The plain-text version of a message: links as "text (address)", pictures left out."""
    def sub(m):
        if m.group(1):
            return f"{m.group(1)} ({m.group(2)})"
        return m.group(3) or ""
    text = LINK_RE.sub(sub, body)
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"


def body_html(body, picture_src):
    """HTML for a message. picture_src(key) returns the image address, or None to leave it out."""
    out, pos = [], 0
    for m in LINK_RE.finditer(body):
        out.append(html.escape(body[pos:m.start()]))
        pos = m.end()
        if m.group(1):
            out.append(f'<a href="{html.escape(m.group(2))}" style="color:#4B3BE0">{html.escape(m.group(1))}</a>')
        elif m.group(3):
            # "see https://x.com." keeps the full stop out of the link
            url, tail = re.match(r"(.*?)([.,;:!?)]*)$", m.group(3)).groups()
            out.append(f'<a href="{html.escape(url)}" style="color:#4B3BE0">{html.escape(url)}</a>{html.escape(tail)}')
        else:
            src = picture_src(m.group(4))
            if src:
                out.append(f'<img src="{html.escape(src)}" alt="" style="display:block;max-width:100%;height:auto;'
                           f'border:0;margin:6px 0">')
    out.append(html.escape(body[pos:]))
    # a picture is a block of its own, so the line break after it isn't needed as well
    joined = re.sub(r"(<img [^>]+>)\n", r"\1", "".join(out))
    return joined.replace("\n", "<br>")


def wrap_html(inner):
    return ('<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:#1B2140;'
            'max-width:640px">' + inner +
            '<p style="margin:28px 0 0;padding-top:12px;border-top:1px solid #E3E6F0;font-size:12px;color:#8A90AE">'
            + html.escape(NO_REPLY_NOTE) + '</p></div>')


def message_values(base_url, ev, p):
    return {
        "name": p["full_name"],
        "session": ev["name"],
        "client": ev["client_name"] or "",
        "date": ev["event_date"] or "",
        "download_link": f"{base_url}/c/{p['cert_code']}/{p['token']}",
        "cert_id": "",  # certificate IDs were removed; blank it in messages written before that
    }


# Mail settings an admin can change on the Email setup page. Saved values override .env.
SETTING_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_SSL", "SMTP_STARTTLS",
                "MAIL_FROM")


def _fernet(secret_key):
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret_key.encode()).digest()))


def mail_settings(conn, cfg):
    """The mail settings in cfg with any saved in the admin panel on top.

    The saved SMTP password is stored encrypted with SECRET_KEY. If SECRET_KEY has changed
    it can't be read: the password is then empty and PASSWORD_UNREADABLE is set.
    """
    out = {k: cfg[k] for k in SETTING_KEYS}
    out["PASSWORD_UNREADABLE"] = False
    for row in conn.execute("SELECT key, value FROM settings"):
        key, val = row["key"], row["value"]
        if key not in SETTING_KEYS:
            continue
        if key == "SMTP_PASSWORD":
            try:
                val = _fernet(cfg["SECRET_KEY"]).decrypt(val.encode()).decode()
            except InvalidToken:
                val, out["PASSWORD_UNREADABLE"] = "", True
        elif key == "SMTP_PORT":
            val = int(val)
        elif key in ("SMTP_SSL", "SMTP_STARTTLS"):
            val = val == "1"
        out[key] = val
    return out


def save_mail_settings(conn, secret_key, values):
    for key, val in values.items():
        if key == "SMTP_PASSWORD":
            val = _fernet(secret_key).encrypt(val.encode()).decode()
        elif isinstance(val, bool):
            val = "1" if val else "0"
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(val)))
    conn.commit()


def format_sender(name, email):
    name = name.replace('"', "").replace("\\", "").strip()
    return f'"{name}" <{email}>' if name else email


def fill(template, values):
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", val or "")
    return out


def build_message(cfg, ev, p, pdf_bytes, pictures_dir=None):
    values = message_values(cfg["BASE_URL"], ev, p)
    subject = fill(ev["email_subject"] or DEFAULT_SUBJECT, values).replace("\n", " ").strip()
    body = fill(ev["email_body"] or DEFAULT_BODY, values)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = p["email"]
    # sent from a no-reply address, so there is no Reply-To; this also stops out-of-office auto-replies
    msg["Auto-Submitted"] = "auto-generated"
    domain = parseaddr(cfg["MAIL_FROM"])[1].rpartition("@")[2] or None
    msg["Message-ID"] = make_msgid(domain=domain)
    msg.set_content(body_text(body) + "\n--\n" + NO_REPLY_NOTE + "\n")

    pictures = {}  # key -> (content id, file)

    def picture_src(key):
        path = picture_file(pictures_dir, key)
        if path is None:
            return None
        if key not in pictures:
            pictures[key] = (make_msgid(idstring="pic", domain=domain), path)
        return "cid:" + pictures[key][0][1:-1]

    msg.add_alternative(wrap_html(body_html(body, picture_src)), subtype="html")
    if pictures:
        # pictures travel inside the email rather than being linked from the web, so mail apps show them straight away
        html_part = msg.get_payload()[1]
        for cid, path in pictures.values():
            maintype, subtype = PICTURE_TYPES[path.suffix[1:]]
            html_part.add_related(path.read_bytes(), maintype=maintype, subtype=subtype, cid=cid,
                                  disposition="inline", filename=path.name)
    safe = "".join(ch if ch.isalnum() else "_" for ch in p["full_name"]).strip("_")[:60] or "Certificate"
    msg.add_attachment(pdf_bytes, maintype="application", subtype="pdf", filename=f"Certificate_{safe}.pdf")
    return msg


def deliver(cfg, msg, outbox_dir):
    if not cfg["SMTP_HOST"]:
        outbox = Path(outbox_dir)
        outbox.mkdir(parents=True, exist_ok=True)
        name = f"{datetime.now():%Y%m%d-%H%M%S}-{msg['To'].replace('@', '_at_')}.eml"
        (outbox / name).write_bytes(bytes(msg))
        return
    ctx = ssl.create_default_context()
    if cfg["SMTP_SSL"]:
        server = smtplib.SMTP_SSL(cfg["SMTP_HOST"], cfg["SMTP_PORT"], context=ctx, timeout=30)
    else:
        server = smtplib.SMTP(cfg["SMTP_HOST"], cfg["SMTP_PORT"], timeout=30)
    try:
        server.ehlo()
        if not cfg["SMTP_SSL"] and cfg["SMTP_STARTTLS"]:
            server.starttls(context=ctx)
            server.ehlo()
        if cfg["SMTP_USER"]:
            server.login(cfg["SMTP_USER"], cfg["SMTP_PASSWORD"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001
            pass


class MailWorker(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True, name="certqr-mail")
        self.cfg = {k: app.config[k] for k in (
            "BASE_URL", "SECRET_KEY", "MAIL_MAX_ATTEMPTS", "MAIL_SEND_INTERVAL") + SETTING_KEYS}
        self.data_dir = Path(app.config["DATA_DIR"])
        self.tpl_dir, self.cert_dir = self.data_dir / "templates", self.data_dir / "certs"
        self._wake = threading.Event()
        self._stop = threading.Event()

    def wake(self):
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def run(self):
        conn = connect(self.data_dir / "certqr.db")
        # rows left in 'sending' by a crash go back to the queue
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(microsecond=0).isoformat()
        conn.execute("UPDATE participants SET email_status='queued' WHERE email_status='sending' "
                     "AND (next_attempt_at IS NULL OR next_attempt_at < ?)", (stale,))
        conn.commit()
        while not self._stop.is_set():
            made = sent = False
            try:
                made = self.make_one_pdf(conn)
                sent = not made and self.process_one(conn)
            except Exception:  # noqa: BLE001
                log.exception("mail worker error")
            if sent:
                time.sleep(self.cfg["MAIL_SEND_INTERVAL"])  # only emails need pacing for the provider
            elif not made:
                self._wake.wait(timeout=3)
                self._wake.clear()
        conn.close()

    def make_one_pdf(self, conn):
        """Make the next certificate that hasn't been made yet. Returns True if there was one."""
        now = now_iso()
        # a worker that died mid-way leaves pdf_claimed_at behind; after 5 minutes another takes over
        stale = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(microsecond=0).isoformat()
        for cand in conn.execute("SELECT id FROM participants WHERE pdf_file IS NULL "
                                 "AND (pdf_claimed_at IS NULL OR pdf_claimed_at < ?) ORDER BY id LIMIT 5",
                                 (stale,)).fetchall():
            cur = conn.execute("UPDATE participants SET pdf_claimed_at=? WHERE id=? AND pdf_file IS NULL "
                               "AND (pdf_claimed_at IS NULL OR pdf_claimed_at < ?)", (now, cand["id"], stale))
            conn.commit()
            if cur.rowcount == 1:
                make_pdf(conn, cand["id"], self.tpl_dir, self.cert_dir)
                return True
        return False

    def process_one(self, conn):
        now = now_iso()
        # Claim the next due email. The status check in the UPDATE makes the claim atomic,
        # so two app processes never send the same email.
        row = None
        for cand in conn.execute("SELECT id FROM participants WHERE email_status='queued' AND pdf_file IS NOT NULL "
                                 "AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT 5",
                                 (now,)).fetchall():
            cur = conn.execute("UPDATE participants SET email_status='sending', email_attempts=email_attempts+1, "
                               "next_attempt_at=? WHERE id=? AND email_status='queued'", (now, cand["id"]))
            conn.commit()
            if cur.rowcount == 1:
                row = conn.execute("SELECT * FROM participants WHERE id=?", (cand["id"],)).fetchone()
                break
        if row is None:
            return False
        ev = conn.execute("SELECT * FROM events WHERE id=?", (row["event_id"],)).fetchone()
        # read settings per email so changes in the admin panel apply without a restart
        cfg = {**self.cfg, **mail_settings(conn, self.cfg)}
        try:
            pdf_name = row["pdf_file"]
            if is_outdated(self.cert_dir / pdf_name, ev):
                # the design (font, position, template...) changed since this one was made, e.g. before a resend
                pdf_name = make_pdf(conn, row["id"], self.tpl_dir, self.cert_dir)
            pdf = (self.cert_dir / pdf_name).read_bytes()
            msg = build_message(cfg, ev, row, pdf, self.data_dir / PICTURE_DIR)
            deliver(cfg, msg, self.data_dir / "outbox")
        except Exception as exc:  # noqa: BLE001
            attempts = row["email_attempts"]
            err = f"{exc.__class__.__name__}: {exc}"[:500]
            if attempts >= self.cfg["MAIL_MAX_ATTEMPTS"]:
                conn.execute("UPDATE participants SET email_status='failed', email_error=? WHERE id=?", (err, row["id"]))
            else:
                retry = datetime.now(timezone.utc) + timedelta(seconds=30 * 2 ** (attempts - 1))
                conn.execute("UPDATE participants SET email_status='queued', email_error=?, next_attempt_at=? WHERE id=?",
                             (err, retry.replace(microsecond=0).isoformat(), row["id"]))
            conn.commit()
            log.warning("email to %s failed (attempt %s): %s", row["email"], attempts, err)
            return True
        conn.execute("UPDATE participants SET email_status='sent', email_error=NULL, email_sent_at=? WHERE id=?",
                     (now_iso(), row["id"]))
        conn.commit()
        log.info("certificate emailed to %s", row["email"])
        return True


def is_outdated(path, ev):
    """True if a certificate file is missing or older than the last change to its session's design."""
    if not path.exists():
        return True
    if not ev["design_updated_at"]:
        return False
    made = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    return made < datetime.fromisoformat(ev["design_updated_at"])


def make_pdf(conn, pid, tpl_dir, cert_dir):
    """Render one participant's certificate, save it and record the file name. Returns the file name."""
    p = conn.execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()
    ev = conn.execute("SELECT * FROM events WHERE id=?", (p["event_id"],)).fetchone()
    name = f"{ev['id']}-{p['cert_code']}.pdf"
    (Path(cert_dir) / name).write_bytes(render.render_certificate(ev, p["full_name"], tpl_dir))
    conn.execute("UPDATE participants SET pdf_file=?, pdf_claimed_at=NULL WHERE id=?", (name, pid))
    conn.commit()
    return name
