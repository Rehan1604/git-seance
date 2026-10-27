"use strict";
// git séance front end. Vanilla JS, no build step, no network calls beyond 127.0.0.1.

const $ = (s) => document.querySelector(s);
const S = { health: null, repo: null, evidence: {}, history: [], busy: false, replay: null };
// Set by static/config.js. Empty means "same origin": the local app and the Render service serve the API themselves.
const API = String(window.SEANCE_API || "").replace(/\/$/, "");
const REPLAYS = window.SEANCE_REPLAYS || [];
const isPublic = () => Boolean(S.health ? S.health.public : API);
const sleep = (ms) => new Promise((r) => setTimeout(r, reduceMotionMs(ms)));
function reduceMotionMs(ms) { return matchMedia("(prefers-reduced-motion: reduce)").matches ? 0 : ms; }
const KIND = {
  commit: "Commit", todo: "Unfinished business", readme: "README", file: "Source file",
  unsaved: "Uncommitted work", stash: "Stash", branch: "Branches", successor: "Successor",
  vitals: "Vital record", layout: "Files",
};
const CITE_RE = /\[([A-Z]\d+(?:\s*,\s*[A-Z]\d+)*)\]/g;
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
// Dates show in the committer's own clock (the offset git recorded), like git log does, so they match the ghost's words.
function wallClock(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(iso || "");
  return m ? new Date(Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5])) : new Date(iso);
}
const fmtDate = (iso) => iso
  ? wallClock(iso).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" })
  : "never";
const fmtDateTime = (iso) => iso
  ? wallClock(iso).toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZone: "UTC" })
  : "never";
const candle = (text) => `<span class="thinking"><span class="candle" aria-hidden="true"></span>${esc(text)}</span>`;
const errHTML = (e) => `<p class="err">${esc(e.message || e)}</p>`;

function store(key, value) {
  try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); } catch { return null; }
  return null;
}

async function api(url, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  let res;
  try { res = await fetch(API + url, opts); }
  catch { throw new Error(API ? "Can't reach the séance room on Render. It may still be waking up; try again in a minute." : "The séance server isn't answering."); }
  if (!res.ok) throw new Error(await failure(res));
  return res.json();
}

async function failure(res) {
  try {
    const j = await res.json();
    if (j.detail) return typeof j.detail === "string" ? j.detail : j.detail.map((d) => d.msg).join("; ");
  } catch { /* not JSON */ }
  return `${res.status} ${res.statusText}`;
}

// ---------------------------------------------------------------- citations

// Same rules as seance/spirit.py and seance/retrieval.py, so the browser and the server agree.
const STOP = new Set(("the a an and or of to in on for with is it its this that be as at by from was were are " +
  "i you my your we our me what why how when did do does have has had not no so if then there their they them " +
  "he she his her who whom which would could should will can").split(" "));
