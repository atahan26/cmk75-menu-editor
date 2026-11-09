"use strict";
// CMK75 menu editor. Talks only to the local menu app (same origin); every API call carries X-FLKB: 1.

let menu = null, meta = null, apps = null, browsers = [];
let selected = null, dirty = false, previewScreen = "home";
const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) e.append(k instanceof Node ? k : document.createTextNode(k));
  return e;
};

async function api(path, body) {
  const opts = { headers: { "X-FLKB": "1" } };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).error || msg; } catch (_) {}
    throw new Error(msg);
  }
  return (r.headers.get("Content-Type") || "").includes("json") ? r.json() : r.blob();
}

function setStatus(text, cls = "") { const s = $("status"); s.textContent = text; s.className = "status " + cls; }
function markDirty() { dirty = true; setStatus("Unsaved changes", "dirty"); renderList(); schedulePreview(); }
const uid = () => Math.random().toString(16).slice(2, 12);

// ---- tree helpers
function walk(items, fn, parent = null) {
  for (const it of items) { fn(it, items, parent); if (it.kind === "group") walk(it.children, fn, it); }
}
function findWithList(id) {
  let hit = null;
  walk(menu.items, (it, list, parent) => { if (it.id === id) hit = { it, list, parent }; });
  return hit;
}
function groups() { const g = []; walk(menu.items, (it) => { if (it.kind === "group") g.push(it); }); return g; }

const DEFAULT_OPTIONS = {
  app: [["Launch / switch", "launch"], ["Mute / unmute", "mute"], ["Close", "close"]],
  website: [["Open", "open"]], folder: [["Open", "open"]], file: [["Open", "open"]], command: [["Run", "command"]],
  action: [["Run", "system"]], group: [],
};
function newItem(kind, name, target) {
  return { id: uid(), kind, name: name || kind, icon: "", color: randomColour(), target: target || "", browser: "",
    process: "", options: DEFAULT_OPTIONS[kind].map(([label, action]) => ({ label, action, value: action === "system" ? "media_playpause" : "" })), children: [] };
}
function randomColour() {
  const c = ["#E53935", "#8E24AA", "#3949AB", "#039BE5", "#00897B", "#43A047", "#F4511E", "#6D4C41", "#546E7A"];
  return c[Math.floor(Math.random() * c.length)];
}

// ---- tiles
function tileEl(it, big = false) {
  const t = el("div", { class: "tile" + (big ? " big" : "") });
  if (it.icon) t.append(el("img", { src: "/icons/" + encodeURIComponent(it.icon) + "?v=" + (it._v || 0), alt: "" }));
  else { t.style.background = it.color || "#2ea8ff"; t.textContent = (it.name || "?").slice(0, 1).toUpperCase(); }
  return t;
}

// ---- list (left)
let dragId = null;
function renderList() {
  const root = $("items");
  root.replaceChildren(...menu.items.map(itemNode));
}
function itemNode(it) {
  const row = el("div", { class: "item" + (selected === it.id ? " selected" : ""), draggable: "true",
    onclick: () => { selected = it.id; renderList(); renderEditor(); if (previewScreen !== "home") schedulePreview(); },
    ondragstart: (e) => { dragId = it.id; e.dataTransfer.effectAllowed = "move"; },
    ondragover: (e) => { if (dragId && dragId !== it.id) { e.preventDefault(); row.classList.add("drop-before"); } },
    ondragleave: () => row.classList.remove("drop-before"),
    ondrop: (e) => { e.preventDefault(); row.classList.remove("drop-before"); moveBefore(dragId, it.id); },
  }, tileEl(it), el("span", { class: "name" }, it.name), el("span", { class: "kind" }, it.kind));
  const li = el("li", {}, row);
  if (it.kind === "group") li.append(el("ol", {}, ...it.children.map(itemNode)));
  return li;
}
function moveBefore(id, beforeId) {
  const a = findWithList(id), b = findWithList(beforeId);
  if (!a || !b || a.it === b.it) return;
  if (a.it.kind === "group") { let inside = false; walk(a.it.children, (x) => { if (x.id === beforeId) inside = true; }); if (inside) return; }
  a.list.splice(a.list.indexOf(a.it), 1);
  b.list.splice(b.list.indexOf(b.it), 0, a.it);
  markDirty();
}

// ---- editor (middle)
const VALUE_HINT = { keys: "e.g. ctrl+shift+m", command: "command line", open_in: "browser", launch: "arguments (optional)",
  open: "different path / URL (optional)" };
