"""Background email delivery.

Registrations are written to the database with email_status='queued'. A worker
thread claims queued rows one at a time (safe with several app processes),
sends the certificate, and retries failures with back-off.
"""
import html
import logging
import smtplib
import ssl
import threading
import time
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path

from db import connect, now_iso

log = logging.getLogger("certqr.mail")

DEFAULT_SUBJECT = "Your certificate — {session}"
DEFAULT_BODY = (
    "Hi {name},\n\n"
    "Thank you for joining {session}. Your certificate of participation is attached to this email.\n\n"
    "You can also download it any time here:\n{download_link}\n\n"
    "Certificate ID: {cert_id}\n"
)
PLACEHOLDERS = ("{name}", "{session}", "{date}", "{download_link}", "{cert_id}")


def fill(template, values):
    out = template or ""
    for key, val in values.items():
        out = out.replace("{" + key + "}", val or "")
    return out


def build_message(cfg, ev, p, pdf_bytes):
    values = {
        "name": p["full_name"],
        "session": ev["name"],
        "date": ev["event_date"] or "",
        "download_link": f"{cfg['BASE_URL']}/c/{p['cert_code']}/{p['token']}",
        "cert_id": p["cert_code"],
    }
    subject = fill(ev["email_subject"] or DEFAULT_SUBJECT, values).replace("\n", " ").strip()
    body = fill(ev["email_body"] or DEFAULT_BODY, values)

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg["MAIL_FROM"]
    msg["To"] = p["email"]
    if cfg.get("MAIL_REPLY_TO"):
        msg["Reply-To"] = cfg["MAIL_REPLY_TO"]
    msg["Message-ID"] = make_msgid(domain=cfg["MAIL_FROM"].split("@")[-1].strip("> ") or None)
    msg.set_content(body)

    link = values["download_link"]
    body_html = html.escape(body).replace(html.escape(link), f'<a href="{html.escape(link)}">{html.escape(link)}</a>')
    msg.add_alternative(
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;line-height:1.6;color:#1B2140">'
        + body_html.replace("\n", "<br>") + "</div>",
        subtype="html",
    )
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
            "BASE_URL", "MAIL_FROM", "MAIL_REPLY_TO", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
            "SMTP_PASSWORD", "SMTP_SSL", "SMTP_STARTTLS", "MAIL_MAX_ATTEMPTS", "MAIL_SEND_INTERVAL")}
        self.data_dir = Path(app.config["DATA_DIR"])
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
            try:
                did_work = self.process_one(conn)
            except Exception:  # noqa: BLE001
                log.exception("mail worker error")
                did_work = False
            if did_work:
                time.sleep(self.cfg["MAIL_SEND_INTERVAL"])
            else:
                self._wake.wait(timeout=3)
                self._wake.clear()
        conn.close()

    def process_one(self, conn):
        now = now_iso()
        # Claim the next due email. The status check in the UPDATE makes the claim atomic,
        # so two app processes never send the same email.
        row = None
        for cand in conn.execute("SELECT id FROM participants WHERE email_status='queued' "
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
        try:
            pdf = (self.data_dir / "certs" / row["pdf_file"]).read_bytes()
            msg = build_message(self.cfg, ev, row, pdf)
            deliver(self.cfg, msg, self.data_dir / "outbox")
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
