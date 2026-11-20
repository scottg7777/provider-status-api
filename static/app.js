(() => {
  "use strict";

  const HISTORY_KEY = "chs-provider-status-history-v1";
  const HISTORY_LIMIT = 25;
  const TX_LABELS = {
    eligibilityCheck: "270/271 Eligibility",
    claimStatus: "276/277 Claim Status",
    professionalClaimSubmission: "837P Professional Claims",
    institutionalClaimSubmission: "837I Institutional Claims",
    dentalClaimSubmission: "837D Dental Claims",
    claimPayment: "835 ERA",
    coordinationOfBenefits: "Coordination of Benefits",
    unsolicitedClaimAttachment: "275 Claim Attachments",
    claimAttachment: "Claim Attachments",
  };

  const state = {
    lastLookup: null,
    batchResults: [],
    payersCatalog: null,
  };

  const $ = (id) => document.getElementById(id);

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function text(value, fallback = "—") {
    if (value === null || value === undefined || value === "") return fallback;
    return escapeHtml(value);
  }

  function badge(label, kind) {
    return `<span class="badge badge-${kind}">${escapeHtml(label)}</span>`;
  }

  function supportBadge(value) {
    const raw = normalizeSupport(value);
    if (raw === "SUPPORTED") return badge("Supported", "good");
    if (raw === "ENROLLMENT_REQUIRED") return badge("Enrollment required", "warn");
    if (raw === "NOT_SUPPORTED") return badge("Not supported", "muted");
    return badge(raw || "Unknown", "muted");
  }

  function normalizeSupport(value) {
    if (value == null || value === "") return "";
    if (typeof value === "string") return value.toUpperCase();
    if (typeof value === "object") {
      const inner = value.status || value.support || value.value || value.code;
      if (typeof inner === "string") return inner.toUpperCase();
    }
    return String(value).toUpperCase();
  }

  function enrollmentBadge(status) {
    const key = String(status || "").toUpperCase();
    const kind = {
      LIVE: "good",
      PROVISIONING: "info",
      SUBMITTED: "info",
      DRAFT: "muted",
      STEDI_ACTION_REQUIRED: "warn",
      PROVIDER_ACTION_REQUIRED: "warn",
      REJECTED: "bad",
      CANCELED: "muted",
      CANCELLED: "muted",
    }[key] || "muted";
    return badge(key || "Unknown", kind);
  }

  function boolBadge(value, yes = "Yes", no = "No") {
    if (value === true) return badge(yes, "good");
    if (value === false) return badge(no, "muted");
    return badge("Unknown", "muted");
  }

  function formatAddress(addr) {
    if (!addr) return "";
    const lines = [
      [addr.address_1, addr.address_2].filter(Boolean).join(" "),
      [addr.city, addr.state, addr.postal_code].filter(Boolean).join(", "),
      addr.telephone ? `Tel ${addr.telephone}` : "",
    ].filter(Boolean);
    return lines.map(escapeHtml).join("<br>");
  }

  function formatFhirAddress(addr) {
    if (!addr) return "";
    const lines = [
      ...(addr.lines || []),
      addr.text,
      [addr.city, addr.state, addr.postal_code].filter(Boolean).join(", "),
    ].filter(Boolean);
    return escapeHtml(lines.join(", "));
  }

  function showStatus(el, message, kind) {
    if (!el) return;
    el.hidden = !message;
    el.classList.remove("is-error", "is-ok");
    if (kind) el.classList.add(kind);
    el.textContent = message || "";
  }

  async function fetchJson(url, options) {
    const response = await fetch(url, options);
    const raw = await response.text();
    let data = null;
    try { data = raw ? JSON.parse(raw) : null; } catch { data = null; }
    if (!response.ok) {
      const detail = data && (data.detail || data.message);
      const message = Array.isArray(detail)
        ? detail.map((d) => d.msg || d).join("; ")
        : (detail || raw || `Request failed (${response.status})`);
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  async function refreshHealth() {
    const el = $("health-badge");
    try {
      const data = await fetchJson("/health");
      el.classList.remove("is-loading", "is-connected", "is-offline", "is-error");
      if (data.stedi_configured) {
        el.classList.add("is-connected");
        el.querySelector(".health-label").textContent = "Stedi Connected";
      } else {
        el.classList.add("is-offline");
        el.querySelector(".health-label").textContent = "Stedi Not Configured";
      }
    } catch {
      el.classList.remove("is-loading", "is-connected", "is-offline");
      el.classList.add("is-error");
      el.querySelector(".health-label").textContent = "API unreachable";
    }
  }

  async function loadPayerOptions() {
    const catalog = await fetchJson("/payers");
    state.payersCatalog = catalog;
    const list = $("payer-list");
    const values = new Set(["medicare", "aetna"]);
    (catalog.fhir_payers || []).forEach((p) => {
      values.add(p.id);
      (p.aliases || []).forEach((a) => values.add(a));
    });
    (catalog.unsupported_or_auth_required || []).forEach((p) => {
      values.add(p.id);
      (p.aliases || []).forEach((a) => values.add(a));
    });
    list.innerHTML = [...values]
      .sort((a, b) => a.localeCompare(b))
      .map((v) => `<option value="${escapeHtml(v)}"></option>`)
      .join("");
    return catalog;
  }

  function kpiStrip(data) {
    const summary = data.summary || {};
    const stedi = data.stedi_edi_enrollment || {};
    const nppes = data.provider || {};
    const pecos = data.medicare_pecos || {};
    const nppesValue = nppes.found ? (nppes.active ? "Active" : "Inactive") : "Not found";
    const pecosValue = pecos.enrolled === true ? "Enrolled" : pecos.enrolled === false ? "Not enrolled" : "Unavailable";
    const networks = (summary.in_network_payers || []).join(", ") || "None found";
    const stediValue = !stedi.configured
      ? "Not configured"
      : stedi.error
        ? "Error"
        : (stedi.enrollment_count ? `${stedi.enrollment_count} enrollment(s)` : "No enrollments");
    return `
      <div class="kpi-strip">
        <div class="kpi"><span class="label">NPPES Status</span><span class="value">${text(nppesValue)}</span><span class="hint">${text(nppes.enumeration_type || "")}</span></div>
        <div class="kpi"><span class="label">Medicare PECOS</span><span class="value">${text(pecosValue)}</span><span class="hint">${text((pecos.states || []).join(", "))}</span></div>
        <div class="kpi"><span class="label">FHIR In-Network</span><span class="value">${text((summary.in_network_payers || []).length)}</span><span class="hint">${text(networks)}</span></div>
        <div class="kpi"><span class="label">Stedi EDI Status</span><span class="value">${text(stediValue)}</span><span class="hint">${text(summary.stedi || "")}</span></div>
      </div>`;
  }

  function renderProviderCard(provider) {
    const tax = provider.primary_taxonomy || {};
    const practice = (provider.practice_addresses || []).map((a) =>
      `<div class="address"><strong>${text(a.purpose || "Practice")}</strong>${formatAddress(a) || "—"}</div>`
    ).join("");
    const mailing = (provider.mailing_addresses || []).map((a) =>
      `<div class="address"><strong>Mailing</strong>${formatAddress(a) || "—"}</div>`
    ).join("");
    return `
      <section class="card">
        <div class="card-head"><h2>Provider profile</h2><div class="meta-row">
          ${provider.found ? badge(provider.enumeration_type || "NPI", "navy") : badge("Not found", "muted")}
          ${provider.found ? boolBadge(provider.active, "Active", "Inactive") : ""}
        </div></div>
        <dl class="dl-grid">
          <div><dt>Name</dt><dd>${text(provider.name)}</dd></div>
          <div><dt>NPI</dt><dd>${text(provider.npi)}</dd></div>
          <div><dt>Primary taxonomy</dt><dd>${text(tax.description || tax.code)}${tax.license ? ` · License ${text(tax.license)}${tax.state ? ` (${text(tax.state)})` : ""}` : ""}</dd></div>
          <div><dt>Organization</dt><dd>${text(provider.organization_name)}</dd></div>
        </dl>
        <div class="address-list">${practice || ""}${mailing || ""}</div>
        ${provider.error ? `<div class="callout">${text(provider.error)}</div>` : ""}
      </section>`;
  }

  function renderPecosCard(pecos) {
    const rows = (pecos.enrollments || []).map((row) => `
      <tr>
        <td>${text(row.enrollment_id)}</td>
        <td>${text(row.provider_type)}</td>
        <td>${text(row.state)}</td>
        <td>${text(row.pac_id)}</td>
      </tr>`).join("");
    return `
      <section class="card">
        <div class="card-head">
          <h2>Medicare PECOS</h2>
          <div class="meta-row">
            ${pecos.enrolled ? badge("Enrolled", "good") : pecos.enrolled === false ? badge("Not enrolled", "muted") : badge("Unavailable", "warn")}
          </div>
        </div>
        <dl class="dl-grid">
          <div><dt>PAC ID</dt><dd>${text((pecos.enrollments || [])[0]?.pac_id)}</dd></div>
          <div><dt>States</dt><dd>${text((pecos.states || []).join(", "))}</dd></div>
          <div><dt>Provider types</dt><dd>${text((pecos.provider_types || []).join("; "))}</dd></div>
          <div><dt>Enrollment records</dt><dd>${text(pecos.enrollment_count ?? 0)}</dd></div>
        </dl>
        ${rows ? `<div class="table-wrap" style="margin-top:12px"><table><thead><tr><th>Enrollment ID</th><th>Type</th><th>State</th><th>PAC ID</th></tr></thead><tbody>${rows}</tbody></table></div>` : ""}
        ${pecos.error ? `<div class="callout">${text(pecos.error)}</div>` : ""}
      </section>`;
  }

  function renderNetworkCards(network) {
    const results = network.results || [];
    const skipped = network.skipped || [];
    const cards = results.map((row) => {
      const found = row.found || row.in_network;
      const networks = (row.networks || []).map((n) => `<li>${text(n.name || n.id)}</li>`).join("");
      const orgs = (row.organizations || []).map((n) => `<li>${text(n.name || n.id)}</li>`).join("");
      const locs = (row.locations || []).map((n) =>
        `<li>${text(n.name || n.id)}${n.address ? ` — ${formatFhirAddress(n.address)}` : ""}</li>`
      ).join("");
      return `
        <article class="card network-card">
          <div class="card-head">
            <h3>${text(row.payer_name || row.payer_id)}</h3>
            <div class="meta-row">
              ${found ? badge("In-network", "good") : badge("Not found", "muted")}
              ${row.active === true ? badge("Active", "good") : row.active === false ? badge("Inactive", "warn") : ""}
            </div>
          </div>
          <p class="muted">${text(row.practitioner_name)} · ${text(row.roles_count || 0)} role(s)</p>
          ${row.notes && !found ? `<div class="callout">${text(row.notes)}</div>` : ""}
          ${row.error ? `<div class="callout">${text(row.error)}</div>` : ""}
          ${networks ? `<details open><summary>Networks / plans (${row.networks.length})</summary><ul class="pill-list">${networks}</ul></details>` : ""}
          ${orgs ? `<details><summary>Organizations (${row.organizations.length})</summary><ul class="pill-list">${orgs}</ul></details>` : ""}
          ${locs ? `<details><summary>Practice locations (${row.locations.length})</summary><ul class="pill-list">${locs}</ul></details>` : ""}
        </article>`;
    }).join("");
    const skippedCards = skipped.map((row) => `
      <article class="card network-card">
        <div class="card-head"><h3>${text(row.name || row.payer)}</h3>${badge("Manual / OAuth", "warn")}</div>
        <div class="callout">${text(row.reason || "This payer requires registration or a manual directory check.")}</div>
      </article>`).join("");
    return `
      <section>
        <div class="card-head" style="margin: 8px 2px 10px"><h2>Commercial network status</h2></div>
        <div class="network-grid">${cards || skippedCards || `<div class="card empty">No commercial Plan-Net directories were queried.</div>`}${cards ? skippedCards : ""}</div>
      </section>`;
  }

  function transactionRows(support) {
    const entries = Object.entries(support || {});
    if (!entries.length) return "";
    return entries.map(([code, value]) => `
      <tr>
        <td>${text(TX_LABELS[code] || code)}</td>
        <td>${supportBadge(value)}</td>
      </tr>`).join("");
  }

  function renderStediCard(stedi) {
    const matched = (stedi.matched_payers || []).map((payer) => `
      <div class="card" style="box-shadow:none;margin-top:12px">
        <div class="card-head">
          <div>
            <h3>${text(payer.name || payer.stedi_payer_id)}</h3>
            <p class="muted">Stedi ID ${text(payer.stedi_payer_id)} · Primary payer ID ${text(payer.primary_payer_id)}</p>
          </div>
          <div class="meta-row">
            ${payer.ptan_required === true ? badge("PTAN required", "warn") : payer.ptan_required === false ? badge("PTAN not required", "good") : badge("PTAN unknown", "muted")}
          </div>
        </div>
        ${Object.keys(payer.transaction_support || {}).length ? `
          <div class="table-wrap">
            <table class="matrix">
              <thead><tr><th>Transaction</th><th>Support</th></tr></thead>
              <tbody>${transactionRows(payer.transaction_support)}</tbody>
            </table>
          </div>` : `<p class="muted">No transaction support details returned for this payer.</p>`}
      </div>`).join("");

    const enrollRows = (stedi.enrollments || []).map((row) => {
      const tx = (row.transactions || []).map((t) => t.label || t.code).join(", ");
      const payerName = (row.payer || {}).name || (row.payer || {}).stedi_payer_id;
      return `<tr>
        <td>${text(payerName)}</td>
        <td>${enrollmentBadge(row.status)}</td>
        <td>${text(tx)}</td>
        <td>${text(row.status_last_updated_at || row.updated_at || row.submitted_at)}</td>
        <td>${text(row.id)}</td>
      </tr>`;
    }).join("");

    const tasks = (stedi.open_tasks || []).map((task) => `
      <li>
        <strong>${text(task.responsible_party || "Task")}</strong>
        ${task.enrollment_status ? enrollmentBadge(task.enrollment_status) : ""}
        — ${text(task.instructions || task.id)}
        ${task.payer_name ? `<span class="muted"> (${text(task.payer_name)})</span>` : ""}
      </li>`).join("");

    let banner = "";
    if (!stedi.configured) banner = `<div class="callout">${text(stedi.message || "Stedi is not configured.")}</div>`;
    else if (stedi.error) banner = `<div class="callout">${text(stedi.error)}</div>`;

    return `
      <section class="card">
        <div class="card-head">
          <h2>Stedi EDI enrollments &amp; transaction rules</h2>
          <div class="meta-row">
            ${stedi.configured ? badge("Connected", "good") : badge("Not configured", "warn")}
          </div>
        </div>
        ${banner}
        ${matched || (stedi.configured ? `<p class="muted">No Stedi payer match was returned. Enter a payer name such as Aetna to load transaction rules.</p>` : "")}
        <h3 style="margin:18px 0 8px">Active / pending enrollments</h3>
        ${enrollRows ? `<div class="table-wrap"><table><thead><tr><th>Payer</th><th>Status</th><th>Transactions</th><th>Updated</th><th>Enrollment ID</th></tr></thead><tbody>${enrollRows}</tbody></table></div>` : `<p class="muted">No EDI enrollments were found for this provider${stedi.configured ? "" : " (Stedi is not configured)"}.</p>`}
        <h3 style="margin:18px 0 8px">Open manual tasks</h3>
        ${tasks ? `<ul>${tasks}</ul>` : `<p class="muted">No open manual tasks.</p>`}
      </section>`;
  }

  function renderJsonTools(data, filename) {
    const json = JSON.stringify(data, null, 2);
    const id = `json-${Math.random().toString(36).slice(2, 8)}`;
    return `
      <section class="card">
        <div class="card-head"><h2>Raw JSON</h2></div>
        <div class="json-tools">
          <button type="button" class="btn btn-ghost btn-sm" data-toggle-json="${id}">Show JSON</button>
          <button type="button" class="btn btn-ghost btn-sm" data-copy-json>Copy JSON</button>
          <button type="button" class="btn btn-ghost btn-sm" data-download-json="${escapeHtml(filename || "provider-status.json")}">Download JSON</button>
        </div>
        <pre class="json-view" id="${id}" hidden></pre>
      </section>`;
  }

  function hydrateJson(container, data) {
    const json = JSON.stringify(data, null, 2);
    const pre = container.querySelector(".json-view");
    if (pre) pre.textContent = json;
    container.querySelector("[data-copy-json]")?.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(json);
        showStatus($("lookup-status") || $("batch-status"), "JSON copied to clipboard.", "is-ok");
      } catch {
        showStatus($("lookup-status") || $("batch-status"), "Unable to copy JSON in this browser.", "is-error");
      }
    });
    container.querySelector("[data-download-json]")?.addEventListener("click", (event) => {
      const name = event.currentTarget.getAttribute("data-download-json") || "provider-status.json";
      const blob = new Blob([json], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
    });
    container.querySelector("[data-toggle-json]")?.addEventListener("click", (event) => {
      if (!pre) return;
      const hidden = pre.hasAttribute("hidden");
      if (hidden) pre.removeAttribute("hidden");
      else pre.setAttribute("hidden", "");
      event.currentTarget.textContent = hidden ? "Hide JSON" : "Show JSON";
    });
  }

  function renderLookup(data) {
    return [
      kpiStrip(data),
      renderProviderCard(data.provider || {}),
      renderPecosCard(data.medicare_pecos || {}),
      renderNetworkCards(data.network_status || {}),
      renderStediCard(data.stedi_edi_enrollment || {}),
      renderJsonTools(data, `provider-status-${data.npi || "result"}.json`),
    ].join("");
  }

  function rememberLookup(data, payer) {
    const summary = data.summary || {};
    const entry = {
      ts: new Date().toISOString(),
      npi: data.npi,
      payer: payer || "",
      name: summary.provider_name || (data.provider || {}).name || "",
      nppes_active: summary.nppes_active,
      medicare_enrolled: summary.medicare_enrolled,
      in_network: summary.in_network_payers || [],
      stedi: summary.stedi || "",
    };
    const items = loadHistory().filter((row) => !(row.npi === entry.npi && row.payer === entry.payer));
    items.unshift(entry);
    localStorage.setItem(HISTORY_KEY, JSON.stringify(items.slice(0, HISTORY_LIMIT)));
    renderHistory();
  }

  function loadHistory() {
    try {
      const raw = localStorage.getItem(HISTORY_KEY);
      const items = raw ? JSON.parse(raw) : [];
      return Array.isArray(items) ? items : [];
    } catch {
      return [];
    }
  }

  function renderHistory() {
    const root = $("history-results");
    const items = loadHistory();
    if (!items.length) {
      root.innerHTML = `<p class="empty">No lookups saved in this browser yet.</p>`;
      return;
    }
    root.innerHTML = `
      <div class="table-wrap">
        <table>
          <thead><tr><th>When</th><th>NPI</th><th>Provider</th><th>Payer</th><th>Medicare</th><th>In-network</th><th>Stedi</th><th></th></tr></thead>
          <tbody>
            ${items.map((row, index) => `
              <tr>
                <td>${text(row.ts ? new Date(row.ts).toLocaleString() : "")}</td>
                <td>${text(row.npi)}</td>
                <td>${text(row.name)}</td>
                <td>${text(row.payer || "All")}</td>
                <td>${row.medicare_enrolled === true ? badge("Enrolled", "good") : row.medicare_enrolled === false ? badge("No", "muted") : "—"}</td>
                <td>${text((row.in_network || []).join(", ") || "—")}</td>
                <td>${text(row.stedi)}</td>
                <td><button type="button" class="btn btn-ghost btn-sm" data-history-index="${index}">Reload</button></td>
              </tr>`).join("")}
          </tbody>
        </table>
      </div>`;
    root.querySelectorAll("[data-history-index]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const row = items[Number(btn.getAttribute("data-history-index"))];
        if (!row) return;
        $("npi").value = row.npi || "";
        $("payer").value = row.payer || "";
        $("tin").value = "";
        switchTab("lookup");
        runLookup();
      });
    });
  }

  async function runLookup() {
    const npi = $("npi").value.trim();
    const payer = $("payer").value.trim();
    const tin = $("tin").value.trim();
    const submit = $("lookup-submit");
    const results = $("lookup-results");
    showStatus($("lookup-status"), "Looking up provider status…");
    submit.disabled = true;
    try {
      const params = new URLSearchParams({ npi });
      if (payer) params.set("payer", payer);
      if (tin) params.set("tin", tin);
      const data = await fetchJson(`/provider-status?${params.toString()}`);
      state.lastLookup = data;
      results.innerHTML = renderLookup(data);
      hydrateJson(results, data);
      rememberLookup(data, payer);
      showStatus($("lookup-status"), `Results for ${data.summary?.provider_name || npi}.`, "is-ok");
    } catch (error) {
      results.innerHTML = "";
      showStatus($("lookup-status"), error.message || "Lookup failed.", "is-error");
    } finally {
      submit.disabled = false;
    }
  }

  function parseBatchText(textValue) {
    const lines = textValue.split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
    const items = [];
    lines.forEach((line, index) => {
      const parts = line.split(/[,\t]/).map((p) => p.trim());
      if (index === 0 && parts[0] && parts[0].toLowerCase() === "npi") return;
      const npi = (parts[0] || "").replace(/\D/g, "");
      if (!npi) return;
      const item = { npi };
      if (parts[1]) item.payer = parts[1];
      if (parts[2]) item.tin = parts[2];
      items.push(item);
    });
    return items;
  }

  function updateBatchCount() {
    const items = parseBatchText($("batch-input").value);
    $("batch-count").textContent = `${items.length} row${items.length === 1 ? "" : "s"}`;
  }

  function renderBatchTable(results) {
    const root = $("batch-results");
    root.innerHTML = `
      <div class="card">
        <div class="card-head">
          <h2>Batch results</h2>
          <button type="button" class="btn btn-ghost btn-sm" id="batch-export">Export CSV</button>
        </div>
        <div class="filter-bar">
          <input id="batch-filter" type="search" placeholder="Filter by NPI, name, payer, or status">
        </div>
        <div class="table-wrap">
          <table>
            <thead><tr><th>NPI</th><th>Provider</th><th>NPPES</th><th>Medicare PECOS</th><th>In-network payers</th><th>Stedi status</th><th></th></tr></thead>
            <tbody id="batch-table-body"></tbody>
          </table>
        </div>
      </div>`;
    const body = $("batch-table-body");
    const paint = (filter) => {
      const q = (filter || "").toLowerCase();
      body.innerHTML = results.map((data, index) => {
        const s = data.summary || {};
        const hay = [data.npi, s.provider_name, (s.in_network_payers || []).join(" "), s.stedi, s.medicare_enrolled].join(" ").toLowerCase();
        if (q && !hay.includes(q)) return "";
        return `<tr>
          <td>${text(data.npi)}</td>
          <td>${text(s.provider_name)}</td>
          <td>${s.nppes_active === true ? badge("Active", "good") : s.nppes_found ? badge("Inactive", "warn") : badge("Not found", "muted")}</td>
          <td>${s.medicare_enrolled === true ? badge("Enrolled", "good") : s.medicare_enrolled === false ? badge("Not enrolled", "muted") : badge("Unknown", "warn")}</td>
          <td>${text((s.in_network_payers || []).join(", ") || "—")}</td>
          <td>${text(s.stedi)}</td>
          <td><button type="button" class="btn btn-ghost btn-sm" data-batch-index="${index}">View details</button></td>
        </tr>`;
      }).join("") || `<tr><td colspan="7" class="empty">No rows match that filter.</td></tr>`;
      body.querySelectorAll("[data-batch-index]").forEach((btn) => {
        btn.addEventListener("click", () => openDrawer(results[Number(btn.getAttribute("data-batch-index"))]));
      });
    };
    paint("");
    $("batch-filter").addEventListener("input", (e) => paint(e.target.value));
    $("batch-export").addEventListener("click", () => exportBatchCsv(results));
  }

  function exportBatchCsv(results) {
    const header = ["NPI", "Provider Name", "NPPES Active", "Medicare PECOS", "In-Network Payers", "Stedi Status"];
    const rows = results.map((data) => {
      const s = data.summary || {};
      return [
        data.npi,
        s.provider_name || "",
        s.nppes_active === true ? "Active" : s.nppes_found ? "Inactive" : "Not found",
        s.medicare_enrolled === true ? "Enrolled" : s.medicare_enrolled === false ? "Not enrolled" : "Unknown",
        (s.in_network_payers || []).join("; "),
        s.stedi || "",
      ];
    });
    const csv = [header, ...rows].map((cols) => cols.map((c) => `"${String(c).replaceAll('"', '""')}"`).join(",")).join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "provider-status-batch.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  function openDrawer(data) {
    const drawer = $("drawer");
    $("drawer-title").textContent = (data.summary && data.summary.provider_name) || data.npi || "Lookup details";
    const body = $("drawer-body");
    body.innerHTML = renderLookup(data);
    hydrateJson(body, data);
    drawer.hidden = false;
  }

  function closeDrawer() {
    $("drawer").hidden = true;
  }

  async function runBatch() {
    const items = parseBatchText($("batch-input").value);
    if (!items.length) {
      showStatus($("batch-status"), "Add at least one NPI row.", "is-error");
      return;
    }
    if (items.length > 50) {
      showStatus($("batch-status"), "Batch is limited to 50 rows.", "is-error");
      return;
    }
    showStatus($("batch-status"), `Running ${items.length} lookup(s)…`);
    $("batch-run").disabled = true;
    try {
      const payload = await fetchJson("/provider-status/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items }),
      });
      state.batchResults = payload.results || [];
      renderBatchTable(state.batchResults);
      showStatus($("batch-status"), `Finished ${payload.count || state.batchResults.length} lookup(s).`, "is-ok");
    } catch (error) {
      showStatus($("batch-status"), error.message || "Batch lookup failed.", "is-error");
    } finally {
      $("batch-run").disabled = false;
    }
  }

  function renderPayersTab(catalog) {
    const fhir = (catalog.fhir_payers || []).map((p) => `
      <article class="card network-card">
        <div class="card-head"><h3>${text(p.name)}</h3>${badge("Live FHIR", "good")}</div>
        <dl class="dl-grid">
          <div><dt>ID</dt><dd>${text(p.id)}</dd></div>
          <div><dt>Search</dt><dd>${text(p.search_strategy)}</dd></div>
        </dl>
        <p class="muted" style="margin-top:8px">${text(p.base_url)}</p>
        ${p.notes ? `<p style="margin-top:8px">${text(p.notes)}</p>` : ""}
      </article>`).join("");
    const cms = catalog.cms || {};
    const stedi = catalog.stedi || {};
    const unsupported = (catalog.unsupported_or_auth_required || []).map((p) => `
      <tr>
        <td>${text(p.name)}</td>
        <td>${text(p.id)}</td>
        <td>${text(p.reason)}</td>
      </tr>`).join("");
    $("payers-results").innerHTML = `
      <div class="card">
        <div class="card-head"><h2>CMS &amp; Stedi sources</h2></div>
        <dl class="dl-grid">
          <div><dt>NPPES</dt><dd>${text(cms.nppes && cms.nppes.url)} · no auth</dd></div>
          <div><dt>Medicare PECOS</dt><dd>${text(cms.medicare_pecos && cms.medicare_pecos.dataset)} · no auth</dd></div>
          <div><dt>Stedi Payer Network</dt><dd>${stedi.configured ? badge("Connected", "good") : badge("Not configured", "warn")}<br><span class="muted">${text(stedi.payers_search_url)}</span></dd></div>
        </dl>
        <p class="muted" style="margin-top:10px">${text(cms.medicare_pecos && cms.medicare_pecos.meaning)}</p>
      </div>
      <div class="card-head" style="margin:18px 2px 10px"><h2>Live public Plan-Net FHIR payers</h2></div>
      <div class="network-grid">${fhir}</div>
      <div class="card" style="margin-top:16px">
        <div class="card-head"><h2>Auth-required or unsupported FHIR payers</h2></div>
        <div class="table-wrap"><table><thead><tr><th>Payer</th><th>ID</th><th>Reason</th></tr></thead><tbody>${unsupported}</tbody></table></div>
      </div>`;
  }

  function switchTab(id) {
    document.querySelectorAll(".tab").forEach((tab) => {
      const active = tab.dataset.tab === id;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    document.querySelectorAll(".panel").forEach((panel) => {
      panel.hidden = panel.id !== `panel-${id}`;
      panel.classList.toggle("is-active", !panel.hidden);
    });
    if (id === "history") renderHistory();
    if (id === "payers" && state.payersCatalog) renderPayersTab(state.payersCatalog);
  }

  function bind() {
    document.querySelectorAll(".tab").forEach((tab) => {
      tab.addEventListener("click", () => switchTab(tab.dataset.tab));
    });
    $("lookup-form").addEventListener("submit", (event) => {
      event.preventDefault();
      runLookup();
    });
    document.querySelectorAll(".chip").forEach((chip) => {
      chip.addEventListener("click", () => {
        $("npi").value = chip.dataset.npi || "";
        $("payer").value = chip.dataset.payer || "";
        $("tin").value = "";
        runLookup();
      });
    });
    $("batch-input").addEventListener("input", updateBatchCount);
    $("batch-run").addEventListener("click", runBatch);
    $("batch-file").addEventListener("change", async (event) => {
      const file = event.target.files && event.target.files[0];
      if (!file) return;
      $("batch-input").value = await file.text();
      updateBatchCount();
    });
    $("history-clear").addEventListener("click", () => {
      localStorage.removeItem(HISTORY_KEY);
      renderHistory();
    });
    document.querySelectorAll("[data-close-drawer]").forEach((el) => el.addEventListener("click", closeDrawer));
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeDrawer();
    });
  }

  async function init() {
    bind();
    renderHistory();
    updateBatchCount();
    await refreshHealth();
    try {
      const catalog = await loadPayerOptions();
      renderPayersTab(catalog);
    } catch (error) {
      showStatus($("payers-status"), error.message || "Unable to load payer catalog.", "is-error");
    }
    const qs = new URLSearchParams(window.location.search);
    if (qs.get("npi")) {
      $("npi").value = qs.get("npi");
      if (qs.get("payer")) $("payer").value = qs.get("payer");
      await runLookup();
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
