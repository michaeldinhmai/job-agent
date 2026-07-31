const STATUSES = ["new", "shortlist", "applied", "ignored", "rejected"];

// ---------- tabs ----------
document.querySelectorAll(".tab-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById(btn.dataset.tab + "-tab").classList.add("active");
  });
});

function escapeHtml(s) {
  // Escapes quotes too, not just <>& — several call sites use this inside
  // attribute values (title="...", value="...") where an unescaped quote
  // would break out of the attribute.
  return (s ?? "").toString()
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function safeHref(url) {
  // Only allow http(s) links as hrefs — blocks javascript:/data: URI XSS
  // even after HTML-escaping, since an escaped javascript: URI still runs.
  try {
    const u = new URL(url, window.location.origin);
    if (u.protocol === "http:" || u.protocol === "https:") return escapeHtml(url);
  } catch (e) { /* invalid URL */ }
  return "#";
}

function closeModal() {
  document.getElementById("modal-backdrop").classList.add("hidden");
  document.getElementById("modal").innerHTML = "";
}
document.getElementById("modal-backdrop").addEventListener("click", e => {
  if (e.target.id === "modal-backdrop") closeModal();
});
function openModal(html) {
  document.getElementById("modal").innerHTML = html;
  document.getElementById("modal-backdrop").classList.remove("hidden");
  document.querySelectorAll(".close-x").forEach(b => b.addEventListener("click", closeModal));
}

function fillSelect(select, values, placeholder) {
  const current = select.value;
  select.innerHTML = `<option value="">${placeholder}</option>` +
    values.map(v => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
  select.value = current;
}

// ---------- summary ----------
async function loadSummary() {
  const r = await fetch("/api/status");
  const s = await r.json();
  const el = document.getElementById("summary");
  const parts = STATUSES.map(st => `<span><b>${s.counts[st] ?? 0}</b> ${st}</span>`);
  parts.push(`<span><b>${s.total}</b> total</span>`);
  parts.push(`<span><b>${s.contacts}</b> contacts</span>`);
  el.innerHTML = parts.join("");
}

// ---------- filter options ----------
async function loadFilterOptions() {
  const j = await (await fetch("/api/jobs/filter-options")).json();
  fillSelect(document.getElementById("job-company-filter"), j.company, "All companies");
  fillSelect(document.getElementById("job-state-filter"), j.state, "All states");
  fillSelect(document.getElementById("job-source-filter"), j.source, "All sources");

  const c = await (await fetch("/api/contacts/filter-options")).json();
  fillSelect(document.getElementById("contact-channel-filter"), c.channel, "All channels");
}

// ---------- jobs ----------
async function loadJobs() {
  const q = document.getElementById("job-search").value.trim();
  const status = document.getElementById("job-status-filter").value;
  const company = document.getElementById("job-company-filter").value;
  const state = document.getElementById("job-state-filter").value;
  const source = document.getElementById("job-source-filter").value;
  const minScore = document.getElementById("job-min-score").value;
  const hasHm = document.getElementById("job-has-hm").checked;
  const params = new URLSearchParams();
  if (q) params.set("q", q);
  if (status) params.set("status", status);
  if (company) params.set("company", company);
  if (state) params.set("state", state);
  if (source) params.set("source", source);
  if (minScore) params.set("min_score", minScore);
  if (hasHm) params.set("has_hiring_manager", "true");
  document.getElementById("job-export-link").href = "/api/export.csv?" + params.toString();
  const r = await fetch("/api/jobs?" + params.toString());
  const rows = await r.json();
  const tbody = document.getElementById("jobs-tbody");
  tbody.innerHTML = rows.map(j => {
    const cityState = [j.city, j.state].filter(Boolean).join(", ");
    const where = cityState || j.remote_label || "—";
    return `
    <tr data-id="${j.id}">
      <td title="raw score: ${j.score}">${j.score_pct}/100</td>
      <td class="truncate" title="${escapeHtml(j.title)}">${escapeHtml(j.title)}</td>
      <td>${escapeHtml(j.company)}</td>
      <td class="truncate" title="${escapeHtml(j.location)}">${escapeHtml(where)}</td>
      <td>
        <select class="status-select" data-id="${j.id}">
          ${STATUSES.map(s => `<option value="${s}" ${s === j.status ? "selected" : ""}>${s}</option>`).join("")}
        </select>
      </td>
      <td>${escapeHtml(j.hiring_manager) || "—"}</td>
      <td>${escapeHtml(j.source)}</td>
      <td><a href="${safeHref(j.url)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">open ↗</a></td>
    </tr>
  `;
  }).join("") || `<tr><td colspan="8">No matches for this filter.</td></tr>`;

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", e => {
      if (e.target.tagName === "SELECT" || e.target.tagName === "A") return;
      showJobDetail(tr.dataset.id);
    });
  });
  tbody.querySelectorAll(".status-select").forEach(sel => {
    sel.addEventListener("click", e => e.stopPropagation());
    sel.addEventListener("change", async () => {
      await fetch(`/api/jobs/${sel.dataset.id}/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ status: sel.value }),
      });
      loadSummary();
    });
  });
}

async function showJobDetail(id) {
  const r = await fetch(`/api/jobs/${id}`);
  const j = await r.json();
  openModal(`
    <button class="close-x">&times;</button>
    <h2>${escapeHtml(j.title)}</h2>
    <div class="field"><label>Company</label><div>${escapeHtml(j.company)}</div></div>
    <div class="field"><label>Location (as listed)</label><div>${escapeHtml(j.location)}</div></div>
    <div class="field"><label>Location (city / state / country)</label>
      <div>${escapeHtml(j.city) || "—"} / ${escapeHtml(j.state) || "—"} / ${escapeHtml(j.country) || "—"}</div>
    </div>
    <div class="field"><label>Match % / status / source</label>
      <div>${j.score_pct}/100 (raw ${j.score}) · <span class="badge ${j.status}">${j.status}</span> · ${escapeHtml(j.source)}</div>
    </div>
    <div class="field"><label>Why it matched</label><div class="reasons">${escapeHtml(j.reasons)}</div></div>
    <div class="field"><label>Hiring manager</label>
      <input id="edit-hm" value="${escapeHtml(j.hiring_manager)}" placeholder="Name, once you find one">
    </div>
    <div class="field"><label>Resume used</label><div>${escapeHtml(j.resume_path) || "(none tailored yet)"}</div></div>
    <div class="field"><label>Description</label><pre class="desc">${escapeHtml(j.description)}</pre></div>
    <div id="job-tool-result" class="field"></div>
    <div class="actions">
      <a href="${safeHref(j.url)}" target="_blank" rel="noopener"><button>Open listing ↗</button></a>
      <button id="run-tailor">Tailor gap-report</button>
      <button id="run-findhm">Find hiring manager</button>
      <button id="run-apply">Pre-fill application</button>
      <button class="primary" id="save-job-hm">Save hiring manager</button>
      <button class="danger" id="delete-job">Delete</button>
      <button class="close-x">Close</button>
    </div>
  `);

  const resultBox = document.getElementById("job-tool-result");

  document.getElementById("save-job-hm").addEventListener("click", async () => {
    await fetch(`/api/jobs/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ hiring_manager: document.getElementById("edit-hm").value.trim() }),
    });
    closeModal();
    loadJobs();
    loadFilterOptions();
  });

  document.getElementById("delete-job").addEventListener("click", async () => {
    if (!confirm(`Permanently delete "${j.title}" @ ${j.company}? This can't be undone.`)) return;
    await fetch(`/api/jobs/${id}`, { method: "DELETE" });
    closeModal();
    loadJobs();
    loadSummary();
  });

  document.getElementById("run-tailor").addEventListener("click", async () => {
    resultBox.innerHTML = "<label>Tailor gap-report</label><div>running…</div>";
    const res = await fetch(`/api/jobs/${id}/tailor`, { method: "POST" });
    const data = await res.json();
    if (!res.ok) {
      resultBox.innerHTML = `<label>Tailor gap-report</label><div class="reasons">${escapeHtml(data.error)}</div>`;
      return;
    }
    resultBox.innerHTML = `<label>Tailor gap-report</label>
      <div class="reasons">JD emphasises ${data.jd_terms} distinct terms; your resume already shows
      ${Math.round(data.coverage * 100)}% of them.</div>`;
  });

  document.getElementById("run-findhm").addEventListener("click", async () => {
    resultBox.innerHTML = "<label>Hiring-manager search</label><div>building…</div>";
    const data = await (await fetch(`/api/jobs/${id}/find-hm`)).json();
    const hits = (data.report_line_hits || []).map(h => `<div>- ${escapeHtml(h)}</div>`).join("");
    resultBox.innerHTML = `
      <label>Hiring-manager search (role family: ${escapeHtml(data.role_family)})</label>
      <div class="reasons">
        <div><b>X-ray (ask Claude to run this):</b> ${escapeHtml(data.x_ray_query)}</div>
        <div><b>LinkedIn Jobs (paste yourself):</b> ${escapeHtml(data.linkedin_jobs_query)}</div>
        <div><b>LinkedIn Posts (paste yourself):</b> ${escapeHtml(data.linkedin_posts_query)}</div>
        ${hits ? `<div><b>Reporting-line mentions in JD:</b></div>${hits}` : ""}
      </div>`;
  });

  document.getElementById("run-apply").addEventListener("click", async () => {
    if (!confirm(`Launch the application pre-fill for "${j.title}" @ ${j.company}?\n\n`
      + "This opens a real browser window and fills your real profile info into "
      + "the employer's actual application form. It stops before submitting — "
      + "you review and click submit yourself.")) return;
    resultBox.innerHTML = "<label>Apply</label><div>launching…</div>";
    const data = await (await fetch(`/api/jobs/${id}/apply`, { method: "POST" })).json();
    resultBox.innerHTML = `<label>Apply</label><div class="reasons">${escapeHtml(data.message || data.error)}</div>`;
  });
}

