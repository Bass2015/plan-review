"use strict";

const BLAST = {
  file: { icon: "candle", label: "One file" },
  module: { icon: "fire", label: "One module" },
  packages: { icon: "explosion", label: "Several packages or dependent code" },
  prod: { icon: "nuke", label: "Production, other repos, shared infra" },
};
const UNDO = { easy: { label: "Easy to undo" }, hard: { label: "Hard to undo" } };
const VERDICTS = {
  ok: { label: "ok" },
  question: { label: "question" },
  concern: { label: "concern" },
  blocker: { label: "blocker" },
};
const VERDICT_RANK = { blocker: 4, concern: 3, question: 2, ok: 1 };

const S = {
  data: null,
  pending: { comments: [], resolve: [], reopen: [] },
  composing: {}, // composer key -> { text, verdict }
  general: "",
  openTask: null,
  known: new Set(),
  sent: false,
};

const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
const dark = matchMedia("(prefers-color-scheme: dark)").matches;

marked.setOptions({ gfm: true, breaks: false });
mermaid.initialize({
  startOnLoad: false,
  securityLevel: "strict",
  theme: "base",
  themeVariables: dark
    ? { background: "transparent", primaryColor: "#2c2823", primaryBorderColor: "#6e665a", primaryTextColor: "#ebe4d8", secondaryColor: "#2c2823", tertiaryColor: "#23201c", lineColor: "#a39887", edgeLabelBackground: "#23201c", fontFamily: "-apple-system, system-ui, sans-serif", fontSize: "14px" }
    : { background: "transparent", primaryColor: "#efe8dc", primaryBorderColor: "#b9ab96", primaryTextColor: "#2a2622", secondaryColor: "#e8dfcf", tertiaryColor: "#fbf8f2", lineColor: "#8a7f72", edgeLabelBackground: "#fbf8f2", fontFamily: "-apple-system, system-ui, sans-serif", fontSize: "14px" },
});

function md(text) {
  return DOMPurify.sanitize(marked.parse(text || ""));
}

async function renderMermaid(root) {
  const nodes = [];
  root.querySelectorAll("code.language-mermaid").forEach((code) => {
    const div = document.createElement("div");
    div.className = "mermaid";
    div.textContent = code.textContent;
    code.closest("pre").replaceWith(div);
    nodes.push(div);
  });
  if (nodes.length) {
    try {
      await mermaid.run({ nodes });
    } catch (err) {
      console.warn("mermaid", err);
    }
  }
}

function setMd(el, text) {
  el.innerHTML = md(text);
  renderMermaid(el);
}

function blastIcon(blast, cls = "blast") {
  const b = BLAST[blast];
  if (!b) return "";
  return `<svg class="${cls}" role="img" aria-label="${esc(b.label)}"><title>Blast radius: ${esc(b.label)}</title><use href="#${b.icon}"/></svg>`;
}

// Reversible is the norm; only the exception is flagged.
function undoFlag(undo) {
  return undo === "hard" ? `<span class="flag hard">hard to undo</span>` : "";
}

function verdictTag(verdict) {
  return verdict && VERDICTS[verdict] ? `<span class="verdict v-${verdict}"><i class="dot"></i>${esc(VERDICTS[verdict].label)}</span>` : "";
}

const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;

/* ---------- Server I/O ---------- */