function targetLabel(kind) {
  return { app: "Program or shortcut", website: "Web address", folder: "Folder", file: "File", command: "Command line" }[kind];
}
function renderEditor() {
  const box = $("editor");
  const hit = selected && findWithList(selected);
  if (!hit) { box.replaceChildren(el("p", { class: "empty" }, "Select an entry on the left, or add one.")); return; }
  const it = hit.it;
  const field = (label, key, attrs = {}) => el("label", {}, label,
    el("input", { value: it[key] || "", ...attrs, oninput: (e) => { it[key] = e.target.value; markDirty(); } }));

  const iconRow = el("div", { class: "icon-row" }, tileEl(it, true), el("div", {},
    el("div", { class: "icon-buttons" },
      it.kind !== "group" && it.kind !== "command" ? el("button", { class: "small", onclick: () => fetchIcon(it) }, "Get icon automatically") : "",
      el("button", { class: "small", onclick: () => chooseIcon(it) }, "Choose image..."),
      it.icon ? el("button", { class: "small", onclick: () => { it.icon = ""; markDirty(); renderEditor(); } }, "Use letter tile") : ""),
    el("label", {}, "Tile colour (without icon)", el("input", { type: "color", value: it.color || "#2ea8ff",
      oninput: (e) => { it.color = e.target.value; markDirty(); } }))));

  const parts = [el("h2", {}, it.name || "(no name)"), iconRow, field("Name on the screen", "name")];
  if (it.kind !== "group" && it.kind !== "action") parts.push(field(targetLabel(it.kind), "target"));
  if (it.kind === "website") {
    parts.push(el("label", {}, "Open with", browserSelect(it.browser, (v) => { it.browser = v; markDirty(); })));
  }
  if (it.kind === "app") parts.push(field("Process name (for switching, closing, audio)", "process",
    { placeholder: "found from the program, e.g. chrome.exe" }));

  // location (top level or a group)
  const loc = el("select", { onchange: (e) => moveTo(it, e.target.value) },
    el("option", { value: "" }, "Top level"),
    ...groups().filter((g) => g !== it).map((g) => el("option", { value: g.id }, "In group: " + g.name)));
  loc.value = hit.parent ? hit.parent.id : "";
  parts.push(el("label", {}, "Location", loc));

  if (it.kind !== "group") {
    parts.push(el("h3", {}, "Options on the keyboard"));
    it.options.forEach((o, i) => parts.push(optionRow(it, o, i)));
    parts.push(el("button", { class: "small", onclick: () => { it.options.push({ label: "New option", action: "keys", value: "" }); markDirty(); renderEditor(); } }, "+ Option"));
    parts.push(el("p", { class: "hint" }, it.options.length === 1 ? "With a single option the entry runs it straight away." : "Pressing the entry shows this list."));
  } else {
    parts.push(el("p", { class: "hint" }, "Groups open a second row of entries. Move entries into it with their Location setting."));
  }
  parts.push(el("div", { class: "edit-actions" },
    el("button", { class: "small", onclick: () => duplicate(hit) }, "Duplicate"),
    el("button", { class: "small danger", onclick: () => remove(hit) }, "Delete")));
  box.replaceChildren(...parts);
}
function browserSelect(value, onchange) {
  const s = el("select", { onchange: (e) => onchange(e.target.value) }, el("option", { value: "" }, "Default browser"),
    ...browsers.map((b) => el("option", { value: b.path }, b.name)));
  s.value = value || "";
  return s;
}
function systemSelect(value, onchange) {
  const s = el("select", { onchange: (e) => onchange(e.target.value) },
    ...Object.entries(meta.system || {}).map(([k, v]) => el("option", { value: k }, v)));
  s.value = value || "media_playpause";
  return s;
}
function optionRow(it, o, i) {
  const action = el("select", { onchange: (e) => { o.action = e.target.value; markDirty(); renderEditor(); } },
    ...Object.entries(meta.actions).map(([k, v]) => el("option", { value: k }, v)));
  action.value = o.action;
  let value;
  if (o.action === "open_in") value = browserSelect(o.value, (v) => { o.value = v; markDirty(); });
  else if (o.action === "system") value = systemSelect(o.value, (v) => { o.value = v; markDirty(); });
  else if (["mute", "volume", "close"].includes(o.action)) value = el("span", { class: "hint" }, "");
  else value = el("input", { value: o.value || "", placeholder: VALUE_HINT[o.action] || "", oninput: (e) => { o.value = e.target.value; markDirty(); } });
  return el("div", { class: "opt" },
    el("input", { value: o.label, oninput: (e) => { o.label = e.target.value; markDirty(); } }), action, value,
    el("div", { class: "tools" },
      el("button", { title: "Up", onclick: () => { if (i) { it.options.splice(i - 1, 0, it.options.splice(i, 1)[0]); markDirty(); renderEditor(); } } }, "\u2191"),
      el("button", { title: "Remove", onclick: () => { it.options.splice(i, 1); markDirty(); renderEditor(); } }, "\u2715")));
}
function moveTo(it, groupId) {
  const hit = findWithList(it.id);
  hit.list.splice(hit.list.indexOf(it), 1);
  (groupId ? findWithList(groupId).it.children : menu.items).push(it);
  markDirty(); renderEditor();
}
function duplicate(hit) {
  const copy = JSON.parse(JSON.stringify(hit.it));
  walk([copy], (x) => { x.id = uid(); });
  copy.name += " copy";
  hit.list.splice(hit.list.indexOf(hit.it) + 1, 0, copy);
  selected = copy.id; markDirty(); renderEditor();
}
function remove(hit) {
  if (!confirm(`Delete "${hit.it.name}"${hit.it.kind === "group" ? " and everything in it" : ""}?`)) return;
  hit.list.splice(hit.list.indexOf(hit.it), 1);
  selected = null; markDirty(); renderEditor();
}

