"""Certificate rendering: template processing, name stamping, previews and QR codes."""
import io
import re
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter
from reportlab.graphics.barcode.qr import QrCodeWidget
from reportlab.lib.colors import HexColor
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"

# key -> (label shown in admin, ttf file, css font-family used in the editor preview)
FONTS = {
    "poppins-bold":    ("Poppins Bold", "Poppins-Bold.ttf", "CQ Poppins Bold"),
    "poppins-medium":  ("Poppins Medium", "Poppins-Medium.ttf", "CQ Poppins Medium"),
    "poppins-regular": ("Poppins Regular", "Poppins-Regular.ttf", "CQ Poppins Regular"),
    "serif-bold":      ("Serif Bold", "DejaVuSerif-Bold.ttf", "CQ Serif Bold"),
    "serif-regular":   ("Serif Regular", "DejaVuSerif.ttf", "CQ Serif Regular"),
    "serif-italic":    ("Serif Italic", "DejaVuSerif-Italic.ttf", "CQ Serif Italic"),
}
DEFAULT_FONT = "poppins-bold"

A4_LONG = 841.89  # points
MAX_IMAGE_EDGE = 3508  # px, A4 at 300 dpi

_registered = set()


def _font(key):
    key = key if key in FONTS else DEFAULT_FONT
    name = "CQ-" + key
    if name not in _registered:
        pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / FONTS[key][1])))
        _registered.add(name)
    return name


class TemplateError(ValueError):
    pass


# ---------------------------------------------------------------- templates