function showAddJobForm() {
  openModal(`
    <button class="close-x">&times;</button>
    <h2>Add a listing manually</h2>
    <div class="field"><label>Title *</label><input id="new-job-title"></div>
    <div class="field"><label>Company</label><input id="new-job-company"></div>
    <div class="field"><label>Location (as written on the posting)</label><input id="new-job-location"></div>
    <div class="field"><label>URL *</label><input id="new-job-url" placeholder="https://..."></div>
    <div class="field"><label>Description (paste the JD for scoring/tailoring)</label>
      <textarea id="new-job-description" style="min-height:8em"></textarea>
    </div>
    <div class="actions">
      <button class="close-x">Cancel</button>
      <button class="primary" id="save-new-job">Add listing</button>
    </div>
  `);
  document.getElementById("save-new-job").addEventListener("click", async () => {
    const title = document.getElementById("new-job-title").value.trim();
    const url = document.getElementById("new-job-url").value.trim();
    if (!title || !url) { alert("Title and URL are required."); return; }
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        url,
        company: document.getElementById("new-job-company").value.trim(),
        location: document.getElementById("new-job-location").value.trim(),
        description: document.getElementById("new-job-description").value.trim(),
      }),
    });
    const data = await res.json();
    if (!res.ok) { alert(data.error || "Could not add listing."); return; }
    closeModal();
    loadJobs();
    loadSummary();
    loadFilterOptions();
  });
}