// ---- icons
async function fetchIcon(it) {
  const target = it.kind === "app" ? (it._lnk || it.target) : it.target;
  if (!target) { alert("Fill in the " + targetLabel(it.kind).toLowerCase() + " first."); return; }
  setStatus("Getting icon...");
  try {
    const r = await api("/api/icon/fetch", { id: it.id, kind: it.kind, target });
    it.icon = r.icon; it._v = Date.now(); markDirty(); renderEditor();
  } catch (e) { setStatus("No icon found: " + e.message, "dirty"); }
}
function chooseIcon(it) {
  const input = el("input", { type: "file", accept: "image/png,image/jpeg,image/x-icon,image/webp,image/bmp" });
  input.onchange = () => {
    const f = input.files[0]; if (!f) return;
    const rd = new FileReader();
    rd.onload = async () => {
      try { const r = await api("/api/icon/upload", { id: it.id, data: rd.result }); it.icon = r.icon; it._v = Date.now(); markDirty(); renderEditor(); }
      catch (e) { alert("Could not use that image: " + e.message); }
    };
    rd.readAsDataURL(f);
  };
  input.click();
}

// ---- preview (right)
let previewTimer = null;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(refreshPreview, 200); }
async function refreshPreview() {
  const screen = previewScreen === "item" && selected ? "item:" + selected : "home";
  try {
    const blob = await api("/api/preview", { menu: cleanMenu(), screen });
    const img = $("preview"); const old = img.src;
    img.src = URL.createObjectURL(blob); if (old.startsWith("blob:")) URL.revokeObjectURL(old);
  } catch (e) { setStatus("Preview failed: " + e.message, "dirty"); }
}
function cleanMenu() {
  return JSON.parse(JSON.stringify(menu, (k, v) => (k.startsWith("_") ? undefined : v)));
}