def process_template(upload_bytes, filename, dest_dir, stem):
    """Validate an uploaded design and store a normalised copy plus a PNG preview.

    Returns dict(template_file, template_kind, page_w, page_h, preview_file).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext == "pdf":
        try:
            reader = PdfReader(io.BytesIO(upload_bytes))
            if reader.is_encrypted:
                raise TemplateError("This PDF is password-protected. Upload an unlocked copy.")
            page = reader.pages[0]
            page.transfer_rotation_to_content()
            writer = PdfWriter()
            writer.add_page(page)
            buf = io.BytesIO()
            writer.write(buf)
        except TemplateError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise TemplateError(f"Could not read that PDF ({exc.__class__.__name__}).") from exc
        pdf_bytes = buf.getvalue()
        tpl = dest_dir / f"{stem}.pdf"
        tpl.write_bytes(pdf_bytes)
        box = PdfReader(io.BytesIO(pdf_bytes)).pages[0].mediabox
        page_w, page_h = float(box.width), float(box.height)
        preview = pdf_to_png(pdf_bytes, width=1600)
        kind = "pdf"
    elif ext in ("png", "jpg", "jpeg", "webp"):
        try:
            img = Image.open(io.BytesIO(upload_bytes))
            img.load()
        except Exception as exc:  # noqa: BLE001
            raise TemplateError("That file doesn't look like a valid image.") from exc
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        if max(img.size) > MAX_IMAGE_EDGE:
            img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
        if min(img.size) < 400:
            raise TemplateError("Image is too small. Use at least 1600 px wide for a sharp certificate.")
        tpl = dest_dir / f"{stem}.jpg"
        img.save(tpl, "JPEG", quality=92, optimize=True)
        w, h = img.size
        scale = A4_LONG / max(w, h)
        page_w, page_h = w * scale, h * scale
        prev = img.copy()
        prev.thumbnail((1600, 1600), Image.LANCZOS)
        out = io.BytesIO()
        prev.save(out, "PNG", optimize=True)
        preview = out.getvalue()
        kind = "image"
    else:
        raise TemplateError("Upload a PNG, JPG or PDF file.")

    prev_path = dest_dir / f"{stem}-preview.png"
    prev_path.write_bytes(preview)
    return {
        "template_file": tpl.name,
        "template_kind": kind,
        "page_w": round(page_w, 2),
        "page_h": round(page_h, 2),
        "preview_file": prev_path.name,
    }


# ---------------------------------------------------------------- names

def format_name(name, case="as_typed"):
    name = re.sub(r"\s+", " ", (name or "")).strip()
    if case == "upper":
        return name.upper()
    if case == "title":
        # keep particles like "bin", "binti", "a/l", "van" lowercase when they're not first
        small = {"bin", "binti", "bt", "bte", "a/l", "a/p", "van", "von", "de", "da", "al"}
        parts = []
        for i, w in enumerate(name.split(" ")):
            lw = w.lower()
            parts.append(lw if (i and lw in small) else "-".join(p[:1].upper() + p[1:].lower() for p in w.split("-")))
        return " ".join(parts)
    return name


def _draw_text_layer(c, ev, name, cert_code, verify_url):
    W, H = ev["page_w"], ev["page_h"]
    font = _font(ev["name_font"])
    text = format_name(name, ev["name_case"])
    size = max(4.0, ev["name_size"] * H)
    max_w = ev["name_w"] * W
    width = pdfmetrics.stringWidth(text, font, size)
    if width > max_w:  # auto-shrink long names to fit the box
        size = size * max_w / width
    asc, desc = pdfmetrics.getAscentDescent(font, size)
    cx = ev["name_x"] * W
    cy = H - ev["name_y"] * H
    baseline = cy - (asc + desc) / 2
    try:
        c.setFillColor(HexColor(ev["name_color"]))
    except Exception:  # noqa: BLE001
        c.setFillColor(HexColor("#1E2A4A"))
    c.setFont(font, size)
    align = ev["name_align"]
    if align == "left":
        c.drawString(cx - max_w / 2, baseline, text)
    elif align == "right":
        c.drawRightString(cx + max_w / 2, baseline, text)
    else:
        c.drawCentredString(cx, baseline, text)

    if ev["show_cert_id"] and cert_code:
        small = _font("poppins-regular")
        fs = max(5.5, H * 0.011)
        c.setFont(small, fs)
        c.setFillColor(HexColor("#6B7194"))
        label = f"Certificate ID {cert_code}"
        if verify_url:
            label += f"  ·  Verify: {verify_url}"
        c.drawRightString(W - W * 0.035, H * 0.022, label)


def render_certificate(ev, name, cert_code, template_dir, verify_url=None):
    """Return PDF bytes for one participant."""
    if not ev["template_file"]:
        raise TemplateError("This session has no certificate template yet.")
    W, H = ev["page_w"], ev["page_h"]
    tpl_path = Path(template_dir) / ev["template_file"]
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(W, H))
    c.setTitle(f"Certificate - {format_name(name, ev['name_case'])}")
    c.setAuthor(ev["name"])
    if ev["template_kind"] == "image":
        c.drawImage(ImageReader(str(tpl_path)), 0, 0, width=W, height=H)
    _draw_text_layer(c, ev, name, cert_code, verify_url)
    c.showPage()
    c.save()

    if ev["template_kind"] != "pdf":
        return buf.getvalue()

    base = PdfReader(str(tpl_path)).pages[0]
    overlay = PdfReader(io.BytesIO(buf.getvalue())).pages[0]
    base.merge_page(overlay)
    writer = PdfWriter()
    writer.add_page(base)
    writer.add_metadata({"/Title": f"Certificate - {format_name(name, ev['name_case'])}"})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def pdf_to_png(pdf_bytes, width=1200):
    pdf = pdfium.PdfDocument(pdf_bytes)
    try:
        page = pdf[0]
        scale = width / page.get_width()
        img = page.render(scale=scale).to_pil().convert("RGB")
    finally:
        pdf.close()
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- QR codes

def qr_matrix(data):
    w = QrCodeWidget(data, barLevel="M")
    w.qr.make()
    return [[bool(v) for v in row] for row in w.qr.modules]


def qr_png(data, box=16, border=4, fg=(17, 22, 46), bg=(255, 255, 255)):
    m = qr_matrix(data)
    n = len(m)
    size = (n + border * 2) * box
    img = Image.new("RGB", (size, size), bg)
    draw = ImageDraw.Draw(img)
    for r, row in enumerate(m):
        for col, dark in enumerate(row):
            if dark:
                x0, y0 = (col + border) * box, (r + border) * box
                draw.rectangle([x0, y0, x0 + box - 1, y0 + box - 1], fill=fg)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def qr_svg(data, border=4, fg="#11162E"):
    m = qr_matrix(data)
    n = len(m) + border * 2
    d = "".join(f"M{c + border} {r + border}h1v1h-1z" for r, row in enumerate(m) for c, v in enumerate(row) if v)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" shape-rendering="crispEdges">'
            f'<rect width="{n}" height="{n}" fill="#fff"/><path d="{d}" fill="{fg}"/></svg>')