document.getElementById("job-refresh").addEventListener("click", loadJobs);
document.getElementById("job-search").addEventListener("keydown", e => { if (e.key === "Enter") loadJobs(); });
document.getElementById("job-status-filter").addEventListener("change", loadJobs);
document.getElementById("job-company-filter").addEventListener("change", loadJobs);
document.getElementById("job-state-filter").addEventListener("change", loadJobs);
document.getElementById("job-source-filter").addEventListener("change", loadJobs);
document.getElementById("job-has-hm").addEventListener("change", loadJobs);
document.getElementById("job-min-score").addEventListener("keydown", e => { if (e.key === "Enter") loadJobs(); });
document.getElementById("job-add-btn").addEventListener("click", showAddJobForm);

// ---------- contacts ----------
async function loadContacts() {
  const company = document.getElementById("contact-search").value.trim();
  const channel = document.getElementById("contact-channel-filter").value;
  const params = new URLSearchParams();
  if (company) params.set("company", company);
  if (channel) params.set("channel", channel);
  const r = await fetch("/api/contacts?" + params.toString());
  const rows = await r.json();
  const tbody = document.getElementById("contacts-tbody");
  tbody.innerHTML = rows.map(c => `
    <tr data-id="${c.id}">
      <td>${escapeHtml(c.contacted_at)}</td>
      <td>${escapeHtml(c.name)}</td>
      <td>${escapeHtml(c.title) || "—"}</td>
      <td>${escapeHtml(c.company)}</td>
      <td>${escapeHtml(c.channel) || "—"}</td>
      <td class="truncate" title="${escapeHtml(c.outcome)}">${escapeHtml(c.outcome) || "—"}</td>
      <td class="truncate" title="${escapeHtml(c.follow_up)}">${escapeHtml(c.follow_up) || "—"}</td>
      <td>${c.listing_id ? `#${c.listing_id}` : ""}</td>
    </tr>
  `).join("") || `<tr><td colspan="8">No contacts logged yet.</td></tr>`;

  tbody.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => showContactDetail(tr.dataset.id));
  });
}

async function showContactDetail(id) {
  const r = await fetch(`/api/contacts/${id}`);
  const c = await r.json();
  openModal(`
    <button class="close-x">&times;</button>
    <h2>${escapeHtml(c.name)} — ${escapeHtml(c.company)}</h2>
    <div class="field"><label>Title / channel / date</label>
      <div>${escapeHtml(c.title) || "—"} · ${escapeHtml(c.channel) || "—"} · ${escapeHtml(c.contacted_at)}</div>
    </div>
    <div class="field"><label>Linked listing</label><div>${c.listing_id ? `#${c.listing_id}` : "none"}</div></div>
    <div class="field"><label>Outcome</label><textarea id="edit-outcome">${escapeHtml(c.outcome)}</textarea></div>
    <div class="field"><label>Follow-up</label><textarea id="edit-followup">${escapeHtml(c.follow_up)}</textarea></div>
    <div class="actions">
      <button class="close-x">Cancel</button>
      <button class="danger" id="delete-contact">Delete</button>
      <button class="primary" id="save-contact">Save changes</button>
    </div>
  `);
  document.getElementById("save-contact").addEventListener("click", async () => {
    await fetch(`/api/contacts/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        outcome: document.getElementById("edit-outcome").value,
        follow_up: document.getElementById("edit-followup").value,
      }),
    });
    closeModal();
    loadContacts();
  });
  document.getElementById("delete-contact").addEventListener("click", async () => {
    if (!confirm(`Permanently delete contact "${c.name}" @ ${c.company}? This can't be undone.`)) return;
    await fetch(`/api/contacts/${id}`, { method: "DELETE" });
    closeModal();
    loadContacts();
    loadSummary();
    loadFilterOptions();
  });
}