const ids = (inner) => inner.split(/\s*,\s*/);
const bareLen = (s) => s.replace(CITE_RE, "").replace(/[\s.!?…,;:"'“”]/g, "").length;

function tokenize(text) {
  const out = [];
  for (const word of String(text).match(/[A-Za-z0-9_]+/g) || []) {
    let parts = word.split("_").flatMap((c) => c.match(/[A-Za-z][a-z]+|[A-Z]+(?![a-z])|\d+/g) || []);
    if (!parts.length) parts = [word];
    if (parts.length > 1) parts.push(word);
    for (const p of parts.map((x) => x.toLowerCase())) if (p.length > 1 && !STOP.has(p)) out.push(p);
  }
  return out;
}

// A citation is loose when its source shares no words with the sentence that cites it.
const isLoose = (claim, ev) => !tokenize(ev.match || ev.preview).some((t) => claim.has(t));
const claimOf = (sentence) => new Set(tokenize(sentence.replace(CITE_RE, " ")));

// Sentences with their trailing whitespace kept, so streamed text renders exactly as written.
// A stray "[C1]." after a full stop belongs to the sentence before it.
function spans(text) {
  const parts = String(text).split(/((?<=[.!?…])\s+(?=[A-Z"“‘'(\[]))/);
  const out = [];
  for (let i = 0; i < parts.length; i += 2) {
    const s = parts[i];
    const sep = parts[i + 1] || "";
    if (bareLen(s) < 3 && out.length) {
      const last = out[out.length - 1];
      last.text += last.sep + s;
      last.sep = sep;
    } else out.push({ text: s, sep });
  }
  return out;
}

function chip(id, claim) {
  const ev = S.evidence[id];
  if (!ev) return `<span class="cite phantom" title="Phantom memory: there is no evidence ${id}. The ghost made this one up.">${id}</span>`;
  return isLoose(claim, ev)
    ? `<button type="button" class="cite loose" data-id="${id}" data-loose="1" title="Loose: ${esc(ev.label)} doesn't mention what this sentence says. Check it.">${id}</button>`
    : `<button type="button" class="cite" data-id="${id}" title="${esc(ev.label)}">${id}</button>`;
}

function cited(text) {
  return spans(text).map(({ text: s, sep }) => {
    const claim = claimOf(s);
    return esc(s).replace(CITE_RE, (_, inner) => ids(inner).map((id) => chip(id, claim)).join("")) + esc(sep);
  }).join("");
}

function honesty(h) {
  if (!h || !h.sentences) return "";
  const loose = (h.loose || []).length;
  const notes = [];
  if (loose) notes.push(`<span class="loose-note">${plural(loose, "loose citation", "loose citations")}</span>`);
  if (h.phantom.length) notes.push(`<span class="lie">${plural(h.phantom.length, "phantom memory", "phantom memories")} caught</span>`);
  return `<p class="honesty">${h.grounded_sentences} of ${plural(h.sentences, "sentence", "sentences")} cite evidence${notes.length ? ", " + notes.join(", ") : ""}.</p>`;
}

function honestyOf(text) {
  const sentences = spans(text.trim()).filter((sp) => bareLen(sp.text) >= 3);
  const phantom = new Set();
  const loose = new Set();
  let grounded = 0;
  for (const { text: s } of sentences) {
    const cites = [...s.matchAll(CITE_RE)].flatMap((m) => ids(m[1]));
    cites.filter((id) => !S.evidence[id]).forEach((id) => phantom.add(id));
    const real = cites.filter((id) => S.evidence[id]);
    if (!real.length) continue;
    grounded += 1;
    const claim = claimOf(s);
    real.filter((id) => isLoose(claim, S.evidence[id])).forEach((id) => loose.add(id));
  }
  return { phantom: [...phantom], loose: [...loose], sentences: sentences.length, grounded_sentences: grounded };
}

function showReceipt(id, loose = false) {
  const ev = S.evidence[id];
  if (!ev) return;
  const box = $("#receipt");
  box.innerHTML = `
    <div class="receipt-head"><strong>${esc(id)}</strong><span>${esc(KIND[ev.kind] || ev.kind)}</span>
      <button type="button" id="receipt-close">Close</button></div>
    ${loose ? `<p class="loose-note">This source doesn't share a single word with the sentence that cites it. Read that sentence as feeling, not fact.</p>` : ""}
    <p>${esc(ev.label)}</p>
    <pre>${esc(ev.preview)}</pre>`;
  box.hidden = false;
  $("#receipt-close").focus();
}

// ---------------------------------------------------------------- the gate

function renderSpirit() {
  const h = S.health;
  const el = $("#spirit");
  if (!h && API) {
    el.innerHTML = `The live séance room on Render isn't answering yet (free servers sleep). The recorded séance below works right now; try summoning again in a minute.`;
    return;
  }
  if (!h) { el.innerHTML = `<span class="err">The séance server isn't answering. Is <code>python -m seance</code> running?</span>`; return; }
  if (h.public) {
    el.innerHTML = h.model_ready
      ? `<span class="candle" aria-hidden="true"></span>Live séances use <code>${esc(h.model)}</code>, an open-weight model, and see only public GitHub history. For your private graveyard, <a href="https://github.com/Rehan1604/git-seance">run git séance on your own machine</a>.`
      : `The live séance room is open, but it has no model key yet. The recorded séance below still works.`;
    return;
  }
  if (!h.ollama) {
    el.innerHTML = `The ghosts can't speak yet: Ollama isn't answering at <code>${esc(h.host)}</code>. Start it with <code>ollama serve</code>. You can still walk the graveyard.`;
  } else if (!h.model_ready) {
    el.innerHTML = `Ollama is up, but the ghost needs a vessel: run <code>ollama pull ${esc(h.model)}</code>.`;
  } else {
    el.innerHTML = `<span class="candle" aria-hidden="true"></span><code>${esc(h.model)}</code> is listening. It runs on this machine, so you can unplug the internet.`;
  }
}

async function walk() {
  if (isPublic()) { summon(); return; }
  const root = $("#root").value.trim();
  if (!root) { $("#root").focus(); return; }
  store("seance.root", root);
  $("#census").innerHTML = candle(`Walking ${root}…`);
  $("#graves").innerHTML = "";
  $("#breathing").innerHTML = "";
  try {
    renderGraveyard(await api(`/api/graveyard?root=${encodeURIComponent(root)}`));
  } catch (e) {
    $("#census").innerHTML = errHTML(e);
  }
}

// ---------------------------------------------------------------- the public demo

function setupPublicGate() {
  $("#root").placeholder = "github.com/owner/repo";
  $("#root").setAttribute("aria-label", "A public GitHub repo to summon");
  $("label[for=root]").textContent = "A public GitHub repo to summon";
  $("#summon button").textContent = "Summon its ghost";
  $("#root").value = "";
  $("#public-hint").hidden = false;
}

async function summon() {
  const url = $("#root").value.trim();
  if (!url) { $("#root").focus(); return; }
  const btn = $("#summon button");
  btn.disabled = true;
  $("#census").innerHTML = candle(`Digging up ${url}…`);
  try {
    const s = await api("/api/summon", { url });
    $("#root").value = "";
    loadPublicGraveyard();
    openRoom(s.path);
  } catch (e) {
    $("#census").innerHTML = errHTML(e);
  } finally {
    btn.disabled = false;
  }
}

async function loadPublicGraveyard() {
  renderGraveyard({ dead: [], alive: [] });
  if (!S.health) return;
  try { renderGraveyard(await api("/api/graveyard")); } catch { /* the replays still show */ }
}

function replayTombs() {
  return isPublic() || API ? REPLAYS.map((r) => ({ ...r.tomb, replay: r.key })) : [];
}

function renderGraveyard(g) {
  const replays = replayTombs();
  const dead = g.dead.length;
  const alive = g.alive.length;
  const days = S.health?.dead_after_days ?? 14;
  let census;
  if (isPublic() || (!S.health && API)) {
    census = dead
      ? `${replays.length ? "One séance recorded on a laptop, and " : ""}${plural(dead, "public repo", "public repos")} summoned by visitors.`
      : "One séance recorded on a laptop. Summon a dead public repo to hold a live one.";
  } else if (!dead && !alive) census = "No git repositories under this folder. Point it at the folder that holds your projects.";
  else if (!dead) census = `Nobody has died here. All ${alive} projects have had a commit in the last ${days} days.`;
  else census = `${dead === 1 ? "One project lies" : `${dead} projects lie`} here.` + (alive ? ` ${alive} still breathing.` : "");
  $("#census").textContent = census;
  $("#graves").innerHTML = [...replays, ...g.dead].map(tombHTML).join("");
  $("#breathing").innerHTML = alive
    ? "Still breathing: " + g.alive.map((t) => `<span>${esc(t.name)} (quiet for ${plural(t.days_silent, "day", "days")})</span>`).join(", ")
    : "";
}

function tombHTML(t) {
  const still = t.status === "stillborn";
  const dates = still ? "never born" : `${fmtDate(t.born)} – ${fmtDate(t.died)}`;
  const words = still ? "nothing was ever committed" : `“${t.last_words}”`;
  const target = t.replay ? `data-replay="${esc(t.replay)}"` : `data-path="${esc(t.path)}"`;
  return `
    <button type="button" class="tomb ${t.status}${t.replay ? " recorded" : ""}" ${target} aria-label="Open the grave of ${esc(t.name)}${t.replay ? " (recorded séance)" : ""}">
      ${t.replay ? `<span class="t-recorded">recorded séance</span>` : ""}
      <span class="t-lies">here lies</span>
      <span class="t-name">${esc(t.name)}</span>
      <span class="t-dates">${esc(dates)}</span>
      <span class="t-words">${esc(words)}</span>
      <span class="t-cause">${esc(t.cause_hint)}</span>
      ${t.successor ? `<span class="t-survived">survived by ${esc(t.successor.name)}</span>` : ""}
      <span class="petals" aria-hidden="true"></span>
    </button>`;
}

// ---------------------------------------------------------------- recorded séances

async function openReplay(key, push = true) {
  const meta = REPLAYS.find((r) => r.key === key);
  if (!meta) { showYard(); return; }
  resetRoom(meta.tomb.name);
  if (push) history.pushState({ replay: key }, "", `#replay:${encodeURIComponent(key)}`);
  try {
    const res = await fetch(meta.src);
    if (!res.ok) throw new Error(`Couldn't load the recording (${res.status}).`);
    const data = await res.json();
    S.replay = data;
    S.repo = data.repo;
    S.evidence = data.repo.evidence;
    renderRoom(data.repo);
    $("#r-note").hidden = false;
    $("#r-note").innerHTML = `<strong>Recorded séance.</strong> ${esc(data.recorded.note)}`;
    $("#asks").innerHTML = data.seance.turns.map((t) => `<button type="button" data-q="${esc(t.q)}">${esc(t.q)}</button>`).join("");
    setButtons(false);
  } catch (e) {
    $("#r-dates").innerHTML = errHTML(e);
  }
}

// Plays a recorded answer back at roughly the speed it was written, so the replay feels like the real thing.
async function* replayStream(text) {
  const parts = text.match(/\S+\s*/g) || [];
  for (const p of parts) { await sleep(28); yield p; }
}

function recordedAnswer(message, opening) {
  if (opening) return S.replay.seance.opening;
  const turn = S.replay.seance.turns.find((t) => t.q === message);
  return turn ? turn.a : "That question wasn't asked during the recording. Summon a public repo to ask your own.";
}

// ---------------------------------------------------------------- the room

const DEFAULT_ASKS = $("#asks").innerHTML;

function resetRoom(name) {
  S.repo = null; S.evidence = {}; S.history = []; S.replay = null;
  $("#yard").hidden = true;
  $("#room").hidden = false;
  $("#receipt").hidden = true;
  window.scrollTo(0, 0);
  $("#r-name").textContent = name;
  $("#r-dates").innerHTML = candle("Digging up the history…");
  $("#r-note").hidden = true;
  for (const id of ["#vitals", "#omens", "#todos", "#autopsy", "#chat", "#rite"]) $(id).innerHTML = "";
  $("#ekg").innerHTML = ""; $("#ekg-cap").innerHTML = "";
  $("#asks").innerHTML = DEFAULT_ASKS;
  $("#asks").hidden = true; $("#ask").hidden = true; $("#do-begin").hidden = false; $("#do-autopsy").hidden = false;
  setButtons(true);
}

async function openRoom(path, push = true) {
  const base = path.split(/[\\/]/).filter(Boolean).pop() || path;
  resetRoom(isPublic() ? base.replace("__", "/") : base);
  if (push) history.pushState({ path }, "", `#${encodeURIComponent(path)}`);
  try {
    const d = await api(`/api/repo?path=${encodeURIComponent(path)}`);
    S.repo = d;
    S.evidence = d.evidence;
    renderRoom(d);
    if (d.public) {
      $("#r-note").hidden = false;
      $("#r-note").innerHTML = `<strong>Live séance.</strong> The ghost speaks through <code>${esc(d.model)}</code>, an open-weight model, and knows only this repo's public GitHub history.`;
    }
    setButtons(false);
  } catch (e) {
    $("#r-dates").innerHTML = errHTML(e);
  }
}

function showYard() {
  $("#room").hidden = true;
  $("#yard").hidden = false;
  $("#receipt").hidden = true;
}

function renderRoom(d) {
  const v = d.vitals;
  $("#r-name").textContent = v.name;
  $("#r-dates").textContent = v.status === "stillborn"
    ? "Never born. git init was run, and then nothing."
    : `${fmtDate(v.born)} – ${fmtDate(v.died)}`;
  drawEKG(d.activity, v);

  const rows = [];
  if (v.status !== "stillborn") {
    rows.push(["Born", fmtDate(v.born), `first words: “${v.first_words}”`]);
    rows.push(["Time of death", fmtDateTime(v.died)]);
    rows.push(["Silent for", plural(v.days_silent, "day", "days")]);
    rows.push(["Lived", `${plural(v.lifespan_days, "day", "days")}, ${plural(v.commits, "commit", "commits")}`]);
    rows.push(["Last words", `“${v.last_words}”`, `commit ${v.last_hash}`, "last-words"]);
  }
  const unsaved = v.modified.length + v.untracked.length;
  if (unsaved) rows.push(["Died holding", plural(unsaved, "uncommitted file", "uncommitted files"), v.dirty_stat]);
  if (v.stashes.length) rows.push(["Buried with it", plural(v.stashes.length, "stash never popped", "stashes never popped")]);
  if (v.unmerged.length) rows.push(["Paths not taken", v.unmerged.join(", ")]);
  if (d.successor) rows.push(["Survived by", d.successor.name, `first commit ${plural(d.successor.days_after, "day", "days")} after this one's last`]);
  if (v.authors.length) rows.push(["Made by", v.authors.join(", ")]);
  if (d.languages.length) rows.push(["Written in", d.languages.join(", ")]);
  rows.push(["Hidden from the ghost", d.redactions
    ? `${plural(d.redactions, "secret", "secrets")} redacted before the model saw anything`
    : "No secrets found to redact"]);
  $("#vitals").innerHTML = rows.map(([k, val, sub, cls]) =>
    `<dt>${esc(k)}</dt><dd${cls ? ` class="${cls}"` : ""}>${esc(val)}${sub ? `<small>${esc(sub)}</small>` : ""}</dd>`).join("");

  $("#omens").innerHTML = d.signals.length
    ? d.signals.map((s) => `<li><strong>${esc(s.label)}</strong><span>${esc(s.detail)}</span></li>`).join("")
    : `<li class="empty">No omens. It went quietly.</li>`;

  const todos = Object.entries(d.evidence).filter(([, e]) => e.kind === "todo").slice(0, 14);
  $("#todos").innerHTML = todos.length
    ? todos.map(([id, e]) => {
        const text = (e.preview.includes(" — ") ? e.preview.split(" — ").slice(1).join(" — ") : e.preview).replace(/^\[ \] /, "");
        return `<li><button type="button" data-id="${id}"><code>${esc(e.label)}</code>${esc(text)}</button></li>`;
      }).join("")
    : `<li class="empty">No TODOs left behind. It either finished, or never made plans.</li>`;

  $("#autopsy").innerHTML = d.autopsy ? "" : `<p class="hint">Ask the ghost to explain its own death, citing its own commits. Takes a minute or two on a laptop CPU.</p>`;
  if (d.autopsy) renderAutopsy(d.autopsy);
  if (d.resurrection) renderRise(d.resurrection);
}

function drawEKG(act, v) {
  const svg = $("#ekg");
  const box = svg.getBoundingClientRect();
  const W = Math.max(280, Math.round(box.width || 1000));
  const H = Math.max(100, Math.round(box.height || 170));
  const base = Math.round(H * 0.69);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  const b = act.buckets || [];
  if (!b.length) {
    svg.innerHTML = `<line class="flat" x1="0" y1="${base}" x2="${W}" y2="${base}"/>`;
    $("#ekg-cap").innerHTML = `<span class="cap-born">It never drew breath.</span>`;
    return;
  }
  const life = Math.max(act.lifespan_days, 1);
  const silence = Math.max(act.silence_days, 0);
  const frac = Math.min(0.78, Math.max(0.34, life / (life + silence)));
  const aliveW = W * frac;
  const step = aliveW / b.length;
  const max = Math.max(...b);
  const pts = [[0, base]];
  b.forEach((c, i) => {
    if (!c) return;
    const x = i * step;
    const h = H * (0.13 + 0.46 * (c / max));
    pts.push([x + step * .18, base], [x + step * .3, base + H * .05], [x + step * .48, base - h], [x + step * .64, base + H * .09], [x + step * .8, base]);
  });
  pts.push([aliveW, base]);
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(" ");
  const bx = W - Math.min(70, W * 0.12);
  svg.innerHTML = `
    <polyline class="beat" points="${line}"/>
    <line class="flat" x1="${aliveW.toFixed(1)}" y1="${base}" x2="${W}" y2="${base}"/>
    <line class="tod-line" x1="${aliveW.toFixed(1)}" y1="8" x2="${aliveW.toFixed(1)}" y2="${H - 6}"/>
    <polyline class="blip" points="${bx - 14},${base} ${bx - 6},${base + H * .04} ${bx},${base - H * .2} ${bx + 7},${base + H * .06} ${bx + 15},${base}"/>`;
  const pct = (frac * 100).toFixed(1);
  const shift = frac > 0.6 ? "translateX(calc(-100% - 8px))" : "translateX(8px)";
  $("#ekg-cap").innerHTML = `
    <span class="cap-born">${esc(fmtDate(v.born))}</span>
    <span class="cap-tod" style="left:${pct}%;transform:${shift}">time of death, ${esc(fmtDateTime(v.died))}</span>
    <span class="cap-now">${esc(plural(silence, "day", "days"))} of silence</span>`;
  if (reduceMotion) return;
  const beat = svg.querySelector(".beat");
  const flat = svg.querySelector(".flat");
  const len = beat.getTotalLength();
  const flen = W - aliveW;
  beat.style.strokeDasharray = flat.style.strokeDasharray = `${Math.max(len, flen)}`;
  beat.style.strokeDashoffset = `${len}`;
  flat.style.strokeDashoffset = `${flen}`;
  requestAnimationFrame(() => requestAnimationFrame(() => {
    beat.style.transition = "stroke-dashoffset 2.4s ease-in-out";
    beat.style.strokeDashoffset = "0";
    flat.style.transition = "stroke-dashoffset 1.6s linear 2.4s";
    flat.style.strokeDashoffset = "0";
  }));
}

function setButtons(disabled) {
  for (const id of ["#do-autopsy", "#do-begin", "#do-rise", "#do-bury"]) $(id).disabled = disabled;
  for (const b of document.querySelectorAll("#asks button, #ask button")) b.disabled = disabled;
}

// ---------------------------------------------------------------- autopsy

async function doAutopsy(fresh = false) {
  if (!S.repo || S.busy) return;
  S.busy = true; setButtons(true);
  $("#autopsy").innerHTML = candle("The ghost is examining its own remains…");
  try {
    if (S.replay) await sleep(1400);
    const a = S.replay ? S.replay.autopsy : await api("/api/autopsy", { path: S.repo.vitals.path, fresh });
    S.repo.autopsy = a;
    renderAutopsy(a);
  } catch (e) {
    $("#autopsy").innerHTML = errHTML(e);
  } finally {
    S.busy = false; setButtons(false);
  }
}

function renderAutopsy(a) {
  const overruled = a.cause_of_death !== a.coroner_hint
    ? `<p class="overrule">The ghost overruled the coroner's first guess, “${esc(a.coroner_hint)}”.</p>` : "";
  $("#autopsy").innerHTML = `
    <p class="cause">${esc(a.cause_of_death)}</p>
    ${overruled}
    <p>${cited(a.cause_explanation)}</p>
    <h4>What it wanted to be</h4><p>${cited(a.what_i_wanted_to_be)}</p>
    <h4>Its last moment</h4><p>${cited(a.last_moment)}</p>
    ${a.epitaph ? `<blockquote class="epitaph">${esc(a.epitaph)}</blockquote>` : ""}
    ${honesty(a.honesty)}
    ${S.replay ? `<p class="honesty">Recorded: ${esc(S.replay.recorded.timings.autopsy)}.</p>` : `<button type="button" class="again" data-again="autopsy">Ask again</button>`}`;
  $("#do-autopsy").hidden = true;
}

// ---------------------------------------------------------------- séance

function addLine(who, html) {
  const el = document.createElement("div");
  el.className = `line ${who}`;
  const name = who === "ghost" ? `the ghost of ${S.repo.vitals.name}` : "you";
  el.innerHTML = `<span class="who">${esc(name)}</span><div class="words">${html}</div>`;
  $("#chat").appendChild(el);
  $("#chat").scrollTop = $("#chat").scrollHeight;
  return el;
}

async function speak(message, opening = false) {
  if (!S.repo || S.busy) return;
  S.busy = true; setButtons(true);
  if (!opening) addLine("you", esc(message));
  const line = addLine("ghost", candle(opening ? "Lighting the candle…" : "The ghost is gathering itself…"));
  const words = line.querySelector(".words");
  document.body.classList.add("possessed");
  let text = "";
  try {
    if (S.replay) {
      await sleep(900);
      for await (const chunk of replayStream(recordedAnswer(message, opening))) {
        text += chunk;
        words.innerHTML = cited(text);
        $("#chat").scrollTop = $("#chat").scrollHeight;
      }
    } else {
      let res;
      try {
        res = await fetch(API + "/api/seance", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ path: S.repo.vitals.path, message, opening, history: S.history.slice(-8) }),
        });
      } catch { throw new Error("Lost the connection to the séance room."); }
      if (!res.ok) throw new Error(await failure(res));
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        text += dec.decode(value, { stream: true });
        words.innerHTML = cited(text);
        $("#chat").scrollTop = $("#chat").scrollHeight;
      }
      text += dec.decode();
    }
    words.innerHTML = cited(text.trim());
    S.history.push({ role: "user", content: opening ? "__opening__" : message },
                   { role: "assistant", content: text.trim() });
    line.insertAdjacentHTML("beforeend", honesty(honestyOf(text)));
    if (opening) { $("#do-begin").hidden = true; $("#asks").hidden = false; $("#ask").hidden = Boolean(S.replay); }
  } catch (e) {
    words.innerHTML = errHTML(e);
  } finally {
    document.body.classList.remove("possessed");
    S.busy = false; setButtons(false);
    if (!$("#ask").hidden) $("#q").focus();
  }
}

// ---------------------------------------------------------------- rites

async function doRise(fresh = false) {
  if (!S.repo || S.busy) return;
  S.busy = true; setButtons(true);
  $("#rite").innerHTML = candle("The ghost is deciding whether it wants to rise…");
  try {
    if (S.replay) await sleep(1400);
    renderRise(S.replay ? S.replay.resurrection : await api("/api/resurrect", { path: S.repo.vitals.path, fresh }));
  } catch (e) {
    $("#rite").innerHTML = errHTML(e);
  } finally {
    S.busy = false; setButtons(false);
  }
}

function renderRise(r) {
  const steps = r.steps.map((s) => `<li><strong>${esc(s.title)}</strong>${cited(s.detail)}</li>`).join("");
  $("#rite").innerHTML = `
    <p class="verdict ${r.verdict}">${r.verdict === "rise" ? "It wants to rise." : "It would rather rest."}</p>
    <p>${cited(r.why)}</p>
    ${steps ? `<h4>${r.verdict === "rise" ? "Three steps back to life" : "If you bring it back anyway"}</h4><ol class="steps">${steps}</ol>` : ""}
    <p class="first-words">First words of its second life: <code>git commit -m "${esc(r.first_words)}"</code></p>
    ${honesty(r.honesty)}
    ${S.replay ? `<p class="honesty">Recorded: ${esc(S.replay.recorded.timings.resurrection)}.</p>` : `<button type="button" class="again" data-again="rise">Ask again</button>`}`;
}

async function doBury(fresh = false) {
  if (!S.repo || S.busy) return;
  S.busy = true; setButtons(true);
  $("#rite").innerHTML = candle("The ghost is writing its own obituary…");
  try {
    if (S.replay) await sleep(1400);
    const b = S.replay ? S.replay.obituary : await api("/api/bury", { path: S.repo.vitals.path, fresh });
    if (b.autopsy) { S.repo.autopsy = b.autopsy; renderAutopsy(b.autopsy); }
    const canWrite = !S.replay && !isPublic();
    $("#rite").innerHTML = `
      <pre class="obit" id="obit">${esc(b.markdown)}</pre>
      ${honesty(b.honesty)}
      <div class="rite-buttons">
        <button type="button" class="ghostly" id="copy-obit">Copy</button>
        ${canWrite ? `<button type="button" class="ghostly" id="write-obit">Write OBITUARY.md to the repo</button>` : ""}
      </div>`;
    $("#copy-obit").onclick = async () => {
      try { await navigator.clipboard.writeText(b.markdown); $("#copy-obit").textContent = "Copied"; }
      catch { $("#copy-obit").textContent = "Select the text above to copy"; }
    };
    if (canWrite) $("#write-obit").onclick = async () => {
      if (!confirm(`Write OBITUARY.md into ${S.repo.vitals.path}?\nAn existing OBITUARY.md there will be replaced.`)) return;
      try {
        await api("/api/bury/write", { path: S.repo.vitals.path, markdown: b.markdown });
        $("#write-obit").textContent = "Written to OBITUARY.md";
        $("#write-obit").disabled = true;
      } catch (e) { $("#write-obit").textContent = e.message; }
    };
  } catch (e) {
    $("#rite").innerHTML = errHTML(e);
  } finally {
    S.busy = false; setButtons(false);
  }
}

// ---------------------------------------------------------------- wiring

document.addEventListener("click", (e) => {
  const cite = e.target.closest("button.cite, .todos button[data-id]");
  if (cite) { showReceipt(cite.dataset.id, cite.dataset.loose === "1"); return; }
  if (e.target.id === "receipt-close") { $("#receipt").hidden = true; return; }
  const tomb = e.target.closest(".tomb");
  if (tomb) { tomb.dataset.replay ? openReplay(tomb.dataset.replay) : openRoom(tomb.dataset.path); return; }
  const example = e.target.closest("[data-example]");
  if (example) { $("#root").value = example.dataset.example; summon(); return; }
  const ask = e.target.closest("#asks button");
  if (ask) { speak(ask.dataset.q); return; }
  const again = e.target.closest("[data-again]");
  if (again) { again.dataset.again === "autopsy" ? doAutopsy(true) : doRise(true); }
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#receipt").hidden = true; });

$("#summon").addEventListener("submit", (e) => { e.preventDefault(); walk(); });
$("#back").addEventListener("click", () => { history.pushState({}, "", location.pathname); showYard(); });
$("#do-autopsy").addEventListener("click", () => doAutopsy(false));
$("#do-begin").addEventListener("click", () => speak("", true));
$("#do-rise").addEventListener("click", () => doRise(false));
$("#do-bury").addEventListener("click", () => doBury(false));
$("#ask").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("#q").value.trim();
  if (!q) return;
  $("#q").value = "";
  speak(q);
});
window.addEventListener("popstate", (e) => {
  if (e.state && e.state.replay) openReplay(e.state.replay, false);
  else if (e.state && e.state.path) openRoom(e.state.path, false);
  else showYard();
});

function openFromHash() {
  if (location.hash.length <= 1) return;
  const h = decodeURIComponent(location.hash.slice(1));
  if (h.startsWith("replay:")) openReplay(h.slice(7), false);
  else openRoom(h, false);
}

(async function init() {
  if (API) {
    // The hosted page: show the recorded séance at once, then wake the live room (Render's free tier sleeps).
    setupPublicGate();
    renderGraveyard({ dead: [], alive: [] });
    $("#spirit").innerHTML = candle("Waking the live séance room on Render (free servers can take up to a minute)…");
    if (location.hash.startsWith("#replay:")) openFromHash();
    try { S.health = await api("/api/health"); } catch { S.health = null; }
    renderSpirit();
    loadPublicGraveyard();
    if (location.hash.length > 1 && !location.hash.startsWith("#replay:")) openFromHash();
    return;
  }
  try { S.health = await api("/api/health"); } catch { S.health = null; }
  renderSpirit();
  if (isPublic()) {
    setupPublicGate();
    loadPublicGraveyard();
  } else {
    $("#root").value = store("seance.root") || S.health?.default_root || "";
    if ($("#root").value) walk();
  }
  openFromHash();
})();
