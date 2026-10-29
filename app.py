"""CertQR - QR-based certificate registration.

Run locally:   python app.py        (then open http://localhost:5000/admin)
Production:    gunicorn -w 2 -b 0.0.0.0:8000 "app:create_app()"
"""
import csv
import hmac
import io
import logging
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import time
import zipfile
from collections import defaultdict, deque
from datetime import datetime
from email.utils import parseaddr
from functools import wraps
from zoneinfo import ZoneInfo
from pathlib import Path

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

import bulk
import mailer
import render
from config import Config
from db import get_db, init_db, now_iso

# Windows can map .js/.css to text/plain in the registry; with nosniff the browser then refuses them
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")

CODE_ALPHABET ="ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
EMAIL_RE = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[A-Za-z]{2,}$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{1,58})[a-z0-9]$")
HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


# ======================================================================== helpers

class RateLimiter:
    """Small in-memory sliding-window limiter (per process)."""

    def __init__(self):
        self.hits = defaultdict(deque)

    def allow(self, key, limit, window=60):
        now = time.monotonic()
        q = self.hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        return True


limiter = RateLimiter()


def client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    return (fwd.split(",")[0].strip() if fwd else request.remote_addr) or "?"


def slugify(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:50].strip("-") or "session")


def unique_slug(db, base, exclude_id=None):
    slug, n = base, 2
    while True:
        row = db.execute("SELECT id FROM events WHERE slug=?", (slug,)).fetchone()
        if row is None or row["id"] == exclude_id:
            return slug
        slug = f"{base}-{n}"
        n += 1


def new_code(db):
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        if not db.execute("SELECT 1 FROM participants WHERE cert_code=?", (code,)).fetchone():
            return code


def clean_name(raw):
    name = re.sub(r"\s+", " ", (raw or "")).strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "<>{}[]\\|^~`")
    return name


def fnum(value, default, lo, hi):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def event_by_id(event_id):
    ev = get_db().execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    if ev is None:
        abort(404)
    return ev


def event_by_slug(slug):
    ev = get_db().execute("SELECT * FROM events WHERE slug=?", (slug,)).fetchone()
    if ev is None:
        abort(404)
    return ev


DONE_DEFAULTS = {"done_heading": "You're all set", "done_title": "Certificate ready, {first_name}!",
                 "done_message": ""}
DONE_PLACEHOLDERS = ("{name}", "{first_name}", "{session}", "{date}")
# email_status values, with the label the admin panel shows
EMAIL_STATUSES = {"sent": "Sent", "queued": "Queued", "sending": "Sending", "failed": "Failed", "none": "Not emailed"}
DONE_FIELDS = ("done_heading", "done_title", "done_message", "done_button_label", "done_button_url",
               "done_show_download", "done_show_view")


def done_texts(ev, full_name):
    """The thank-you page's heading, title and message for one participant, placeholders filled in."""
    values = {"name": full_name, "first_name": full_name.split(" ")[0], "session": ev["name"],
              "date": ev["event_date"] or ""}
    return {key[5:]: mailer.fill(ev[key] or default, values) for key, default in DONE_DEFAULTS.items()}


def safe_filename(full_name):
    return "".join(ch if ch.isalnum() else "_" for ch in full_name).strip("_")[:60] or "Certificate"


def paths(app):
    d = Path(app.config["DATA_DIR"])
    return d / "templates", d / "certs"


# ======================================================================== app factory

def check_production_settings():
    """On Render, refuse to start with settings that are unsafe or break logins."""
    if not os.environ.get("RENDER"):
        return
    missing = []
    if not os.environ.get("SECRET_KEY"):
        # each gunicorn worker would invent its own key, so logins and forms fail at random
        missing.append("SECRET_KEY (a long random string)")
    if os.environ.get("ADMIN_PASSWORD", "change-me") == "change-me":
        missing.append("ADMIN_PASSWORD (your admin login password)")
    if missing:
        raise RuntimeError("Set these in Render (Environment tab), then redeploy: " + "; ".join(missing))


