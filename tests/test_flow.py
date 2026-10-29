"""End-to-end test of the whole flow. Run:  python -m pytest -q tests  (or python tests/test_flow.py)"""
import io
import re
import sys
import tempfile
from email import policy
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pypdf import PdfReader  # noqa: E402
from reportlab.pdfgen import canvas  # noqa: E402

import mailer  # noqa: E402
from app import create_app  # noqa: E402
from db import connect  # noqa: E402


def csrf(client, url):
    html = client.get(url).get_data(as_text=True)
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert m, f"no csrf token on {url}"
    return m.group(1)


def make_app(tmp):
    return create_app({"DATA_DIR": Path(tmp), "RUN_MAIL_WORKER": False, "ADMIN_PASSWORD": "pw",
                       "BASE_URL": "https://cert.test", "SMTP_HOST": "", "TESTING": True})


def drain(app):
    w = mailer.MailWorker(app)
    conn = connect(Path(app.config["DATA_DIR"]) / "certqr.db")
    n = 0
    while w.make_one_pdf(conn) or w.process_one(conn):
        n += 1
    conn.close()
    return n


def test_full_flow():
    # Windows can't delete PDFs the test client still holds open; ignore that at cleanup
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        app = make_app(tmp)
        admin = app.test_client()

        # login
        assert admin.get("/admin").status_code == 302
        t = csrf(admin, "/admin/login")
        assert admin.post("/admin/login", data={"csrf_token": t, "password": "nope"}).status_code == 200
        r = admin.post("/admin/login", data={"csrf_token": t, "password": "pw"})
        assert r.status_code == 302

        # create a session with the sample PNG template
        t = csrf(admin, "/admin/sessions/new")
        png = (ROOT / "samples" / "sample-template.png").read_bytes()
        r = admin.post("/admin/sessions/new", data={
            "csrf_token": t, "name": "Digital Skills Workshop 2026", "event_date": "27 September 2026",
            "slug": "", "status": "open", "email_subject": "Your certificate — {session}",
            "email_body": mailer.DEFAULT_BODY, "template": (io.BytesIO(png), "cert.png")},
            content_type="multipart/form-data")
        assert r.status_code == 302 and "/design" in r.headers["Location"], r.get_data(as_text=True)[:500]
        ev_id = int(re.search(r"/sessions/(\d+)/", r.headers["Location"]).group(1))

        # place the name
        t = csrf(admin, f"/admin/sessions/{ev_id}/design")
        r = admin.post(f"/admin/sessions/{ev_id}/design", data={
            "csrf_token": t, "name_x": "0.5", "name_y": "0.487", "name_w": "0.62", "name_size": "0.072",
            "name_font": "poppins-bold", "name_color": "#1E2A4A", "name_align": "center", "name_case": "title",
            "then": "qr"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/qr")
        qr_page = admin.get(f"/admin/sessions/{ev_id}/qr").get_data(as_text=True)
        assert "https://cert.test/r/digital-skills-workshop-2026" in qr_page and "<svg" in qr_page
        assert admin.get(f"/admin/sessions/{ev_id}/qr.png").data[:4] == b"\x89PNG"
        sample = admin.get(f"/admin/sessions/{ev_id}/sample.pdf?sample=Test+Person")
        assert sample.data[:4] == b"%PDF"

        # participant registers
        user = app.test_client()
        url = "/r/digital-skills-workshop-2026"
        t = csrf(user, url)
        r = user.post(url, data={"csrf_token": t, "full_name": "", "email": "bad"})
        assert "Enter your full name" in r.get_data(as_text=True)
        r = user.post(url, data={"csrf_token": t, "full_name": "  nur aisyah   BINTI rahman ", "email": "Aisyah@Example.com"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/confirm")
        page = user.get(url + "/confirm").get_data(as_text=True)
        assert "aisyah@example.com" in page
        prev = user.get(url + "/preview.png")
        assert prev.status_code == 200 and prev.data[:4] == b"\x89PNG"
        t = csrf(user, url + "/confirm")
        r = user.post(url + "/confirm", data={"csrf_token": t})
        assert r.status_code == 302 and "/done/" in r.headers["Location"]
        done_url = r.headers["Location"]
        code, token = done_url.rstrip("/").split("/")[-2:]
        assert "Certificate ready" in user.get(done_url).get_data(as_text=True)

        # PDF contains the formatted name and no certificate ID
        pdf = user.get(f"/c/{code}/{token}")
        assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
        text = PdfReader(io.BytesIO(pdf.data)).pages[0].extract_text()
        assert "Nur Aisyah binti Rahman" in " ".join(text.split()), text
        assert code not in text and "Certificate ID" not in text
        assert user.get(f"/c/{code}/wrongtoken").status_code == 404

        # email goes out (outbox mode) with the PDF attached
        assert drain(app) == 1
        emls = list((Path(tmp) / "outbox").glob("*.eml"))
        assert len(emls) == 1
        msg = BytesParser(policy=policy.default).parsebytes(emls[0].read_bytes())
        assert msg["To"] == "aisyah@example.com"
        assert msg["Subject"] == "Your certificate — Digital Skills Workshop 2026"
        atts = [p for p in msg.iter_attachments()]
        assert atts and atts[0].get_content_type() == "application/pdf"
        body_text = msg.get_body(("plain",)).get_content()
        assert f"https://cert.test/c/{code}/{token}" in body_text and "Certificate ID" not in body_text
        assert user.get(done_url.replace("/done/", "/status/")).get_json()["status"] == "sent"

        # duplicate registration: no link revealed, email re-sent
        other = app.test_client()
        t = csrf(other, url)
        other.post(url, data={"csrf_token": t, "full_name": "Someone Else", "email": "aisyah@example.com"})
        t = csrf(other, url + "/confirm")
        r = other.post(url + "/confirm", data={"csrf_token": t})
        body = r.get_data(as_text=True)
        assert "already have a certificate" in body and token not in body
        assert drain(app) == 1

        # the certificate ID verify page is gone
        assert user.get(f"/v/{code}").status_code == 404 and user.get("/v").status_code == 404

        # admin: list, CSV, fix typo
        people = admin.get(f"/admin/sessions/{ev_id}/participants").get_data(as_text=True)
        assert "Nur Aisyah" in people and code not in people and "Certificate ID" not in people
        csv_txt = admin.get(f"/admin/sessions/{ev_id}/participants.csv").get_data(as_text=True)
        assert "aisyah@example.com" in csv_txt and "Certificate ID" not in csv_txt and "Verify" not in csv_txt
        pid = int(re.search(r"/admin/participants/(\d+)/fix", people).group(1))
        t = csrf(admin, f"/admin/sessions/{ev_id}/participants")
        admin.post(f"/admin/participants/{pid}/fix", data={"csrf_token": t, "full_name": "Nur Aishah binti Rahman",
                                                           "email": "aisyah@example.com"})
        text = PdfReader(io.BytesIO(user.get(f"/c/{code}/{token}").data)).pages[0].extract_text()
        assert "Nur Aishah binti Rahman" in " ".join(text.split())
        assert drain(app) == 1

        # CSRF is enforced
        assert user.post(url, data={"full_name": "X Y", "email": "x@y.com"}).status_code == 400

        # closing registration
        t = csrf(admin, "/admin")
        admin.post(f"/admin/sessions/{ev_id}/toggle", data={"csrf_token": t})
        assert "Registration closed" in app.test_client().get(url).get_data(as_text=True)

        # second session: PDF template + attendance code + duplicate
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(842, 595))
        c.setFont("Helvetica-Bold", 40)
        c.drawCentredString(421, 420, "CERTIFICATE OF ATTENDANCE")
        c.showPage()
        c.save()
        t = csrf(admin, "/admin/sessions/new")
        r = admin.post("/admin/sessions/new", data={
            "csrf_token": t, "name": "Leadership Talk", "event_date": "3 Oct 2026", "slug": "leadership-talk",
            "access_code": "HALL-A", "status": "open", "email_subject": "", "email_body": "",
            "template": (io.BytesIO(buf.getvalue()), "design.pdf")}, content_type="multipart/form-data")
        assert r.status_code == 302
        u2 = app.test_client()
        t = csrf(u2, "/r/leadership-talk")
        r = u2.post("/r/leadership-talk", data={"csrf_token": t, "full_name": "Tan Wei Ming", "email": "wm@example.com",
                                                "access_code": "wrong"})
        assert "attendance code" in r.get_data(as_text=True)
        u2.post("/r/leadership-talk", data={"csrf_token": t, "full_name": "Tan Wei Ming", "email": "wm@example.com",
                                            "access_code": "hall-a"})
        t = csrf(u2, "/r/leadership-talk/confirm")
        r = u2.post("/r/leadership-talk/confirm", data={"csrf_token": t})
        loc = r.headers["Location"]
        c2, tk2 = loc.rstrip("/").split("/")[-2:]
        text = PdfReader(io.BytesIO(u2.get(f"/c/{c2}/{tk2}").data)).pages[0].extract_text()
        assert "CERTIFICATE OF ATTENDANCE" in text and "Tan Wei Ming" in text

        # duplicate session keeps the design
        t = csrf(admin, "/admin")
        r = admin.post(f"/admin/sessions/{ev_id}/duplicate", data={"csrf_token": t})
        assert r.status_code == 302
        dup_id = int(re.search(r"/sessions/(\d+)/", r.headers["Location"]).group(1))
        assert admin.get(f"/admin/sessions/{dup_id}/template.png").status_code == 200

        # delete session
        t = csrf(admin, f"/admin/sessions/{dup_id}/edit")
        admin.post(f"/admin/sessions/{dup_id}/delete", data={"csrf_token": t,
                                                             "confirm_name": "Digital Skills Workshop 2026 (copy)"})
        assert admin.get(f"/admin/sessions/{dup_id}/edit").status_code == 404
        print("all checks passed")


def test_email_settings():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        app = make_app(tmp)
        admin = app.test_client()
        t = csrf(admin, "/admin/login")
        admin.post("/admin/login", data={"csrf_token": t, "password": "pw"})

        # bad sender is rejected and nothing is saved
        t = csrf(admin, "/admin/email")
        r = admin.post("/admin/email", data={"csrf_token": t, "action": "save", "from_email": "nope"})
        assert "Enter the address emails should come from" in r.get_data(as_text=True)

        r = admin.post("/admin/email", data={
            "csrf_token": t, "action": "save", "from_name": "Thriving Talents", "from_email": "certs@tt.example",
            "reply_to": "hello@tt.example", "smtp_host": "smtp.example.com", "smtp_port": "",
            "security": "ssl", "smtp_user": "login", "smtp_password": "s3cret-key"})
        assert r.status_code == 302
        db_file = Path(tmp) / "certqr.db"
        conn = connect(db_file)
        cfg = mailer.mail_settings(conn, app.config)
        assert cfg["MAIL_FROM"] == '"Thriving Talents" <certs@tt.example>' and cfg["MAIL_REPLY_TO"] == "hello@tt.example"
        assert cfg["SMTP_HOST"] == "smtp.example.com" and cfg["SMTP_PORT"] == 465 and cfg["SMTP_SSL"] and not cfg["SMTP_STARTTLS"]
        assert cfg["SMTP_PASSWORD"] == "s3cret-key"
        assert b"s3cret-key" not in db_file.read_bytes()  # stored encrypted
        page = admin.get("/admin/email").get_data(as_text=True)
        assert "s3cret-key" not in page and "certs@tt.example" in page

        # a blank password keeps the saved one; an empty server switches back to outbox mode
        admin.post("/admin/email", data={
            "csrf_token": t, "action": "save", "from_name": "", "from_email": "new@tt.example",
            "smtp_host": "", "security": "starttls", "smtp_user": "login", "smtp_password": ""})
        cfg = mailer.mail_settings(conn, app.config)
        assert cfg["SMTP_PASSWORD"] == "s3cret-key" and cfg["MAIL_FROM"] == "new@tt.example" and not cfg["SMTP_HOST"]

        # the test email goes out with the saved sender
        r = admin.post("/admin/email", data={"csrf_token": t, "action": "test", "to": "me@tt.example"})
        assert "Outbox mode" in r.get_data(as_text=True)
        eml = next((Path(tmp) / "outbox").glob("*.eml")).read_bytes()
        assert BytesParser(policy=policy.default).parsebytes(eml)["From"] == "new@tt.example"

        # a changed SECRET_KEY can't read the saved password, and says so
        cfg = mailer.mail_settings(conn, {**app.config, "SECRET_KEY": "different"})
        assert cfg["PASSWORD_UNREADABLE"] and cfg["SMTP_PASSWORD"] == ""
        conn.close()
        print("email settings checks passed")


def test_long_name_shrinks():
    import render
    ev = {"page_w": 842, "page_h": 595, "name_x": .5, "name_y": .5, "name_w": .3, "name_size": .1,
          "name_font": "poppins-bold", "name_color": "#000000", "name_align": "center", "name_case": "as_typed",
          "show_cert_id": 0, "template_file": None, "template_kind": "image", "name": "x"}
    from reportlab.pdfbase import pdfmetrics
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(842, 595))
    render._draw_text_layer(c, ev, "Muhammad Aiman Hakimi bin Abdul Rahman Shah")
    assert pdfmetrics.stringWidth("x", render._font("poppins-bold"), 10) > 0
    assert render.format_name("ANAND a/l MURUGAN", "title") == "Anand a/l Murugan"

    # names that don't fit go onto two lines, split at a space, and each line fits the box
    font, max_w, size = render._font("poppins-bold"), 0.3 * 842, 0.1 * 595
    lines, fs = render.name_lines("Muhammad Aiman Hakimi bin Abdul Rahman Shah", font, size, max_w)
    assert len(lines) == 2 and lines[0] == lines[0].strip() and "bin" in " ".join(lines)
    assert all(pdfmetrics.stringWidth(s, font, fs) <= max_w + 0.01 for s in lines)
    # a single long word is broken between letters
    word = "AHAHSHHSHSHJSJJDJDDHDHDJFJJFJFJFJJFJFJFJKCJJCCJJFJCJJCJKCVJVJJVJJFKSKKSKSKXKXKKX"
    lines, fs = render.name_lines(word, font, size, max_w)
    assert len(lines) == 2 and "".join(lines) == word
    # short names stay on one line at full size
    assert render.name_lines("Ali", font, size, max_w) == (["Ali"], size)


def logged_in_with_session(app, name="Bulk Workshop"):
    """An admin client and the id of a new session using the sample template."""
    admin = app.test_client()
    t = csrf(admin, "/admin/login")
    admin.post("/admin/login", data={"csrf_token": t, "password": "pw"})
    t = csrf(admin, "/admin/sessions/new")
    png = (ROOT / "samples" / "sample-template.png").read_bytes()
    r = admin.post("/admin/sessions/new", data={
        "csrf_token": t, "name": name, "event_date": "1 Nov 2026", "slug": "", "status": "open",
        "email_subject": "", "email_body": "", "template": (io.BytesIO(png), "cert.png")},
        content_type="multipart/form-data")
    return admin, int(re.search(r"/sessions/(\d+)/", r.headers["Location"]).group(1))


def test_bulk_import_and_downloads():
    import zipfile
    from openpyxl import Workbook
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        app = make_app(tmp)
        admin, ev_id = logged_in_with_session(app)

        wb = Workbook()
        ws = wb.active
        ws.append(["No.", "Full Name", "Email"])
        ws.append([1, "Nur Aisyah binti Rahman", "aisyah@example.com"])
        ws.append([2, "Tan Wei Ming", None])
        ws.append([3, "Anand a/l Murugan", "not-an-email"])
        ws.append([None, None, None])
        ws.append([4, "Siti Nurhaliza", "SITI@example.com"])
        ws.append([5, "Tan Wei Ming", None])  # same name, no email: a duplicate
        ws.append([6, "123", None])
        xlsx = io.BytesIO()
        wb.save(xlsx)

        url = f"/admin/sessions/{ev_id}/import"
        t = csrf(admin, url)
        r = admin.post(url, data={"csrf_token": t, "send_email": "1", "file": (io.BytesIO(xlsx.getvalue()), "names.xlsx")},
                       content_type="multipart/form-data")
        page = r.get_data(as_text=True)
        assert "4 certificates being made" in page, page[:2000]
        assert "Row 4" in page and "Row 7" in page and "Row 8" in page

        conn = connect(Path(tmp) / "certqr.db")
        rows = {r["full_name"]: r for r in conn.execute("SELECT * FROM participants WHERE event_id=?", (ev_id,))}
        conn.close()
        assert set(rows) == {"Nur Aisyah binti Rahman", "Tan Wei Ming", "Anand a/l Murugan", "Siti Nurhaliza"}
        assert all(r["pdf_file"] is None for r in rows.values())
        assert rows["Tan Wei Ming"]["email"] is None and rows["Tan Wei Ming"]["email_status"] == "none"
        assert rows["Anand a/l Murugan"]["email"] is None
        assert rows["Siti Nurhaliza"]["email"] == "siti@example.com"
        assert rows["Siti Nurhaliza"]["email_status"] == "queued"

        # the worker makes all 4 PDFs, then sends only the 2 with an email
        assert drain(app) == 6
        assert len(list((Path(tmp) / "outbox").glob("*.eml"))) == 2

        # uploading the same file again adds nobody
        t = csrf(admin, url)
        r = admin.post(url, data={"csrf_token": t, "file": (io.BytesIO(xlsx.getvalue()), "names.xlsx")},
                       content_type="multipart/form-data")
        assert "0 certificates being made" in r.get_data(as_text=True)

        # CSV with semicolons and no header row
        t = csrf(admin, url)
        csv_bytes = "Lim Mei Ling;mei@example.com\nAhmad Faiz;\n".encode("utf-8")
        r = admin.post(url, data={"csrf_token": t, "file": (io.BytesIO(csv_bytes), "list.csv")},
                       content_type="multipart/form-data")
        assert "2 certificates being made" in r.get_data(as_text=True)
        t = csrf(admin, url)
        r = admin.post(url, data={"csrf_token": t, "file": (io.BytesIO(b"x"), "old.xls")},
                       content_type="multipart/form-data", follow_redirects=True)
        assert ".xlsx" in r.get_data(as_text=True)

        # download all as a ZIP; the 2 CSV names have no PDF yet and are made on the spot
        z = admin.get(f"/admin/sessions/{ev_id}/certificates.zip")
        assert z.status_code == 200 and z.headers["Content-Type"] == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(z.data))
        assert len(zf.namelist()) == 6 and "Certificate_Tan_Wei_Ming.pdf" in zf.namelist()
        assert all(zf.read(n)[:4] == b"%PDF" for n in zf.namelist())

        # tick two and download just those; download one on its own
        people = admin.get(f"/admin/sessions/{ev_id}/participants").get_data(as_text=True)
        ids = re.findall(r'name="ids" value="(\d+)"', people)
        assert len(ids) == 6 and "Not emailed" in people
        t = csrf(admin, f"/admin/sessions/{ev_id}/participants")
        z = admin.post(f"/admin/sessions/{ev_id}/certificates.zip", data={"csrf_token": t, "ids": ids[:2]})
        assert len(zipfile.ZipFile(io.BytesIO(z.data)).namelist()) == 2
        one = admin.get(f"/admin/participants/{ids[0]}/pdf?dl=1")
        assert one.data[:4] == b"%PDF" and "attachment" in one.headers["Content-Disposition"]

        # someone without an email can be given one, which sends the certificate
        conn = connect(Path(tmp) / "certqr.db")
        tan = conn.execute("SELECT id FROM participants WHERE full_name='Tan Wei Ming'").fetchone()["id"]
        conn.close()
        admin.post(f"/admin/participants/{tan}/fix", data={"csrf_token": t, "full_name": "Tan Wei Ming",
                                                           "email": "wm@example.com"})
        assert drain(app) == 1
        print("bulk import checks passed")


def test_thank_you_page():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        app = make_app(tmp)
        admin, ev_id = logged_in_with_session(app, "Leadership Talk")
        edit = f"/admin/sessions/{ev_id}/edit"
        t = csrf(admin, edit)
        form = {"csrf_token": t, "name": "Leadership Talk", "event_date": "1 Nov 2026", "slug": "leadership-talk",
                "status": "open", "email_subject": "", "email_body": "",
                "done_heading": "Terima kasih!", "done_title": "Well done, {first_name}",
                "done_message": "See you at {session}.\nBring a friend.", "done_show_view": "1",
                "done_button_label": "Give feedback", "done_button_url": "not a link"}
        assert "full web address" in admin.post(edit, data=form).get_data(as_text=True)
        form["done_button_url"] = "https://forms.gle/abc"
        assert admin.post(edit, data=form).status_code == 302

        preview = admin.get(f"/admin/sessions/{ev_id}/done-preview").get_data(as_text=True)
        assert "Terima kasih!" in preview and "Well done, Nur" in preview

        user = app.test_client()
        t = csrf(user, "/r/leadership-talk")
        user.post("/r/leadership-talk", data={"csrf_token": t, "full_name": "Ali Hassan", "email": "ali@example.com"})
        t = csrf(user, "/r/leadership-talk/confirm")
        done_url = user.post("/r/leadership-talk/confirm", data={"csrf_token": t}).headers["Location"]
        page = user.get(done_url).get_data(as_text=True)
        assert "Terima kasih!" in page and "Well done, Ali" in page and "See you at Leadership Talk." in page
        assert 'href="https://forms.gle/abc"' in page and "Give feedback" in page
        assert "Download PDF" not in page and "View certificate" in page

        # empty boxes fall back to the defaults
        form.update(done_heading="", done_title="", done_message="", done_button_label="", done_button_url="",
                    done_show_download="1", csrf_token=csrf(admin, edit))
        admin.post(edit, data=form)
        page = user.get(done_url).get_data(as_text=True)
        assert "You&#39;re all set" in page and "Certificate ready, Ali!" in page and "Download PDF" in page
        print("thank-you page checks passed")


OLD_SCHEMA = """
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, slug TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
  event_date TEXT, status TEXT NOT NULL DEFAULT 'open', access_code TEXT, template_file TEXT,
  template_kind TEXT, page_w REAL, page_h REAL, preview_file TEXT, name_x REAL NOT NULL DEFAULT 0.5,
  name_y REAL NOT NULL DEFAULT 0.5, name_w REAL NOT NULL DEFAULT 0.6, name_size REAL NOT NULL DEFAULT 0.07,
  name_font TEXT NOT NULL DEFAULT 'poppins-bold', name_color TEXT NOT NULL DEFAULT '#1E2A4A',
  name_align TEXT NOT NULL DEFAULT 'center', name_case TEXT NOT NULL DEFAULT 'as_typed',
  show_cert_id INTEGER NOT NULL DEFAULT 1, email_subject TEXT, email_body TEXT, created_at TEXT NOT NULL);
CREATE TABLE participants (id INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE, full_name TEXT NOT NULL,
  email TEXT NOT NULL, cert_code TEXT NOT NULL UNIQUE, token TEXT NOT NULL, pdf_file TEXT, ip TEXT,
  created_at TEXT NOT NULL, email_status TEXT NOT NULL DEFAULT 'queued',
  email_attempts INTEGER NOT NULL DEFAULT 0, email_error TEXT, email_sent_at TEXT, next_attempt_at TEXT,
  UNIQUE (event_id, email));
CREATE INDEX idx_participants_event ON participants(event_id);
CREATE INDEX idx_participants_mail ON participants(email_status, next_attempt_at);
INSERT INTO events (slug, name, created_at) VALUES ('old', 'Old session', '2026-01-01');
INSERT INTO participants (event_id, full_name, email, cert_code, token, pdf_file, created_at, email_status)
  VALUES (1, 'Old Person', 'old@example.com', 'ABCDEFGH', 'tok', '1-ABCDEFGH.pdf', '2026-01-01', 'sent');
"""


def test_upgrade_old_database():
    """A database made before emails became optional is upgraded on start."""
    import sqlite3
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        conn = sqlite3.connect(Path(tmp) / "certqr.db")
        conn.executescript(OLD_SCHEMA)
        conn.close()
        make_app(tmp)
        conn = connect(Path(tmp) / "certqr.db")
        assert not {r["name"]: r for r in conn.execute("PRAGMA table_info(participants)")}["email"]["notnull"]
        assert tuple(conn.execute("SELECT full_name, email_status FROM participants").fetchone()) == ("Old Person", "sent")
        assert conn.execute("SELECT done_show_download FROM events").fetchone()[0] == 1
        indexes = {r["name"] for r in conn.execute("PRAGMA index_list(participants)")}
        assert {"idx_participants_event", "idx_participants_mail"} <= indexes
        conn.execute("INSERT INTO participants (event_id, full_name, cert_code, token, created_at) "
                     "VALUES (1, 'No Email', 'XYZ', 't', '2026-01-02')")
        conn.commit()
        conn.close()
        make_app(tmp)  # starting again changes nothing
        print("upgrade checks passed")


if __name__ == "__main__":
    test_full_flow()
    test_email_settings()
    test_long_name_shrinks()
    test_bulk_import_and_downloads()
    test_thank_you_page()
    test_upgrade_old_database()