async function post(path, body) {
  const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

let draftTimer = null;
function saveDraft() {
  clearTimeout(draftTimer);
  draftTimer = setTimeout(() => {
    post("api/draft", { pending: S.pending, composing: S.composing, general: S.general }).catch(() => {});
  }, 400);
}

/* ---------- Thread state helpers ---------- */

const isOrphan = (t) => t.target !== "general" && !S.known.has(t.target);
const threadsFor = (target) =>
  S.data.threads.filter((t) => t.target === target || (target === "general" && isOrphan(t)));
const pendingNewFor = (target) => S.pending.comments.filter((c) => c.target === target && !c.thread);
const pendingRepliesFor = (threadId) => S.pending.comments.filter((c) => c.thread === threadId);

function isResolved(t) {
  return t.resolved ? !S.pending.reopen.includes(t.id) : S.pending.resolve.includes(t.id);
}

function toggleResolved(t) {
  const list = t.resolved ? S.pending.reopen : S.pending.resolve;
  const i = list.indexOf(t.id);
  if (i >= 0) list.splice(i, 1);
  else list.push(t.id);
}

function targetSummary(target) {
  const threads = threadsFor(target);
  const open = threads.filter((t) => !isResolved(t));
  const pending = S.pending.comments.filter(
    (c) => c.target === target || threads.some((t) => t.id === c.thread),
  );
  const verdicts = [
    ...open.map((t) => t.verdict),
    ...pending.map((c) => c.verdict),
  ].filter(Boolean);
  const top = verdicts.sort((a, b) => VERDICT_RANK[b] - VERDICT_RANK[a])[0] || null;
  const replied = threads.some((t) => {
    const last = t.messages[t.messages.length - 1];
    return last && last.author === "claude" && last.round === S.data.round;
  });
  return { open: open.length, pending: pending.length, top, replied };
}

/* ---------- Rendering: threads and composers ---------- */

function messageHtml(m, label) {
  const v = verdictTag(m.verdict);
  const upd = (m.updated_tasks || []).length ? `<span class="flag updated">updated ${esc(m.updated_tasks.join(", "))}</span>` : "";
  return `<div class="msg ${m.author}">
    <div class="msg-meta"><b>${label}</b><span>round ${m.round}</span>${v}${upd}</div>
    <div class="msg-body">${md(m.text)}</div>
  </div>`;
}

function pendingHtml(c) {
  const v = verdictTag(c.verdict);
  return `<div class="msg pending">
    <div class="msg-meta"><b>You</b><span>not sent yet</span>${v}<button class="mini x" data-remove="${esc(c.key)}">remove</button></div>
    <div class="msg-body">${md(c.text)}</div>
  </div>`;
}

function composerHtml(target, threadId, { standalone = false, compact = false, placeholder = "Add a comment, question or note…" } = {}) {
  const key = `${target}|${threadId || ""}`;
  const c = S.composing[key] || { text: "", verdict: null };
  const vbtns = Object.entries(VERDICTS)
    .map(([k, v]) => `<button class="vbtn" data-verdict="${k}" aria-pressed="${c.verdict === k}" title="${v.label}"><i class="dot v-${k}"></i>${v.label}</button>`)
    .join("");
  return `<div class="composer ${standalone ? "standalone" : ""} ${compact ? "compact" : ""} ${c.text || c.verdict ? "has-text" : ""}" data-key="${esc(key)}" data-target="${esc(target)}" data-thread="${esc(threadId || "")}">
    <textarea rows="${compact ? 1 : 3}" placeholder="${esc(placeholder)}">${esc(c.text)}</textarea>
    <div class="composer-row">${vbtns}<span class="spacer"></span><span class="hint">Enter to add · Shift+Enter for a new line</span><button class="btn small primary" data-add>Add</button></div>
  </div>`;
}

function threadHtml(t) {
  const resolved = isResolved(t);
  const msgs = t.messages.map((m) => messageHtml(m, m.author === "you" ? "You" : "Claude")).join("");
  const pend = pendingRepliesFor(t.id).map(pendingHtml).join("");
  const waiting = t.awaiting_reply && !resolved ? `<span class="muted">waiting for Claude</span>` : "";
  const replyKey = `${t.target}|${t.id}`;
  const replying = S.composing[replyKey] !== undefined;
  return `<div class="thread ${resolved ? "resolved" : ""}" data-thread-id="${esc(t.id)}">
    <div class="thread-head">
      <span class="tid">${esc(t.id)}</span>
      ${isOrphan(t) ? `<span class="muted">on ${esc(t.target)}, no longer in the plan</span>` : ""}
      ${t.verdict ? `<i class="dot v-${esc(t.verdict)}" title="${esc(t.verdict)}"></i>` : ""}
      ${resolved ? `<span class="muted">resolved</span>` : waiting}
      <span class="actions">
        ${resolved ? "" : `<button class="mini" data-reply="${esc(t.id)}">Reply</button>`}
        <button class="mini" data-resolve="${esc(t.id)}">${resolved ? "Reopen" : "Resolve"}</button>
      </span>
    </div>
    ${resolved ? "" : msgs + pend}
    ${!resolved && replying ? composerHtml(t.target, t.id, { placeholder: "Reply…" }) : ""}
  </div>`;
}

function renderThreads(slot, { collapsed = true } = {}) {
  const target = slot.dataset.target;
  const newKey = `${target}|`;
  const composer = slot.dataset.inline
    ? composerHtml(target, null, { standalone: true, compact: true, placeholder: `Comment on ${target}…` })
    : !collapsed || S.composing[newKey] !== undefined
    ? composerHtml(target, null, { standalone: true })
    : `<button class="btn small ghost add-comment" data-open-composer="${esc(newKey)}">Add a comment</button>`;
  const threads = threadsFor(target);
  const open = threads.filter((t) => !isResolved(t));
  const closed = threads.filter((t) => isResolved(t));
  slot.innerHTML =
    open.map(threadHtml).join("") +
    pendingNewFor(target).map((c) => `<div class="thread">${pendingHtml(c)}</div>`).join("") +
    // New overall comments come from the footer box; this panel only holds their threads.
    (target === "general" ? "" : composer) +
    (closed.length
      ? `<details><summary class="muted">${closed.length} resolved</summary><div class="threads-slot">${closed.map(threadHtml).join("")}</div></details>`
      : "");
}

/* ---------- Rendering: page ---------- */

function renderStatic() {
  const d = S.data;
  document.title = `Review: ${d.title}`;
  $("#title").textContent = d.title;
  $("#round").textContent = `Round ${d.round}`;

  if (d.warnings.length) {
    $("#warnings").innerHTML = `<details class="warnings"><summary>${plural(d.warnings.length, "format warning")} <span class="muted">· sent to Claude with your feedback</span></summary><ul>${d.warnings.map((w) => `<li>${md(w).replace(/^<p>|<\/p>\s*$/g, "")}</li>`).join("")}</ul></details>`;
  }
  if (d.preamble) setMd($("#preamble"), d.preamble);

  if (d.context) {
    $("#context").innerHTML = `<div class="panel context"><h2 class="eyebrow">${esc(d.context.title)}</h2><div class="md"></div><div class="threads-slot" data-target="context"></div></div>`;
    setMd($("#context .md"), d.context.md);
  }

  $("#task-count").textContent = d.tasks.length ? `· ${d.tasks.length}` : "";
  $("#legend").innerHTML = Object.values(BLAST).map((b) => `<span>${blastIcon(Object.keys(BLAST).find((k) => BLAST[k] === b), "")}${esc(b.label)}</span>`).join("");
  if (!d.tasks.length) $("#board").outerHTML = `<p class="muted">No task cards in this plan: see the sections below.</p>`;

  $("#sections").innerHTML = d.sections
    .map((s) => {
      const body = s.items.length
        ? `<div class="md intro"></div>${s.items.map((i) => `<div class="item"><div class="md" data-item="${esc(i.id)}"></div><div class="threads-slot" data-target="${esc(i.id)}" data-inline="1"></div></div>`).join("")}`
        : `<div class="md"></div>`;
      return `<details class="panel" data-slug="${esc(s.slug)}" ${s.items.length ? "open" : ""}><summary>${esc(s.title)}<span class="count"></span></summary>${body}<div class="threads-slot" data-target="${esc(s.slug)}"></div></details>`;
    })
    .join("");
  d.sections.forEach((s) => {
    const panel = $(`#sections [data-slug="${CSS.escape(s.slug)}"]`);
    if (!s.items.length) return setMd($(".md", panel), s.md);
    if (s.intro) setMd($(".intro", panel), s.intro);
    s.items.forEach((i) => setMd($(`[data-item="${CSS.escape(i.id)}"]`, panel), i.md));
  });
}

function renderBoard() {
  const board = $("#board");
  if (!board) return;
  board.innerHTML = S.data.tasks
    .map((t) => {
      const sum = targetSummary(t.id);
      const meta = [...Object.values(t.labels), ...(t.deps.length ? [`after ${t.deps.join(", ")}`] : [])].map(esc).join(" · ");
      const status = [
        t.change ? `<span class="flag ${t.change}">${t.change}</span>` : "",
        sum.replied ? `<span class="flag replied">Claude replied</span>` : "",
        sum.open ? `<span>${sum.open} open</span>` : "",
        sum.pending ? `<span class="flag pending">${sum.pending} unsent</span>` : "",
        undoFlag(t.undo),
      ].filter(Boolean).join("");
      return `<button class="card" data-task="${esc(t.id)}">
        <div class="card-top"><span class="tid">${esc(t.id)}</span>${sum.top ? `<i class="dot v-${sum.top}" title="${sum.top}"></i>` : ""}${blastIcon(t.blast)}</div>
        <div class="card-title">${esc(t.title)}</div>
        ${meta ? `<div class="card-meta">${meta}</div>` : ""}
        ${status ? `<div class="card-status">${status}</div>` : ""}
      </button>`;
    })
    .join("");
}

function renderDrawer() {
  const t = S.data.tasks.find((x) => x.id === S.openTask);
  if (!t) return;
  $("#d-id").textContent = t.id;
  $("#d-title").textContent = t.title;
  const b = BLAST[t.blast];
  const u = UNDO[t.undo];
  const field = (k, v, cls = "") => `<div class="field ${cls}"><dt>${esc(k)}</dt><dd>${v}</dd></div>`;
  $("#d-meta").innerHTML = `<dl class="fields">${[
    b ? field("blast radius", `${blastIcon(t.blast)}<span>${esc(b.label)}</span>`, "blast-field") : "",
    u ? field("undo", esc(u.label), t.undo === "hard" ? "hard" : "") : "",
    ...Object.entries(t.labels).map(([k, v]) => field(k, esc(v))),
    t.deps.length ? field("after", esc(t.deps.join(", "))) : "",
    t.files.length ? field("files", t.files.map((f) => `<code>${esc(f)}</code>`).join(" "), "wide") : "",
  ].join("")}</dl>`;
  if (t.change === "updated" && t.diff.length) {
    const diff = t.diff.map(([op, text]) => (op === "insert" ? `<ins>${esc(text)}</ins>` : op === "delete" ? `<del>${esc(text)}</del>` : esc(text))).join("");
    $("#d-change").innerHTML = `<div class="change"><button class="mini" id="toggle-diff">Show what changed since last round</button><div class="diff" hidden>${diff}</div></div>`;
  } else if (t.change === "new") {
    $("#d-change").innerHTML = `<div class="change">New this round.</div>`;
  } else {
    $("#d-change").innerHTML = "";
  }
  setMd($("#d-body"), t.md);
  $("#d-threads").dataset.target = t.id;
  renderThreads($("#d-threads"), { collapsed: false });
}

function renderDynamic() {
  renderBoard();
  document.querySelectorAll("main .threads-slot[data-target]").forEach(renderThreads);
  document.querySelectorAll("#sections details").forEach((el) => {
    const section = S.data.sections.find((s) => s.slug === el.dataset.slug);
    const sums = [section.slug, ...section.items.map((i) => i.id)].map(targetSummary);
    const open = sums.reduce((n, x) => n + x.open, 0);
    const pending = sums.reduce((n, x) => n + x.pending, 0);
    el.querySelector(".count").textContent = [open ? `${open} open` : "", pending ? `${pending} unsent` : ""].filter(Boolean).join(" · ");
  });
  $("#general").hidden = !threadsFor("general").length;
  if (S.openTask) renderThreads($("#d-threads"), { collapsed: false });
  const n = S.pending.comments.length;
  const r = S.pending.resolve.length + S.pending.reopen.length;
  $("#pending-summary").textContent = [n ? `${n} unsent comment${n > 1 ? "s" : ""}` : "", r ? `${r} resolve change${r > 1 ? "s" : ""}` : ""].filter(Boolean).join(" · ");
  $("#btn-approve").title = n ? "Approve; your unsent comments go along as notes for the implementation" : "Approve the plan";
}

function openDrawer(id) {
  S.openTask = id;
  renderDrawer();
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#scrim").hidden = false;
  $("#drawer").scrollTop = 0;
}

function closeDrawer() {
  S.openTask = null;
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#scrim").hidden = true;
}

/* ---------- Events ---------- */

document.addEventListener("click", (e) => {
  const el = e.target.closest("button, [data-task]");
  if (!el || S.sent) return;

  if (el.dataset.task) return openDrawer(el.dataset.task);
  if (el.id === "d-close") return closeDrawer();
  if (el.id === "toggle-diff") {
    const diff = el.nextElementSibling;
    diff.hidden = !diff.hidden;
    el.textContent = diff.hidden ? "Show what changed since last round" : "Hide changes";
    return;
  }
  if (el.dataset.verdict) {
    const comp = el.closest(".composer");
    const key = comp.dataset.key;
    const cur = S.composing[key] || { text: comp.querySelector("textarea").value, verdict: null };
    cur.verdict = cur.verdict === el.dataset.verdict ? null : el.dataset.verdict;
    S.composing[key] = cur;
    comp.querySelectorAll(".vbtn").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.verdict === cur.verdict)));
    saveDraft();
    return;
  }
  if (el.hasAttribute("data-add")) {
    const comp = el.closest(".composer");
    const text = comp.querySelector("textarea").value.trim();
    if (!text) return comp.querySelector("textarea").focus();
    const key = comp.dataset.key;
    S.pending.comments.push({
      key: uid(),
      target: comp.dataset.target,
      thread: comp.dataset.thread || null,
      text,
      verdict: S.composing[key]?.verdict || null,
    });
    delete S.composing[key];
    saveDraft();
    return renderDynamic();
  }
  if (el.dataset.remove) {
    S.pending.comments = S.pending.comments.filter((c) => c.key !== el.dataset.remove);
    saveDraft();
    return renderDynamic();
  }
  if (el.dataset.openComposer) {
    const key = el.dataset.openComposer;
    S.composing[key] = { text: "", verdict: null };
    renderDynamic();
    const ta = document.querySelector(`.composer[data-key="${CSS.escape(key)}"] textarea`);
    if (ta) ta.focus();
    return;
  }
  if (el.dataset.reply) {
    const t = S.data.threads.find((x) => x.id === el.dataset.reply);
    const key = `${t.target}|${t.id}`;
    if (S.composing[key] === undefined) S.composing[key] = { text: "", verdict: null };
    renderDynamic();
    const ta = document.querySelector(`.composer[data-key="${CSS.escape(key)}"] textarea`);
    if (ta) ta.focus();
    return;
  }
  if (el.dataset.resolve) {
    toggleResolved(S.data.threads.find((x) => x.id === el.dataset.resolve));
    saveDraft();
    return renderDynamic();
  }
  if (el.id === "btn-revise") return submit("revise");
  if (el.id === "btn-approve") return submit("approve");
  if (el.id === "btn-terminal") return submit("terminal");
});