def create_app(overrides=None):
    check_production_settings()
    app = Flask(__name__)
    app.config.from_object(Config)
    if overrides:
        app.config.update(overrides)
    data = Path(app.config["DATA_DIR"])
    for sub in ("templates", "certs", "outbox"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    init_db(app)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    tpl_dir, cert_dir = paths(app)

    worker = None
    if app.config["RUN_MAIL_WORKER"]:
        worker = mailer.MailWorker(app)
        worker.start()
    app.extensions["mail_worker"] = worker

    def wake_mailer():
        if app.extensions.get("mail_worker"):
            app.extensions["mail_worker"].wake()

    # ---------------------------------------------------------------- security

    def csrf_token():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_urlsafe(24)
        return session["_csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token
    app.jinja_env.globals["app_name"] = app.config["APP_NAME"]
    app.jinja_env.globals["base_url"] = app.config["BASE_URL"]
    app.jinja_env.globals["fonts"] = render.FONTS
    try:
        tz = ZoneInfo(app.config["DISPLAY_TZ"])
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")

    def localtime(iso, fmt="%d %b %Y, %H:%M"):
        if not iso:
            return ""
        try:
            return datetime.fromisoformat(iso).astimezone(tz).strftime(fmt)
        except ValueError:
            return iso

    app.jinja_env.filters["localtime"] = localtime

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            if not sent or not hmac.compare_digest(sent, session.get("_csrf", "")):
                abort(400, "Your form expired. Go back, refresh the page and try again.")

    @app.after_request
    def headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        return resp

    def admin_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if not session.get("admin"):
                return redirect(url_for("admin_login", next=request.path))
            return view(*a, **kw)
        return wrapped

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(413)
    @app.errorhandler(429)
    def http_error(err):
        msgs = {404: "We couldn't find that page.", 413: "That file is too large.",
                429: "Too many attempts. Please wait a minute and try again."}
        message = msgs.get(err.code) or getattr(err, "description", "Something went wrong.")
        return render_template("public/message.html", title="Oops", message=message), err.code

    # ================================================================ public

    @app.get("/")
    def home():
        # participants arrive through their session's QR link, so the bare address is for organisers
        return redirect(url_for("admin_home"))

    @app.route("/r/<slug>", methods=["GET", "POST"])
    def register(slug):
        ev = event_by_slug(slug)
        if ev["status"] != "open" or not ev["template_file"]:
            return render_template("public/closed.html", ev=ev)
        pending = session.get("pending", {})
        form = {"full_name": pending.get("full_name", "") if pending.get("slug") == slug else "",
                "email": pending.get("email", "") if pending.get("slug") == slug else "",
                "access_code": ""}
        errors = {}
        if request.method == "POST":
            if not limiter.allow(("reg", client_ip()), app.config["REGISTER_LIMIT_PER_MIN"]):
                abort(429)
            form["full_name"] = clean_name(request.form.get("full_name"))
            form["email"] = (request.form.get("email") or "").strip().lower()
            form["access_code"] = (request.form.get("access_code") or "").strip()
            if len(form["full_name"]) < 2 or not re.search(r"[^\W\d_]", form["full_name"]):
                errors["full_name"] = "Enter your full name."
            elif len(form["full_name"]) > 80:
                errors["full_name"] = "That name is too long (80 characters max)."
            if not EMAIL_RE.match(form["email"]) or len(form["email"]) > 254:
                errors["email"] = "Enter a valid email address."
            if ev["access_code"] and form["access_code"].lower() != ev["access_code"].lower():
                errors["access_code"] = "That attendance code isn't right. Ask the organiser."
            if not errors:
                session["pending"] = {"slug": slug, "full_name": form["full_name"], "email": form["email"],
                                      "ok": True}
                return redirect(url_for("confirm", slug=slug))
        return render_template("public/register.html", ev=ev, form=form, errors=errors)

    def pending_for(slug):
        p = session.get("pending")
        if not p or p.get("slug") != slug or not p.get("ok"):
            return None
        return p

    @app.get("/r/<slug>/preview.png")
    def public_preview(slug):
        ev = event_by_slug(slug)
        p = pending_for(slug)
        if p is None or not ev["template_file"]:
            abort(404)
        if not limiter.allow(("prev", client_ip()), 30):
            abort(429)
        pdf = render.render_certificate(ev, p["full_name"], tpl_dir)
        return Response(render.pdf_to_png(pdf, width=1000), mimetype="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.route("/r/<slug>/confirm", methods=["GET", "POST"])
    def confirm(slug):
        ev = event_by_slug(slug)
        if ev["status"] != "open" or not ev["template_file"]:
            return render_template("public/closed.html", ev=ev)
        p = pending_for(slug)
        if p is None:
            return redirect(url_for("register", slug=slug))
        if request.method == "GET":
            return render_template("public/confirm.html", ev=ev, p=p, t=int(time.time()))

        if not limiter.allow(("confirm", client_ip()), app.config["REGISTER_LIMIT_PER_MIN"]):
            abort(429)
        db = get_db()
        existing = db.execute("SELECT * FROM participants WHERE event_id=? AND email=?",
                              (ev["id"], p["email"])).fetchone()
        session.pop("pending", None)
        if existing:
            # Don't reveal the download link to whoever typed the email; re-send to the inbox instead.
            if existing["email_status"] in ("sent", "failed"):
                db.execute("UPDATE participants SET email_status='queued', email_attempts=0, next_attempt_at=NULL "
                           "WHERE id=?", (existing["id"],))
                db.commit()
                wake_mailer()
            return render_template("public/already.html", ev=ev, email=p["email"])

        full_name = render.format_name(p["full_name"], ev["name_case"])
        code = new_code(db)
        token = secrets.token_urlsafe(18)
        pdf = render.render_certificate(ev, full_name, tpl_dir)
        pdf_name = f"{ev['id']}-{code}.pdf"
        (cert_dir / pdf_name).write_bytes(pdf)
        try:
            db.execute(
                "INSERT INTO participants (event_id, full_name, email, cert_code, token, pdf_file, ip, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (ev["id"], full_name, p["email"], code, token, pdf_name, client_ip(), now_iso()))
            db.commit()
        except Exception:  # race: same email submitted twice at once
            db.rollback()
            (cert_dir / pdf_name).unlink(missing_ok=True)
            return render_template("public/already.html", ev=ev, email=p["email"])
        wake_mailer()
        return redirect(url_for("done", slug=slug, code=code, token=token))

    @app.get("/r/<slug>/done/<code>/<token>")
    def done(slug, code, token):
        ev = event_by_slug(slug)
        p = get_db().execute("SELECT * FROM participants WHERE cert_code=? AND event_id=?", (code, ev["id"])).fetchone()
        if p is None or not hmac.compare_digest(p["token"], token):
            abort(404)
        return render_template("public/done.html", ev=ev, p=p, texts=done_texts(ev, p["full_name"]))

    @app.get("/r/<slug>/status/<code>/<token>")
    def mail_status(slug, code, token):
        p = get_db().execute("SELECT * FROM participants WHERE cert_code=?", (code,)).fetchone()
        if p is None or not hmac.compare_digest(p["token"], token):
            abort(404)
        return jsonify(status=p["email_status"])

    @app.post("/r/<slug>/resend/<code>/<token>")
    def public_resend(slug, code, token):
        db = get_db()
        p = db.execute("SELECT * FROM participants WHERE cert_code=?", (code,)).fetchone()
        if p is None or not hmac.compare_digest(p["token"], token):
            abort(404)
        if not limiter.allow(("resend", code), 2, window=600):
            flash("We've already re-sent it. Please check your spam folder.", "warn")
        elif p["email_status"] in ("sent", "failed"):
            db.execute("UPDATE participants SET email_status='queued', email_attempts=0, next_attempt_at=NULL "
                       "WHERE id=?", (p["id"],))
            db.commit()
            wake_mailer()
            flash("Sending it again now.", "ok")
        return redirect(url_for("done", slug=slug, code=code, token=token))

    @app.get("/c/<code>/<token>")
    def download(code, token):
        p = get_db().execute("SELECT * FROM participants WHERE cert_code=?", (code,)).fetchone()
        if p is None or not hmac.compare_digest(p["token"], token):
            abort(404)
        return send_file(pdf_path(p), mimetype="application/pdf", as_attachment=request.args.get("dl") == "1",
                         download_name=f"Certificate_{safe_filename(p['full_name'])}.pdf")

    def pdf_path(p):
        """Path of a participant's certificate, making it now if the background worker hasn't yet."""
        if p["pdf_file"] and (cert_dir / p["pdf_file"]).exists():
            return cert_dir / p["pdf_file"]
        return cert_dir / mailer.make_pdf(get_db(), p["id"], tpl_dir, cert_dir)

    @app.route("/healthz")
    def healthz():
        get_db().execute("SELECT 1")
        return "ok"

    # ================================================================ admin: auth

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        error = None
        if request.method == "POST":
            if not limiter.allow(("login", client_ip()), 8, window=300):
                abort(429)
            if hmac.compare_digest(request.form.get("password", ""), app.config["ADMIN_PASSWORD"]):
                session.clear()
                session["admin"] = True
                session.permanent = True
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/admin") else url_for("admin_home"))
            error = "Wrong password."
        return render_template("admin/login.html", error=error,
                               default_pw=app.config["ADMIN_PASSWORD"] == "change-me")

    @app.post("/admin/logout")
    def admin_logout():
        session.clear()
        return redirect(url_for("admin_login"))

    # ================================================================ admin: sessions

    @app.get("/admin")
    @admin_required
    def admin_home():
        rows = get_db().execute(
            "SELECT e.*, "
            " (SELECT COUNT(*) FROM participants p WHERE p.event_id=e.id) AS n_total,"
            " (SELECT COUNT(*) FROM participants p WHERE p.event_id=e.id AND p.email_status='sent') AS n_sent,"
            " (SELECT COUNT(*) FROM participants p WHERE p.event_id=e.id AND p.email_status='failed') AS n_failed "
            "FROM events e ORDER BY e.id DESC").fetchall()
        total_sent = sum(r["n_sent"] for r in rows)
        return render_template("admin/sessions.html", events=rows, total_sent=total_sent,
                               smtp=bool(mailer.mail_settings(get_db(), app.config)["SMTP_HOST"]))

    def save_template_upload(ev_id, file):
        data = file.read()
        info = render.process_template(data, file.filename, tpl_dir, f"event-{ev_id}-{secrets.token_hex(4)}")
        return info

    def session_form(ev=None):
        db = get_db()
        form = dict(ev) if ev else {"name": "", "event_date": "", "slug": "", "access_code": "", "status": "open",
                                    "email_subject": mailer.DEFAULT_SUBJECT, "email_body": mailer.DEFAULT_BODY,
                                    "done_heading": "", "done_title": "", "done_message": "",
                                    "done_button_label": "", "done_button_url": "",
                                    "done_show_download": 1, "done_show_view": 1}
        errors = {}
        if request.method == "POST":
            form["done_heading"] = (request.form.get("done_heading") or "").strip()[:120]
            form["done_title"] = (request.form.get("done_title") or "").strip()[:160]
            form["done_message"] = (request.form.get("done_message") or "").replace("\r\n", "\n").strip()[:3000]
            form["done_button_label"] = (request.form.get("done_button_label") or "").strip()[:60]
            form["done_button_url"] = (request.form.get("done_button_url") or "").strip()[:500]
            form["done_show_download"] = 1 if request.form.get("done_show_download") else 0
            form["done_show_view"] = 1 if request.form.get("done_show_view") else 0
            if form["done_button_url"] and not re.match(r"^https?://\S+$", form["done_button_url"]):
                errors["done_button_url"] = "Use a full web address starting with https://"
            if bool(form["done_button_label"]) != bool(form["done_button_url"]):
                errors["done_button_url"] = "Fill in both the button text and its link, or leave both empty."
            form["name"] = (request.form.get("name") or "").strip()[:120]
            form["event_date"] = (request.form.get("event_date") or "").strip()[:40]
            form["slug"] = (request.form.get("slug") or "").strip().lower()
            form["access_code"] = (request.form.get("access_code") or "").strip()[:40]
            form["status"] = "closed" if request.form.get("status") == "closed" else "open"
            form["email_subject"] = (request.form.get("email_subject") or mailer.DEFAULT_SUBJECT).strip()[:200]
            form["email_body"] = (request.form.get("email_body") or mailer.DEFAULT_BODY).replace("\r\n", "\n")[:5000]
            if not form["name"]:
                errors["name"] = "Give the session a name."
            if not form["slug"]:
                form["slug"] = unique_slug(db, slugify(form["name"]), ev["id"] if ev else None)
            elif not SLUG_RE.match(form["slug"]):
                errors["slug"] = "Use 3-60 lowercase letters, numbers and dashes."
            else:
                taken = db.execute("SELECT id FROM events WHERE slug=?", (form["slug"],)).fetchone()
                if taken and (not ev or taken["id"] != ev["id"]):
                    errors["slug"] = "Another session already uses this link."
            upload = request.files.get("template")
            has_upload = upload is not None and upload.filename
            if not ev and not has_upload:
                errors["template"] = "Upload the certificate design (PNG, JPG or PDF)."
            if not errors:
                if ev is None:
                    cur = db.execute(
                        "INSERT INTO events (slug, name, event_date, status, access_code, email_subject, email_body, "
                        "created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (form["slug"], form["name"], form["event_date"], form["status"], form["access_code"] or None,
                         form["email_subject"], form["email_body"], now_iso()))
                    ev_id = cur.lastrowid
                else:
                    ev_id = ev["id"]
                    db.execute("UPDATE events SET slug=?, name=?, event_date=?, status=?, access_code=?, "
                               "email_subject=?, email_body=? WHERE id=?",
                               (form["slug"], form["name"], form["event_date"], form["status"],
                                form["access_code"] or None, form["email_subject"], form["email_body"], ev_id))
                db.execute("UPDATE events SET " + ", ".join(f"{k}=?" for k in DONE_FIELDS) + " WHERE id=?",
                           [form[k] for k in DONE_FIELDS] + [ev_id])
                if has_upload:
                    try:
                        info = save_template_upload(ev_id, upload)
                    except render.TemplateError as exc:
                        db.rollback()
                        errors["template"] = str(exc)
                        return render_template("admin/session_form.html", ev=ev, form=form, errors=errors)
                    old = [ev["template_file"], ev["preview_file"]] if ev else []
                    db.execute("UPDATE events SET template_file=?, template_kind=?, page_w=?, page_h=?, preview_file=? "
                               "WHERE id=?", (info["template_file"], info["template_kind"], info["page_w"],
                                              info["page_h"], info["preview_file"], ev_id))
                    for f in old:
                        if f:
                            (tpl_dir / f).unlink(missing_ok=True)
                db.commit()
                if ev is None or has_upload:
                    flash("Template uploaded. Now drag the name box to where the name should print.", "ok")
                    return redirect(url_for("admin_design", event_id=ev_id))
                flash("Session saved.", "ok")
                return redirect(url_for("admin_participants", event_id=ev_id))
        return render_template("admin/session_form.html", ev=ev, form=form, errors=errors)

    @app.route("/admin/sessions/new", methods=["GET", "POST"])
    @admin_required
    def admin_new():
        return session_form(None)

    @app.route("/admin/sessions/<int:event_id>/edit", methods=["GET", "POST"])
    @admin_required
    def admin_edit(event_id):
        return session_form(event_by_id(event_id))

    @app.route("/admin/sessions/<int:event_id>/design", methods=["GET", "POST"])
    @admin_required
    def admin_design(event_id):
        ev = event_by_id(event_id)
        if not ev["template_file"]:
            flash("Upload a certificate design first.", "warn")
            return redirect(url_for("admin_edit", event_id=event_id))
        if request.method == "POST":
            f = request.form
            font = f.get("name_font") if f.get("name_font") in render.FONTS else render.DEFAULT_FONT
            color = f.get("name_color") if HEX_RE.match(f.get("name_color", "")) else "#1E2A4A"
            align = f.get("name_align") if f.get("name_align") in ("left", "center", "right") else "center"
            case = f.get("name_case") if f.get("name_case") in ("as_typed", "upper", "title") else "as_typed"
            get_db().execute(
                "UPDATE events SET name_x=?, name_y=?, name_w=?, name_size=?, name_font=?, name_color=?, "
                "name_align=?, name_case=? WHERE id=?",
                (fnum(f.get("name_x"), .5, 0, 1), fnum(f.get("name_y"), .5, 0, 1), fnum(f.get("name_w"), .6, .05, 1),
                 fnum(f.get("name_size"), .07, .01, .3), font, color, align, case, event_id))
            get_db().commit()
            flash("Name position saved.", "ok")
            if f.get("then") == "qr":
                return redirect(url_for("admin_qr", event_id=event_id))
            return redirect(url_for("admin_design", event_id=event_id))
        return render_template("admin/design.html", ev=ev)

    @app.get("/admin/sessions/<int:event_id>/template.png")
    @admin_required
    def admin_template_png(event_id):
        ev = event_by_id(event_id)
        if not ev["preview_file"]:
            abort(404)
        return send_file(tpl_dir / ev["preview_file"], mimetype="image/png", max_age=3600)

    @app.get("/admin/sessions/<int:event_id>/sample.pdf")
    @admin_required
    def admin_sample(event_id):
        ev = dict(event_by_id(event_id))
        a = request.args
        if a:  # unsaved editor values, so admins can test before saving
            ev.update(name_x=fnum(a.get("name_x"), ev["name_x"], 0, 1), name_y=fnum(a.get("name_y"), ev["name_y"], 0, 1),
                      name_w=fnum(a.get("name_w"), ev["name_w"], .05, 1),
                      name_size=fnum(a.get("name_size"), ev["name_size"], .01, .3),
                      name_font=a.get("name_font") if a.get("name_font") in render.FONTS else ev["name_font"],
                      name_color=a.get("name_color") if HEX_RE.match(a.get("name_color", "")) else ev["name_color"],
                      name_align=a.get("name_align", ev["name_align"]), name_case=a.get("name_case", ev["name_case"]))
        name = clean_name(a.get("sample")) or "Nur Aisyah binti Rahman"
        pdf = render.render_certificate(ev, name, tpl_dir)
        return Response(pdf, mimetype="application/pdf", headers={"Cache-Control": "no-store"})

    @app.get("/admin/sessions/<int:event_id>/qr")
    @admin_required
    def admin_qr(event_id):
        ev = event_by_id(event_id)
        link = f"{app.config['BASE_URL']}/r/{ev['slug']}"
        return render_template("admin/qr.html", ev=ev, link=link, qr_svg=render.qr_svg(link))

    @app.get("/admin/sessions/<int:event_id>/qr.<fmt>")
    @admin_required
    def admin_qr_file(event_id, fmt):
        ev = event_by_id(event_id)
        link = f"{app.config['BASE_URL']}/r/{ev['slug']}"
        if fmt == "png":
            return Response(render.qr_png(link, box=24), mimetype="image/png",
                            headers={"Content-Disposition": f'attachment; filename="qr-{ev["slug"]}.png"'})
        if fmt == "svg":
            return Response(render.qr_svg(link), mimetype="image/svg+xml",
                            headers={"Content-Disposition": f'attachment; filename="qr-{ev["slug"]}.svg"'})
        abort(404)

    @app.post("/admin/sessions/<int:event_id>/toggle")
    @admin_required
    def admin_toggle(event_id):
        ev = event_by_id(event_id)
        new = "closed" if ev["status"] == "open" else "open"
        get_db().execute("UPDATE events SET status=? WHERE id=?", (new, event_id))
        get_db().commit()
        flash(f"Registration {'closed' if new == 'closed' else 'opened'} for {ev['name']}.", "ok")
        return redirect(request.form.get("back") or url_for("admin_home"))

    @app.post("/admin/sessions/<int:event_id>/duplicate")
    @admin_required
    def admin_duplicate(event_id):
        ev = event_by_id(event_id)
        db = get_db()
        name = f"{ev['name']} (copy)"
        slug = unique_slug(db, slugify(name))
        copied = ("name_x", "name_y", "name_w", "name_size", "name_font", "name_color", "name_align", "name_case",
                  "email_subject", "email_body") + DONE_FIELDS
        cur = db.execute(
            f"INSERT INTO events (slug, name, event_date, status, access_code, created_at, {', '.join(copied)}) "
            f"VALUES ({', '.join('?' * (6 + len(copied)))})",
            [slug, name, "", "closed", None, now_iso()] + [ev[k] for k in copied])
        new_id = cur.lastrowid
        if ev["template_file"]:
            stem = f"event-{new_id}-{secrets.token_hex(4)}"
            ext = Path(ev["template_file"]).suffix
            shutil.copy(tpl_dir / ev["template_file"], tpl_dir / f"{stem}{ext}")
            shutil.copy(tpl_dir / ev["preview_file"], tpl_dir / f"{stem}-preview.png")
            db.execute("UPDATE events SET template_file=?, template_kind=?, page_w=?, page_h=?, preview_file=? WHERE id=?",
                       (f"{stem}{ext}", ev["template_kind"], ev["page_w"], ev["page_h"], f"{stem}-preview.png", new_id))
        db.commit()
        flash("Session duplicated with the same design. Rename it and open registration when ready.", "ok")
        return redirect(url_for("admin_edit", event_id=new_id))

    @app.post("/admin/sessions/<int:event_id>/delete")
    @admin_required
    def admin_delete(event_id):
        ev = event_by_id(event_id)
        if request.form.get("confirm_name", "").strip() != ev["name"]:
            flash("Type the session name exactly to confirm deleting it.", "warn")
            return redirect(url_for("admin_edit", event_id=event_id))
        db = get_db()
        for p in db.execute("SELECT pdf_file FROM participants WHERE event_id=?", (event_id,)):
            if p["pdf_file"]:
                (cert_dir / p["pdf_file"]).unlink(missing_ok=True)
        for f in (ev["template_file"], ev["preview_file"]):
            if f:
                (tpl_dir / f).unlink(missing_ok=True)
        db.execute("DELETE FROM participants WHERE event_id=?", (event_id,))
        db.execute("DELETE FROM events WHERE id=?", (event_id,))
        db.commit()
        flash(f"Deleted {ev['name']} and its certificates.", "ok")
        return redirect(url_for("admin_home"))

    # ================================================================ admin: participants

    @app.get("/admin/sessions/<int:event_id>/participants")
    @admin_required
    def admin_participants(event_id):
        ev = event_by_id(event_id)
        q = (request.args.get("q") or "").strip()
        status = request.args.get("status", "")
        sql = "SELECT * FROM participants WHERE event_id=?"
        args = [event_id]
        if q:
            sql += " AND (full_name LIKE ? OR email LIKE ?)"
            args += [f"%{q}%"] * 2
        if status in EMAIL_STATUSES:
            sql += " AND email_status=?"
            args.append(status)
        rows = get_db().execute(sql + " ORDER BY id DESC LIMIT 2000", args).fetchall()
        counts = {r["email_status"]: r["n"] for r in get_db().execute(
            "SELECT email_status, COUNT(*) n FROM participants WHERE event_id=? GROUP BY email_status", (event_id,))}
        making = get_db().execute("SELECT COUNT(*) FROM participants WHERE event_id=? AND pdf_file IS NULL",
                                  (event_id,)).fetchone()[0]
        return render_template("admin/participants.html", ev=ev, rows=rows, q=q, status=status, counts=counts,
                               total=sum(counts.values()), making=making, statuses=EMAIL_STATUSES)

    @app.get("/admin/sessions/<int:event_id>/participants.csv")
    @admin_required
    def admin_csv(event_id):
        ev = event_by_id(event_id)
        rows = get_db().execute("SELECT * FROM participants WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
        buf = io.StringIO()
        buf.write("﻿")  # so Excel reads UTF-8 names correctly
        w = csv.writer(buf)
        zone = app.config["DISPLAY_TZ"]
        w.writerow(["Full name", "Email", f"Added at ({zone})", "Email status", f"Email sent at ({zone})",
                    "Download link"])
        base = app.config["BASE_URL"]
        for r in rows:
            w.writerow([r["full_name"], r["email"] or "", localtime(r["created_at"], "%Y-%m-%d %H:%M"),
                        EMAIL_STATUSES.get(r["email_status"], r["email_status"]),
                        localtime(r["email_sent_at"], "%Y-%m-%d %H:%M"), f"{base}/c/{r['cert_code']}/{r['token']}"])
        return Response(buf.getvalue(), mimetype="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="participants-{ev["slug"]}.csv"'})

    def participant_or_404(pid):
        p = get_db().execute("SELECT * FROM participants WHERE id=?", (pid,)).fetchone()
        if p is None:
            abort(404)
        return p

    @app.post("/admin/participants/<int:pid>/resend")
    @admin_required
    def admin_resend(pid):
        p = participant_or_404(pid)
        if not p["email"]:
            flash(f"{p['full_name']} has no email address. Add one with Fix first.", "warn")
            return redirect(url_for("admin_participants", event_id=p["event_id"]))
        get_db().execute("UPDATE participants SET email_status='queued', email_attempts=0, next_attempt_at=NULL, "
                         "email_error=NULL WHERE id=?", (pid,))
        get_db().commit()
        wake_mailer()
        flash(f"Re-sending to {p['email']}.", "ok")
        return redirect(url_for("admin_participants", event_id=p["event_id"]))

    @app.post("/admin/sessions/<int:event_id>/resend-failed")
    @admin_required
    def admin_resend_failed(event_id):
        n = get_db().execute("UPDATE participants SET email_status='queued', email_attempts=0, next_attempt_at=NULL "
                             "WHERE event_id=? AND email_status='failed'", (event_id,)).rowcount
        get_db().commit()
        wake_mailer()
        flash(f"Re-queued {n} failed email(s).", "ok")
        return redirect(url_for("admin_participants", event_id=event_id))

    @app.post("/admin/participants/<int:pid>/fix")
    @admin_required
    def admin_fix(pid):
        """Correct a typo in a name or email, regenerate the PDF and send it again (if there is an email)."""
        p = participant_or_404(pid)
        ev = event_by_id(p["event_id"])
        name = clean_name(request.form.get("full_name")) or p["full_name"]
        email = (request.form.get("email") or "").strip().lower() or None
        if email and not EMAIL_RE.match(email):
            flash("That email address isn't valid.", "warn")
            return redirect(url_for("admin_participants", event_id=ev["id"]))
        db = get_db()
        if email:
            clash = db.execute("SELECT id FROM participants WHERE event_id=? AND email=? AND id<>?",
                               (ev["id"], email, pid)).fetchone()
            if clash:
                flash("Another participant in this session already uses that email.", "warn")
                return redirect(url_for("admin_participants", event_id=ev["id"]))
        pdf_name = p["pdf_file"] or f"{ev['id']}-{p['cert_code']}.pdf"
        (cert_dir / pdf_name).write_bytes(render.render_certificate(ev, name, tpl_dir))
        db.execute("UPDATE participants SET full_name=?, email=?, pdf_file=?, email_status=?, email_attempts=0, "
                   "next_attempt_at=NULL, email_error=NULL WHERE id=?",
                   (name, email, pdf_name, "queued" if email else "none", pid))
        db.commit()
        if email:
            wake_mailer()
            flash(f"Updated and re-sent the certificate to {email}.", "ok")
        else:
            flash(f"Updated the certificate for {name}.", "ok")
        return redirect(url_for("admin_participants", event_id=ev["id"]))

    @app.post("/admin/participants/<int:pid>/delete")
    @admin_required
    def admin_delete_participant(pid):
        p = participant_or_404(pid)
        if p["pdf_file"]:
            (cert_dir / p["pdf_file"]).unlink(missing_ok=True)
        get_db().execute("DELETE FROM participants WHERE id=?", (pid,))
        get_db().commit()
        flash(f"Removed {p['full_name']}. Their certificate link no longer works.", "ok")
        return redirect(url_for("admin_participants", event_id=p["event_id"]))

    @app.get("/admin/participants/<int:pid>/pdf")
    @admin_required
    def admin_pdf(pid):
        p = participant_or_404(pid)
        return send_file(pdf_path(p), mimetype="application/pdf", as_attachment=request.args.get("dl") == "1",
                         download_name=f"Certificate_{safe_filename(p['full_name'])}.pdf")

    @app.route("/admin/sessions/<int:event_id>/certificates.zip", methods=["GET", "POST"])
    @admin_required
    def admin_zip(event_id):
        """All of a session's certificates in one ZIP, or only the ticked ones (POST ids)."""
        ev = event_by_id(event_id)
        db = get_db()
        if request.method == "POST":
            ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
            if not ids:
                flash("Tick at least one participant to download.", "warn")
                return redirect(url_for("admin_participants", event_id=event_id))
            rows = db.execute(f"SELECT * FROM participants WHERE event_id=? AND id IN ({','.join('?' * len(ids))}) "
                              "ORDER BY full_name", [event_id] + ids).fetchall()
        else:
            rows = db.execute("SELECT * FROM participants WHERE event_id=? ORDER BY full_name", (event_id,)).fetchall()
        if not rows:
            flash("There are no certificates to download yet.", "warn")
            return redirect(url_for("admin_participants", event_id=event_id))
        waiting = sum(1 for p in rows if not p["pdf_file"])
        if waiting and app.extensions.get("mail_worker"):
            # making hundreds here would outlast the server's request time limit; the worker is on it
            flash(f"{waiting} of these certificates are still being made. Try again in a minute.", "warn")
            return redirect(url_for("admin_participants", event_id=event_id))
        # built on disk, not in memory: a few hundred certificates can be larger than the server's RAM
        out = tempfile.TemporaryFile()
        used = set()
        with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as zf:  # PDFs are already compressed
            for p in rows:
                base = f"Certificate_{safe_filename(p['full_name'])}"
                name, n = f"{base}.pdf", 2
                while name.lower() in used:
                    name, n = f"{base}_{n}.pdf", n + 1
                used.add(name.lower())
                zf.write(pdf_path(p), name)
        out.seek(0)
        return send_file(out, mimetype="application/zip", as_attachment=True,
                         download_name=f"certificates-{ev['slug']}.zip")

    @app.route("/admin/sessions/<int:event_id>/import", methods=["GET", "POST"])
    @admin_required
    def admin_import(event_id):
        """Create certificates for every name in an Excel or CSV file."""
        ev = event_by_id(event_id)
        if not ev["template_file"]:
            flash("Upload a certificate design first.", "warn")
            return redirect(url_for("admin_edit", event_id=event_id))
        result = None
        if request.method == "POST":
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                flash("Choose an Excel or CSV file to upload.", "warn")
                return redirect(url_for("admin_import", event_id=event_id))
            try:
                people, problems = bulk.read_people(upload.read(), upload.filename, EMAIL_RE)
            except bulk.BulkImportError as exc:
                flash(str(exc), "warn")
                return redirect(url_for("admin_import", event_id=event_id))
            send = bool(request.form.get("send_email"))
            db = get_db()
            added = 0
            for person in people:
                name = render.format_name(clean_name(person["name"]), ev["name_case"])
                email = person["email"] or None
                if len(name) < 2 or not re.search(r"[^\W\d_]", name):
                    problems.append((person["row"], f"“{person['name'] or '(empty)'}” isn't a name, skipped"))
                    continue
                if len(name) > 80:
                    problems.append((person["row"], f"“{name[:30]}…” is longer than 80 characters, skipped"))
                    continue
                # skip people already in this session, so uploading the same file twice adds nobody twice
                if email:
                    dup = db.execute("SELECT 1 FROM participants WHERE event_id=? AND email=?",
                                     (event_id, email)).fetchone()
                else:
                    dup = db.execute("SELECT 1 FROM participants WHERE event_id=? AND email IS NULL AND full_name=?",
                                     (event_id, name)).fetchone()
                if dup:
                    problems.append((person["row"], f"{name} is already in this session, skipped"))
                    continue
                db.execute("INSERT INTO participants (event_id, full_name, email, cert_code, token, created_at, "
                           "email_status) VALUES (?,?,?,?,?,?,?)",
                           (event_id, name, email, new_code(db), secrets.token_urlsafe(18), now_iso(),
                            "queued" if send and email else "none"))
                added += 1
            db.commit()
            wake_mailer()
            problems.sort()
            result = {"added": added, "problems": problems,
                      "emailing": send and any(p["email"] for p in people)}
        return render_template("admin/import.html", ev=ev, result=result)

    @app.get("/admin/import-example.csv")
    @admin_required
    def admin_import_example():
        return Response(bulk.example_csv(), mimetype="text/csv",
                        headers={"Content-Disposition": 'attachment; filename="names-example.csv"'})

    @app.get("/admin/sessions/<int:event_id>/done-preview")
    @admin_required
    def admin_done_preview(event_id):
        """The thank-you page as a participant sees it, with a sample name."""
        ev = event_by_id(event_id)
        p = {"full_name": "Nur Aisyah binti Rahman", "email": "aisyah@example.com", "email_status": "sent",
             "cert_code": "", "token": ""}
        return render_template("public/done.html", ev=ev, p=p, texts=done_texts(ev, p["full_name"]), preview=True)

    # ================================================================ admin: email

    @app.route("/admin/email", methods=["GET", "POST"])
    @admin_required
    def admin_email():
        db = get_db()
        result, errors, form = None, {}, None
        action = request.form.get("action") if request.method == "POST" else None
        if action == "save":
            form = {k: (request.form.get(k) or "").strip() for k in (
                "from_name", "from_email", "reply_to", "smtp_host", "smtp_port", "smtp_user", "security")}
            security = form["security"] if form["security"] in ("starttls", "ssl", "none") else "starttls"
            if not EMAIL_RE.match(form["from_email"]):
                errors["from_email"] = "Enter the address emails should come from."
            if form["reply_to"] and not EMAIL_RE.match(form["reply_to"]):
                errors["reply_to"] = "Enter a valid address, or leave it empty."
            port = form["smtp_port"] or ("465" if security == "ssl" else "587")
            if form["smtp_host"] and not (port.isdigit() and 0 < int(port) < 65536):
                errors["smtp_port"] = "Use a port number such as 587 or 465."
            if not errors:
                values = {"MAIL_FROM": mailer.format_sender(form["from_name"][:100], form["from_email"]),
                          "MAIL_REPLY_TO": form["reply_to"], "SMTP_HOST": form["smtp_host"],
                          "SMTP_PORT": int(port), "SMTP_USER": form["smtp_user"],
                          "SMTP_SSL": security == "ssl", "SMTP_STARTTLS": security == "starttls"}
                # the password field is left blank to keep the saved one
                if request.form.get("smtp_password", "").strip():
                    values["SMTP_PASSWORD"] = request.form["smtp_password"].strip()
                mailer.save_mail_settings(db, app.config["SECRET_KEY"], values)
                flash("Email settings saved. New emails use them straight away.", "ok")
                return redirect(url_for("admin_email"))
        cfg = mailer.mail_settings(db, app.config)
        if action == "test":
            to = (request.form.get("to") or "").strip()
            if not EMAIL_RE.match(to):
                result = ("warn", "Enter a valid address to send the test to.")
            else:
                from email.message import EmailMessage
                msg = EmailMessage()
                msg["Subject"] = f"{app.config['APP_NAME']} test email"
                msg["From"] = cfg["MAIL_FROM"]
                msg["To"] = to
                if cfg["MAIL_REPLY_TO"]:
                    msg["Reply-To"] = cfg["MAIL_REPLY_TO"]
                msg.set_content("If you can read this, certificate emails will be delivered.")
                try:
                    mailer.deliver(cfg, msg, Path(app.config["DATA_DIR"]) / "outbox")
                    result = ("ok", f"Test email sent to {to}." if cfg["SMTP_HOST"]
                              else "Outbox mode: the test email was saved to data/outbox instead of being sent.")
                except Exception as exc:  # noqa: BLE001
                    result = ("warn", f"Sending failed: {exc.__class__.__name__}: {exc}")
        if form is None:
            name, addr = parseaddr(cfg["MAIL_FROM"])
            form = {"from_name": name, "from_email": addr, "reply_to": cfg["MAIL_REPLY_TO"],
                    "smtp_host": cfg["SMTP_HOST"], "smtp_port": str(cfg["SMTP_PORT"]), "smtp_user": cfg["SMTP_USER"],
                    "security": "ssl" if cfg["SMTP_SSL"] else ("starttls" if cfg["SMTP_STARTTLS"] else "none")}
        stats = {r["email_status"]: r["n"] for r in db.execute(
            "SELECT email_status, COUNT(*) n FROM participants GROUP BY email_status")}
        return render_template("admin/email.html", result=result, stats=stats, cfg=cfg, form=form, errors=errors,
                               has_password=bool(cfg["SMTP_PASSWORD"]))

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
                    debug=os.environ.get("FLASK_DEBUG") == "1", use_reloader=False)