function showAddContactForm() {
  openModal(`
    <button class="close-x">&times;</button>
    <h2>Add contact</h2>
    <div class="field"><label>Company *</label><input id="new-company"></div>
    <div class="field"><label>Name *</label><input id="new-name"></div>
    <div class="field"><label>Title</label><input id="new-title"></div>
    <div class="field"><label>Channel</label><input id="new-channel" placeholder="LinkedIn, Slack, email..."></div>
    <div class="field"><label>Date</label><input id="new-date" type="date"></div>
    <div class="field"><label>Linked listing id</label><input id="new-listing" type="number"></div>
    <div class="field"><label>Outcome</label><textarea id="new-outcome"></textarea></div>
    <div class="field"><label>Follow-up</label><textarea id="new-followup"></textarea></div>
    <div class="actions">
      <button class="close-x">Cancel</button>
      <button class="primary" id="save-new-contact">Add contact</button>
    </div>
  `);
  document.getElementById("save-new-contact").addEventListener("click", async () => {
    const company = document.getElementById("new-company").value.trim();
    const name = document.getElementById("new-name").value.trim();
    if (!company || !name) { alert("Company and name are required."); return; }
    await fetch("/api/contacts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        company, name,
        title: document.getElementById("new-title").value.trim(),
        channel: document.getElementById("new-channel").value.trim(),
        contacted_at: document.getElementById("new-date").value,
        listing_id: document.getElementById("new-listing").value || null,
        outcome: document.getElementById("new-outcome").value.trim(),
        follow_up: document.getElementById("new-followup").value.trim(),
      }),
    });
    closeModal();
    loadContacts();
    loadSummary();
    loadFilterOptions();
  });
}