document.addEventListener("input", (e) => {
  const comp = e.target.closest(".composer");
  if (comp) {
    const key = comp.dataset.key;
    S.composing[key] = { ...(S.composing[key] || { verdict: null }), text: e.target.value };
    comp.classList.toggle("has-text", Boolean(e.target.value));
    if (comp.classList.contains("compact")) {
      e.target.style.height = "auto";
      e.target.style.height = `${e.target.scrollHeight + 2}px`;
    }
    saveDraft();
  } else if (e.target.id === "general-input") {
    S.general = e.target.value;
    saveDraft();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && S.openTask) closeDrawer();
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing && e.target.matches?.(".composer textarea")) {
    e.preventDefault();
    const comp = e.target.closest(".composer");
    comp.querySelector("[data-add]").click();
    // The box was re-rendered; keep typing in its replacement.
    const again = document.querySelector(`.composer[data-key="${CSS.escape(comp.dataset.key)}"] textarea`);
    if (again) again.focus();
  }
});
$("#scrim").addEventListener("click", closeDrawer);

/* ---------- Submit ---------- */

function unsentComposerText() {
  return Object.values(S.composing).filter((c) => c && c.text && c.text.trim()).length;
}

async function submit(decision) {
  const hasFeedback = S.pending.comments.length || S.general.trim() || S.pending.resolve.length || S.pending.reopen.length;
  const typed = unsentComposerText();
  if (decision !== "terminal" && typed && !confirm(`${typed} comment box${typed > 1 ? "es have" : " has"} text you didn't add. Continue without it?`)) return;
  if (decision === "revise" && !hasFeedback && !confirm("Send without any comments?")) return;
  if (decision === "approve") {
    const blockers = S.data.threads.filter((t) => t.verdict === "blocker" && !isResolved(t)).length + S.pending.comments.filter((c) => c.verdict === "blocker").length;
    if (blockers && !confirm(`${blockers} blocker${blockers > 1 ? "s are" : " is"} still open. Approve anyway?`)) return;
  }
  if (decision === "terminal" && !confirm("Close this review and use the approval prompt in Claude Code instead?")) return;

  const body = {
    decision,
    general: S.general,
    comments: S.pending.comments.map(({ target, thread, text, verdict }) => ({ target, thread, text, verdict })),
    resolve: S.pending.resolve,
    reopen: S.pending.reopen,
  };
  document.querySelectorAll(".bar button").forEach((b) => (b.disabled = true));
  try {
    await post("api/submit", body);
  } catch (err) {
    document.querySelectorAll(".bar button").forEach((b) => (b.disabled = false));
    alert(`Could not send: ${err.message}. Is Claude Code still waiting? The review may have timed out.`);
    return;
  }
  S.sent = true;
  clearInterval(heartbeat);
  const done = {
    revise: ["Feedback sent", "Claude is revising the plan."],
    approve: ["Approved", "Claude is implementing."],
    terminal: ["Handed back", "Approve in the terminal instead."],
  }[decision];
  $("#done-icon").textContent = done[0];
  $("#done-title").textContent = done[1];
  $("#done").hidden = false;
}

