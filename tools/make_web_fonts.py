"""Make the small .woff2 copies of the certificate fonts that web pages use.

The .ttf files in static/fonts are what the PDFs embed. Pages only need the Latin letters, so each font is
cut down to those and saved as .woff2 in static/fonts/web, which is about a tenth of the size. A name with
other characters still shows correctly: app.css falls back to the full .ttf for just those characters.

Run after adding or changing a font in static/fonts (needs: pip install fonttools brotli):
    python tools/make_web_fonts.py
"""
from pathlib import Path

from fontTools import subset

FONTS = Path(__file__).resolve().parents[1] / "static" / "fonts"
OUT = FONTS / "web"

# keep in step with UNICODE_RANGE in static/app.css
UNICODES = ("U+0000-024F,U+0259,U+02B0-02FF,U+0300-036F,U+1E00-1EFF,U+2000-206F,"
            "U+20AC,U+2122,U+2190-2193,U+2212,U+2715")


def main():
    OUT.mkdir(exist_ok=True)
    for ttf in sorted(FONTS.glob("*.ttf")):
        out = OUT / f"{ttf.stem}.woff2"
        subset.main([str(ttf), f"--unicodes={UNICODES}", "--layout-features=*", "--flavor=woff2",
                     "--no-hinting", "--desubroutinize", f"--output-file={out}"])
        print(f"{out.name:34} {ttf.stat().st_size // 1024:5} KB -> {out.stat().st_size // 1024:4} KB")


if __name__ == "__main__":
    main()