document.getElementById("contact-refresh").addEventListener("click", loadContacts);
document.getElementById("contact-search").addEventListener("keydown", e => { if (e.key === "Enter") loadContacts(); });
document.getElementById("contact-channel-filter").addEventListener("change", loadContacts);
document.getElementById("contact-add-btn").addEventListener("click", showAddContactForm);

// ---------- actions bar ----------
function showActionOutput(text, kind) {
  const el = document.getElementById("action-output");
  el.textContent = text;
  el.className = "action-output" + (kind ? " " + kind : "");
}

async function runAction(name, endpoint, method) {
  showActionOutput(`running ${name}…`, "");
  const res = await fetch(endpoint, { method: method || "POST" });
  const data = await res.json();
  if (data.ok === false) {
    showActionOutput(`${name} failed: ${(data.stderr || "").split("\n").pop() || "see terminal"}`, "error");
  } else {
    const lastLine = (data.stdout || "").trim().split("\n").pop() || `${name} done`;
    showActionOutput(lastLine, "success");
  }
  loadSummary();
  loadJobs();
  loadFilterOptions();
  return data;
}

async function loadScheduleStatus() {
  const data = await (await fetch("/api/actions/schedule")).json();
  const el = document.getElementById("schedule-status");
  el.textContent = "schedule: " + ((data.stdout || "").trim() || (data.ok ? "unknown" : "error"));
}

document.getElementById("action-ingest").addEventListener("click", () => runAction("ingest", "/api/actions/ingest"));
document.getElementById("action-rescore").addEventListener("click", () => runAction("rescore", "/api/actions/rescore"));
document.getElementById("action-digest").addEventListener("click", () => runAction("digest", "/api/actions/digest"));
document.getElementById("action-schedule-on").addEventListener("click", async () => {
  showActionOutput("turning schedule on…", "");
  await fetch("/api/actions/schedule", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state: "on" }),
  });
  showActionOutput("schedule turned on", "success");
  loadScheduleStatus();
});
document.getElementById("action-schedule-off").addEventListener("click", async () => {
  showActionOutput("turning schedule off…", "");
  await fetch("/api/actions/schedule", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state: "off" }),
  });
  showActionOutput("schedule turned off", "success");
  loadScheduleStatus();
});

// ---------- init ----------
loadSummary();
loadJobs();
loadContacts();
loadFilterOptions();
loadScheduleStatus();
