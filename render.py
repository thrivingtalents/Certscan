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

# key -> (label shown in admin, ttf file, css font-family used in the editor preview, group)
FONTS = {
    "poppins-bold":       ("Poppins Bold", "Poppins-Bold.ttf", "CQ Poppins Bold", "Modern"),
    "poppins-medium":     ("Poppins Medium", "Poppins-Medium.ttf", "CQ Poppins Medium", "Modern"),
    "poppins-regular":    ("Poppins Regular", "Poppins-Regular.ttf", "CQ Poppins Regular", "Modern"),
    "montserrat-bold":    ("Montserrat Bold", "Montserrat-Bold.ttf", "CQ Montserrat Bold", "Modern"),
    "montserrat-medium":  ("Montserrat Medium", "Montserrat-Medium.ttf", "CQ Montserrat Medium", "Modern"),
    "raleway-semibold":   ("Raleway SemiBold", "Raleway-SemiBold.ttf", "CQ Raleway SemiBold", "Modern"),
    "serif-bold":         ("Serif Bold", "DejaVuSerif-Bold.ttf", "CQ Serif Bold", "Elegant serif"),
    "serif-regular":      ("Serif Regular", "DejaVuSerif.ttf", "CQ Serif Regular", "Elegant serif"),
    "serif-italic":       ("Serif Italic", "DejaVuSerif-Italic.ttf", "CQ Serif Italic", "Elegant serif"),
    "playfair-bold":      ("Playfair Display Bold", "PlayfairDisplay-Bold.ttf", "CQ Playfair Bold", "Elegant serif"),
    "playfair-italic":    ("Playfair Display Italic", "PlayfairDisplay-Italic.ttf", "CQ Playfair Italic", "Elegant serif"),
    "cinzel-bold":        ("Cinzel Bold (capitals)", "Cinzel-Bold.ttf", "CQ Cinzel Bold", "Elegant serif"),
    "lora-italic":        ("Lora Italic", "Lora-Italic.ttf", "CQ Lora Italic", "Elegant serif"),
    "great-vibes":        ("Great Vibes", "GreatVibes-Regular.ttf", "CQ Great Vibes", "Script / curvy"),
    "pinyon-script":      ("Pinyon Script", "PinyonScript-Regular.ttf", "CQ Pinyon Script", "Script / curvy"),
    "allura":             ("Allura", "Allura-Regular.ttf", "CQ Allura", "Script / curvy"),
    "dancing-script":     ("Dancing Script Bold", "DancingScript-Bold.ttf", "CQ Dancing Script", "Script / curvy"),
}
DEFAULT_FONT = "poppins-bold"
UPLOADED_PREFIX = "up-"
# folder holding uploaded fonts as <key>.ttf; set by the app at start-up
UPLOAD_FONT_DIR = None

A4_LONG = 841.89  # points
MAX_IMAGE_EDGE = 3508  # px, A4 at 300 dpi

_registered = set()


class FontError(ValueError):
    pass


def font_path(key):
    """The .ttf file for a font key, or None if there's no such font (e.g. an uploaded font was deleted)."""
    if key in FONTS:
        return FONT_DIR / FONTS[key][1]
    if key and key.startswith(UPLOADED_PREFIX) and UPLOAD_FONT_DIR and re.fullmatch(r"up-[0-9a-f]{12}", key):
        path = Path(UPLOAD_FONT_DIR) / f"{key}.ttf"
        if path.exists():
            return path
    return None


def _font(key):
    path = font_path(key)
    if path is None:
        key, path = DEFAULT_FONT, font_path(DEFAULT_FONT)
    name = "CQ-" + key
    if name not in _registered:
        pdfmetrics.registerFont(TTFont(name, str(path)))
        _registered.add(name)
    return name


def check_font(data):
    """Make sure an uploaded file is a font the PDF maker can use. Returns the font's own name."""
    if data[:4] == b"OTTO":
        raise FontError("This .otf font uses PostScript outlines, which can't be embedded in the PDF. "
                        "Use the .ttf version of the font instead.")
    if data[:4] in (b"wOFF", b"wOF2"):
        raise FontError("That's a web font (.woff). Upload the .ttf version instead.")
    if data[:4] not in (b"\x00\x01\x00\x00", b"true"):
        raise FontError("That doesn't look like a font file. Upload a .ttf file.")
    try:
        face = TTFont("check", io.BytesIO(data)).face
    except Exception as exc:  # noqa: BLE001
        raise FontError("This font can't be used in the PDF. Try another copy of it, as a .ttf file.") from exc
    missing = [c for c in "ABCabc" if ord(c) not in face.charToGlyph]
    if missing:
        raise FontError("This font has no ordinary letters (A–Z), so it can't print names.")
    return (face.name or b"").decode("latin-1", "ignore") if isinstance(face.name, bytes) else str(face.name or "")


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


TWO_LINE_SCALE = 0.8   # a name split over two lines is set a little smaller so the block stays compact
LINE_SPACING = 1.15    # baseline-to-baseline distance, as a multiple of the font size


def split_name(text, measure):
    """Split text into two lines whose longer line is as short as possible.

    Breaks at a space when there is one; a single long word is broken between letters.
    measure(s) returns the width of s.
    """
    words = text.split(" ")
    if len(words) > 1:
        cands = [(" ".join(words[:i]), " ".join(words[i:])) for i in range(1, len(words))]
    else:
        cands = [(text[:i], text[i:]) for i in range(1, len(text))]
    return list(min(cands, key=lambda pair: max(measure(pair[0]), measure(pair[1]))))


def name_lines(text, font, size, max_w):
    """Return (lines, font_size): one line if the name fits, otherwise two lines, shrunk if still too wide."""
    width = pdfmetrics.stringWidth(text, font, size)
    if width <= max_w:
        return [text], size
    if len(text) < 2:
        return [text], size * max_w / width
    lines = split_name(text, lambda s: pdfmetrics.stringWidth(s, font, size))
    size2 = size * TWO_LINE_SCALE
    widest = max(pdfmetrics.stringWidth(s, font, size2) for s in lines)
    return lines, size2 * min(1, max_w / widest)


def _draw_text_layer(c, ev, name):
    W, H = ev["page_w"], ev["page_h"]
    font = _font(ev["name_font"])
    text = format_name(name, ev["name_case"])
    max_w = ev["name_w"] * W
    lines, size = name_lines(text, font, max(4.0, ev["name_size"] * H), max_w)
    asc, desc = pdfmetrics.getAscentDescent(font, size)
    cx = ev["name_x"] * W
    cy = H - ev["name_y"] * H
    lead = size * LINE_SPACING
    try:
        c.setFillColor(HexColor(ev["name_color"]))
    except Exception:  # noqa: BLE001
        c.setFillColor(HexColor("#1E2A4A"))
    c.setFont(font, size)
    align = ev["name_align"]
    for i, line in enumerate(lines):
        # the last line sits where a one-line name would; extra lines go above it,
        # since designs usually have free space above the name and a rule below it
        baseline = cy + lead * (len(lines) - 1 - i) - (asc + desc) / 2
        if align == "left":
            c.drawString(cx - max_w / 2, baseline, line)
        elif align == "right":
            c.drawRightString(cx + max_w / 2, baseline, line)
        else:
            c.drawCentredString(cx, baseline, line)


def render_certificate(ev, name, template_dir):
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
    _draw_text_layer(c, ev, name)
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