// ---- add dialog
const KIND_LABEL = { app: "App", website: "Website", folder: "Folder", file: "File", command: "Command", action: "Action", group: "Group" };
let addKind = "app", addPick = null;
function openAdd() {
  addKind = "app"; addPick = null;
  $("kinds").replaceChildren(...Object.keys(KIND_LABEL).map((k) => el("button", { type: "button", class: k === addKind ? "on" : "",
    onclick: () => { addKind = k; addPick = null; openAddFields(); [...$("kinds").children].forEach((b) => b.classList.toggle("on", b.textContent === KIND_LABEL[k])); } }, KIND_LABEL[k])));
  openAddFields();
  $("addDialog").showModal();
}
function openAddFields() {
  const f = $("addFields");
  const name = el("input", { id: "add_name", placeholder: "Name on the screen" });
  if (addKind === "app") {
    const search = el("input", { placeholder: "Search installed apps..." });
    const list = el("div", { class: "app-list" }, el("div", {}, "Loading apps..."));
    const path = el("input", { id: "add_target", placeholder: "...or a program path, e.g. C:\\Games\\game.exe" });
    const draw = () => {
      const q = search.value.toLowerCase();
      list.replaceChildren(...(apps || []).filter((a) => a.name.toLowerCase().includes(q)).slice(0, 200).map((a) =>
        el("div", { class: addPick === a ? "on" : "", onclick: () => { addPick = a; name.value = a.name; path.value = a.target || a.path; draw(); } }, a.name)));
    };
    search.oninput = draw;
    f.replaceChildren(search, list, el("label", {}, "Program", path), el("label", {}, "Name", name));
    (apps ? Promise.resolve() : api("/api/apps").then((r) => { apps = r; })).then(draw).catch((e) => list.replaceChildren(el("div", {}, "Could not list apps: " + e.message)));
  } else if (addKind === "website") {
    f.replaceChildren(el("label", {}, "Web address", el("input", { id: "add_target", placeholder: "youtube.com" })),
      el("label", {}, "Open with", Object.assign(browserSelect("", () => {}), { id: "add_browser" })), el("label", {}, "Name", name));
  } else if (addKind === "group") {
    f.replaceChildren(el("label", {}, "Name", name));
  } else if (addKind === "action") {
    const sel = Object.assign(systemSelect("media_playpause", () => {}), { id: "add_system" });
    sel.addEventListener("change", () => { if (!name.dataset.touched) name.value = meta.system[sel.value]; });
    name.addEventListener("input", () => { name.dataset.touched = "1"; });
    name.value = meta.system["media_playpause"];
    f.replaceChildren(el("label", {}, "Built-in action", sel), el("label", {}, "Name", name));
  } else {
    f.replaceChildren(el("label", {}, targetLabel(addKind), el("input", { id: "add_target",
      placeholder: { folder: "C:\\Users\\you\\Downloads", file: "C:\\path\\to\\file.pdf", command: "notepad.exe" }[addKind] })),
    el("label", {}, "Name", name));
  }
}
async function confirmAdd() {
  const target = ($("add_target") || {}).value || "";
  let name = $("add_name").value.trim();
  if (!name) name = addKind === "website" ? target.replace(/^https?:\/\//, "").split("/")[0] : (target.split(/[\\/]/).pop() || KIND_LABEL[addKind]);
  const it = newItem(addKind, name, target);
  if (addKind === "website") it.browser = ($("add_browser") || {}).value || "";
  if (addKind === "action") it.options[0].value = ($("add_system") || {}).value || "media_playpause";
  if (addKind === "app" && addPick) { it.process = addPick.process || ""; it._lnk = addPick.path; if (!it.target) it.target = addPick.path; }
  menu.items.push(it);
  selected = it.id; markDirty(); renderEditor();
  if (["app", "website", "folder", "file"].includes(addKind) && target) fetchIcon(it);
}

// ---- settings
const SETTINGS = ["open_hold_ms", "accent", "background", "set_clock_on_start"];
function bindSettings() {
  for (const k of SETTINGS) {
    const input = $("s_" + k);
    if (input.type === "checkbox") input.checked = !!menu.settings[k]; else input.value = menu.settings[k];
    input.oninput = () => {
      menu.settings[k] = input.type === "checkbox" ? input.checked : input.type === "number" ? Number(input.value) : input.value;
      markDirty();
    };
  }
}

// ---- upload
async function upload() {
  setStatus("Uploading...");
  try {
    menu = await api("/api/menu", cleanMenu());
    dirty = false;
    setStatus("Uploaded: now on the keyboard screen", "ok");
    renderList(); renderEditor(); refreshPreview();
  } catch (e) { setStatus("Upload failed: " + e.message, "dirty"); }
}

// ---- start
window.addEventListener("beforeunload", (e) => { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
$("add").onclick = openAdd;
$("upload").onclick = upload;
$("pv_home").onclick = () => { previewScreen = "home"; refreshPreview(); };
$("pv_item").onclick = () => { previewScreen = "item"; refreshPreview(); };
$("addForm").addEventListener("submit", (e) => { if (e.submitter && e.submitter.value === "ok") confirmAdd(); });

(async () => {
  try {
    [menu, meta, browsers] = await Promise.all([api("/api/menu"), api("/api/meta"), api("/api/browsers")]);
    bindSettings(); renderList(); renderEditor(); refreshPreview();
    setStatus(meta.connected ? "Keyboard connected" : "Keyboard not connected yet", meta.connected ? "ok" : "");
  } catch (e) { setStatus("Cannot reach the menu app: " + e.message, "dirty"); }
})();
