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
    while w.process_one(conn):
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
            "show_cert_id": "1", "then": "qr"})
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

        # PDF contains the formatted name + ID
        pdf = user.get(f"/c/{code}/{token}")
        assert pdf.status_code == 200 and pdf.data[:4] == b"%PDF"
        text = PdfReader(io.BytesIO(pdf.data)).pages[0].extract_text()
        assert "Nur Aisyah binti Rahman" in " ".join(text.split()), text
        assert code in text
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
        assert f"https://cert.test/c/{code}/{token}" in msg.get_body(("plain",)).get_content()
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

        # verify page
        assert "Valid certificate" in user.get(f"/v/{code}").get_data(as_text=True)
        assert "No certificate found" in user.get("/v/ZZZZZZZZ").get_data(as_text=True)

        # admin: list, CSV, fix typo
        people = admin.get(f"/admin/sessions/{ev_id}/participants").get_data(as_text=True)
        assert "Nur Aisyah" in people and code in people
        csv_txt = admin.get(f"/admin/sessions/{ev_id}/participants.csv").get_data(as_text=True)
        assert "aisyah@example.com" in csv_txt and code in csv_txt
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
    render._draw_text_layer(c, ev, "Muhammad Aiman Hakimi bin Abdul Rahman Shah", None, None)
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


if __name__ == "__main__":
    test_full_flow()
    test_email_settings()
    test_long_name_shrinks()