/* ---------- Boot ---------- */

let heartbeat = null;

async function boot() {
  const res = await fetch("api/plan");
  S.data = await res.json();
  const draft = S.data.draft;
  if (draft) {
    const ids = new Set(S.data.threads.map((t) => t.id));
    S.pending = {
      comments: (draft.pending?.comments || []).filter((c) => !c.thread || ids.has(c.thread)),
      resolve: (draft.pending?.resolve || []).filter((id) => ids.has(id)),
      reopen: (draft.pending?.reopen || []).filter((id) => ids.has(id)),
    };
    S.composing = draft.composing || {};
    S.general = draft.general || "";
    $("#general-input").value = S.general;
  }
  S.known = new Set([
    ...S.data.tasks.map((t) => t.id),
    ...(S.data.context ? ["context"] : []),
    ...S.data.sections.flatMap((s) => [s.slug, ...s.items.map((i) => i.id)]),
  ]);
  renderStatic();
  renderDynamic();
  const linked = decodeURIComponent(location.hash.slice(1));
  if (S.data.tasks.some((t) => t.id === linked)) openDrawer(linked);
  heartbeat = setInterval(() => post("api/heartbeat", {}).catch(() => {}), 10000);
}

boot().catch((err) => {
  $("#title").textContent = "Could not load the plan";
  $("#main").insertAdjacentHTML("afterbegin", `<p class="muted">${esc(err.message)}</p>`);
});
