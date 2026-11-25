(function () {
  const $ = (id) => document.getElementById(id);
  const body = $("body"), form = $("sessionForm");
  const csrf = form.querySelector('input[name="csrf_token"]').value;

  // put text into the message where the cursor is (or at the end), keeping the cursor after it
  function insertAtCursor(text) {
    const start = body.selectionStart ?? body.value.length, end = body.selectionEnd ?? start;
    const before = body.value.slice(0, start), after = body.value.slice(end);
    body.value = before + text + after;
    body.focus();
    body.selectionStart = body.selectionEnd = start + text.length;
    showPictures();
  }

  // ---- links
  const panel = $("linkPanel"), lnText = $("lnText"), lnUrl = $("lnUrl"), lnErr = $("lnErr");
  let saved = [0, 0];
  $("addLink").addEventListener("click", () => {
    saved = [body.selectionStart, body.selectionEnd];
    lnText.value = body.value.slice(saved[0], saved[1]).trim();  // selected words become the link text
    lnUrl.value = ""; lnErr.hidden = true; panel.hidden = false;
    (lnText.value ? lnUrl : lnText).focus();
  });
  $("lnCancel").addEventListener("click", () => { panel.hidden = true; body.focus(); });
  function insertLink() {
    let url = lnUrl.value.trim();
    if (url && !/^https?:\/\//i.test(url) && /^[\w-]+(\.[\w-]+)+/.test(url)) url = "https://" + url;
    if (!/^https?:\/\/\S+$/i.test(url)) { lnErr.hidden = false; lnUrl.focus(); return; }
    const text = lnText.value.replace(/[\[\]\n]/g, " ").trim();
    body.selectionStart = saved[0]; body.selectionEnd = saved[1];
    insertAtCursor(text ? `[${text}](${url})` : url);
    panel.hidden = true;
  }
  $("lnInsert").addEventListener("click", insertLink);
  [lnText, lnUrl].forEach(el => el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); insertLink(); }   // don't submit the whole form
    if (e.key === "Escape") { panel.hidden = true; body.focus(); }
  }));

  // ---- pictures
  const picFile = $("picFile"), picStatus = $("picStatus"), pics = $("mailPics");
  $("addPic").addEventListener("click", () => { saved = [body.selectionStart, body.selectionEnd]; picFile.click(); });
  picFile.addEventListener("change", async () => {
    const file = picFile.files[0];
    if (!file) return;
    picStatus.innerHTML = '<span class="spin"></span> Uploading…';
    const data = new FormData();
    data.append("csrf_token", csrf);
    data.append("picture", file);
    try {
      const r = await fetch(window.PICTURE_UPLOAD, { method: "POST", body: data });
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || "Upload failed.");
      body.selectionStart = saved[0]; body.selectionEnd = saved[1];
      const before = body.value.slice(0, saved[0]);
      // a picture sits on its own line
      insertAtCursor((before && !before.endsWith("\n") ? "\n" : "") + `{picture:${d.key}}\n`);
      picStatus.textContent = "Picture added.";
    } catch (err) {
      picStatus.textContent = err.message || "Upload failed.";
    }
    picFile.value = "";
  });

  // thumbnails of the pictures in the message, each with a button to take it out
  function showPictures() {
    const keys = [...new Set([...body.value.matchAll(/\{picture:([0-9a-f]{12})\}/g)].map(m => m[1]))];
    pics.replaceChildren(...keys.map(key => {
      const fig = document.createElement("figure");
      const img = document.createElement("img");
      img.src = window.PICTURE_URL.replace("KEY", key); img.alt = "";
      const del = document.createElement("button");
      del.type = "button"; del.className = "btn sm o"; del.textContent = "Remove";
      del.addEventListener("click", () => {
        body.value = body.value.replace(new RegExp(`\\{picture:${key}\\}\\n?`, "g"), "");
        showPictures();
      });
      fig.append(img, del);
      return fig;
    }));
  }
  body.addEventListener("input", showPictures);
  showPictures();

  // ---- email preview, from the text as it is now (not yet saved), shown in a pop-up on this page
  const dialog = $("mailDialog"), frame = $("mailFrame"), prevBtn = $("mailPreviewBtn");
  prevBtn.addEventListener("click", async () => {
    const data = new FormData(form);
    data.delete("template");  // the certificate design file isn't needed for the preview
    prevBtn.disabled = true;
    try {
      const r = await fetch(prevBtn.dataset.url, { method: "POST", body: data });
      frame.srcdoc = await r.text();
      dialog.showModal();
    } catch (_) {
      alert("Couldn't load the preview. Check your connection and try again.");
    }
    prevBtn.disabled = false;
  });
  $("mailDialogClose").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });  // click outside closes it

  // ---- thank-you page samples
  document.querySelectorAll("[data-sample]").forEach(btn => btn.addEventListener("click", () => {
    const s = window.DONE_SAMPLES[+btn.dataset.sample];
    const fields = { dh: s.heading, dt: s.title, dm: s.message };
    const filled = Object.keys(fields).some(id => $(id).value.trim());
    if (filled && !confirm("Replace the heading, title and message with this sample?")) return;
    Object.entries(fields).forEach(([id, v]) => { $(id).value = v; });
    $("dm").focus();
  }));
})();
