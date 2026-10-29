(function () {
  const $ = (id) => document.getElementById(id);
  const img = $("tpl"), box = $("box"), txt = $("txt"), guide = $("guide"), canvas = $("canvas");
  const fx = $("name_x"), fy = $("name_y"), fw = $("name_w");
  const size = $("size"), font = $("font"), color = $("color"), sample = $("sample");
  const st = { x: +fx.value, y: +fy.value, w: +fw.value };
  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const radio = (n) => (document.querySelector(`input[name="${n}"]:checked`) || {}).value;

  const small = new Set(["bin", "binti", "bt", "bte", "a/l", "a/p", "van", "von", "de", "da", "al"]);
  function applyCase(s, c) {
    s = s.replace(/\s+/g, " ").trim();
    if (c === "upper") return s.toUpperCase();
    if (c === "title") return s.split(" ").map((w, i) => (i && small.has(w.toLowerCase())) ? w.toLowerCase()
      : w.split("-").map(p => p.charAt(0).toUpperCase() + p.slice(1).toLowerCase()).join("-")).join(" ");
    return s;
  }

  // mirror the server: one line if it fits, otherwise two balanced lines (smaller), shrunk if still too wide
  const TWO_LINE_SCALE = 0.8, LINE_SPACING = 1.15;
  const ctx = document.createElement("canvas").getContext("2d");
  function nameLines(text, family, fs, maxW) {
    const measure = (s, px) => { ctx.font = `${px}px ${family}`; return ctx.measureText(s).width; };
    const width = measure(text, fs);
    if (width <= maxW) return [[text], fs];
    if (text.length < 2) return [[text], fs * maxW / width];
    const words = text.split(" "), cands = [];
    if (words.length > 1) for (let i = 1; i < words.length; i++) cands.push([words.slice(0, i).join(" "), words.slice(i).join(" ")]);
    else for (let i = 1; i < text.length; i++) cands.push([text.slice(0, i), text.slice(i)]);
    const worst = (p) => Math.max(measure(p[0], fs), measure(p[1], fs));
    const lines = cands.reduce((a, b) => (worst(b) < worst(a) ? b : a));
    const fs2 = fs * TWO_LINE_SCALE, widest = Math.max(...lines.map(s => measure(s, fs2)));
    return [lines, fs2 * Math.min(1, maxW / widest)];
  }

  function layout() {
    const W = img.clientWidth, H = img.clientHeight;
    if (!W || !H) return;
    const bw = st.w * W;
    const opt = font.options[font.selectedIndex];
    const family = `"${opt.dataset.css}", sans-serif`;
    const text = applyCase(sample.value || "Nur Aisyah binti Rahman", radio("name_case"));
    const [lines, fs] = nameLines(text, family, +size.value * H, bw);
    // like the server, a second line goes above the first, so the box grows upwards
    const bh1 = Math.max(fs * 1.35, 18), extra = (lines.length - 1) * fs * LINE_SPACING;
    box.style.left = (st.x * W - bw / 2) + "px";
    box.style.top = (st.y * H - bh1 / 2 - extra) + "px";
    box.style.width = bw + "px";
    box.style.height = (bh1 + extra) + "px";
    txt.style.fontFamily = family;
    txt.style.color = color.value;
    txt.style.textAlign = radio("name_align") || "center";
    txt.style.whiteSpace = "pre";
    txt.style.lineHeight = String(LINE_SPACING);
    txt.style.fontSize = fs + "px";
    txt.textContent = lines.join("\n");
    fx.value = st.x.toFixed(4); fy.value = st.y.toFixed(4); fw.value = st.w.toFixed(4);
  }

  let drag = null;
  box.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    box.setPointerCapture(e.pointerId);
    drag = { mode: e.target.dataset.h || "move", sx: e.clientX, sy: e.clientY, x: st.x, y: st.y, w: st.w };
  });
  box.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const W = img.clientWidth, H = img.clientHeight;
    const dx = (e.clientX - drag.sx) / W, dy = (e.clientY - drag.sy) / H;
    if (drag.mode === "move") {
      st.x = clamp(drag.x + dx, 0.02, 0.98);
      st.y = clamp(drag.y + dy, 0.02, 0.98);
      const snap = Math.abs(st.x - 0.5) < 0.012;
      if (snap) st.x = 0.5;
      guide.style.display = snap ? "block" : "none";
    } else {
      const d = drag.mode === "r" ? dx : -dx;
      st.w = clamp(drag.w + d * 2, 0.08, 1);
    }
    layout();
  });
  const end = () => { drag = null; guide.style.display = "none"; };
  box.addEventListener("pointerup", end);
  box.addEventListener("pointercancel", end);

  box.addEventListener("keydown", (e) => {
    const step = e.shiftKey ? 0.02 : 0.003;
    const k = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[e.key];
    if (!k) return;
    e.preventDefault();
    st.x = clamp(st.x + k[0], 0.02, 0.98); st.y = clamp(st.y + k[1], 0.02, 0.98);
    layout();
  });

  document.querySelectorAll("#designForm input, #designForm select, #sample").forEach(el => {
    el.addEventListener("input", layout); el.addEventListener("change", layout);
  });
  window.addEventListener("resize", layout);
  if (img.complete) layout(); else img.addEventListener("load", layout);
  if (document.fonts) {
    Promise.all([...font.options].map(o => document.fonts.load(`40px "${o.dataset.css}"`).catch(() => null)))
      .then(layout);
    document.fonts.addEventListener && document.fonts.addEventListener("loadingdone", layout);
  }

  $("pdfBtn").addEventListener("click", () => {
    const q = new URLSearchParams({
      name_x: fx.value, name_y: fy.value, name_w: fw.value, name_size: size.value,
      name_font: font.value, name_color: color.value, name_align: radio("name_align"),
      name_case: radio("name_case"), sample: sample.value
    });
    window.open(window.PDF_URL + "?" + q.toString(), "_blank");
  });
})();
