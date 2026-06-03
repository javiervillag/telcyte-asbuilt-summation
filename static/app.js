const form = document.querySelector("#upload-form");
const fileInput = document.querySelector("#pdf-file");
const fileName = document.querySelector("#file-name");
const resultCard = document.querySelector("#result-card");
const statusBox = document.querySelector("#status");
const submit = document.querySelector("#submit");
const codeList = document.querySelector("#code-list");
const codeSearch = document.querySelector("#code-search");
const selectedCount = document.querySelector("#selected-count");
const categoryTabs = document.querySelector("#category-tabs");
const toggleAll = document.querySelector("#toggle-all");
const toggleManual = document.querySelector("#toggle-manual");
const processingOverlay = document.querySelector("#processing-overlay");
const processingPercent = document.querySelector("#processing-percent");
const processingTitle = document.querySelector("#processing-title");
const processingMessage = document.querySelector("#processing-message");
const processingBar = document.querySelector("#processing-bar");
const processingEta = document.querySelector("#processing-eta");
const manualExtra = document.querySelector("#manual-extra");
const manualCode = document.querySelector("#manual-code");
const manualAdd = document.querySelector("#manual-add");
const manualMessage = document.querySelector("#manual-message");
const selectedExtras = document.querySelector("#selected-extras");
const selectedRows = document.querySelector("#selected-rows");
const clearSelected = document.querySelector("#clear-selected");
const uploadCard = document.querySelector(".upload-card");

let extraCodeCategories = [];
let activeCategory = "All";
let codeState = {};
let showAllCodes = false;
let showManualEntry = false;
let manualExtras = [];
let currentDownload = null;
let processingTimer = null;
let processingStartedAt = 0;
let processingProgress = 0;

const processingSteps = [
  { at: 5, title: "Preparing PDF", message: "Uploading the as-built and checking the selected extras." },
  { at: 18, title: "Reading callouts", message: "Extracting visible labels, notes, and quantity text from the drawing." },
  { at: 38, title: "Checking totals", message: "Combining repeated billing codes and flagging anything that needs review." },
  { at: 58, title: "Reviewing evidence", message: "Using the configured model only as a second check against extracted PDF text." },
  { at: 76, title: "Placing totals box", message: "Finding a clear top corner and writing the MKR Job Totals onto the PDF." },
  { at: 90, title: "Finishing PDF", message: "Preparing the download and final review details." },
];

hideResult();
updateManualControls();

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  fileName.textContent = file ? file.name : "No file selected";
  hideResult();
});

codeSearch.addEventListener("input", () => {
  if (codeSearch.value.trim()) {
    showAllCodes = false;
    activeCategory = "All";
    updateCatalogControls();
  }
  renderExtraCodes();
});

toggleAll.addEventListener("click", () => {
  showAllCodes = !showAllCodes;
  if (showAllCodes) {
    codeSearch.value = "";
  } else {
    activeCategory = "All";
  }
  updateCatalogControls();
  renderExtraCodes();
});

toggleManual.addEventListener("click", () => {
  showManualEntry = !showManualEntry;
  updateManualControls();
  if (showManualEntry) {
    manualCode.focus();
  }
});

manualAdd.addEventListener("click", () => {
  const code = normalizeManualCode(manualCode.value);
  const validation = validateManualCode(code);
  if (!validation.ok) {
    setManualMessage(validation.message, "error");
    return;
  }
  captureCodeState();
  if (codeState[code]?.checked || manualExtras.some((item) => item.code === code)) {
    setManualMessage(`${code} is already in the selected extras.`, "error");
    return;
  }
  manualExtras.push({ code, quantity: "1", note: "" });
  manualCode.value = "";
  renderSelectedExtras();
  updateSelectedCount();
  setManualMessage(`Added ${code}. Set qty or optional note below.`, "done");
  hideResult();
});

selectedRows.addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove-selected]");
  if (!button) return;
  removeSelectedExtra(button.dataset.source, button.dataset.code);
  renderExtraCodes();
  renderSelectedExtras();
  updateSelectedCount();
  setManualMessage("", "");
});

selectedRows.addEventListener("input", (event) => {
  const input = event.target.closest("[data-selected-field]");
  if (!input) return;
  updateSelectedExtra(input.dataset.source, input.dataset.code, input.dataset.selectedField, input.value);
  syncVisibleCatalogRow(input.dataset.code);
});

clearSelected.addEventListener("click", () => {
  clearSelectedExtras();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) {
    setStatus("Choose a PDF first", "Select an as-built PDF before generating the output.", "error");
    return;
  }

  const selectedExtras = collectSelectedExtras();
  if (!selectedExtras.ok) {
    setStatus("Check extra codes", selectedExtras.message, "error");
    return;
  }

  submit.disabled = true;
  submit.textContent = "Generating...";
  hideResult();
  startProcessingProgress();
  const data = new FormData();
  data.append("file", file);
  data.append("extra_billing_codes", JSON.stringify(selectedExtras.items));

  try {
    const response = await fetch("/api/summarize", { method: "POST", body: data });
    if (!response.ok) {
      let message = "The PDF could not be processed.";
      let warnings = [];
      let supportedTotals = [];
      let unresolvedCallouts = [];
      let resultSummary = null;
      try {
        const json = await response.json();
        warnings = Array.isArray(json.warnings) ? json.warnings.filter(Boolean) : [];
        supportedTotals = Array.isArray(json.supported_totals) ? json.supported_totals.filter(Boolean) : [];
        unresolvedCallouts = Array.isArray(json.unresolved_callouts) ? json.unresolved_callouts.filter(Boolean) : [];
        resultSummary = json.result_summary && typeof json.result_summary === "object" ? json.result_summary : null;
        message = json.detail || message;
      } catch {
        // Keep default message.
      }
      throw Object.assign(new Error(message), { warnings, supportedTotals, unresolvedCallouts, resultSummary });
    }
    const warnings = readWarnings(response);
    const resultSummary = readResultSummary(response);
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename=\"?([^"]+)\"?/);
    const outputName = match ? match[1] : file.name.replace(/\.pdf$/i, "-telcyte-summary.pdf");
    if (resultSummary && !resultSummary.output_name) {
      resultSummary.output_name = outputName;
    }
    clearCurrentDownload();
    const url = URL.createObjectURL(blob);
    currentDownload = { url, filename: outputName };
    triggerDownload(currentDownload);
    setStatus("PDF ready", "Download started.", warnings.length ? "warn" : "done", {
      warnings,
      resultSummary,
      canStartOver: true,
      download: currentDownload,
    });
  } catch (error) {
    setStatus("Manual review", error.message, "error", {
      warnings: error.warnings || [],
      supportedTotals: error.supportedTotals || [],
      unresolvedCallouts: error.unresolvedCallouts || [],
      resultSummary: error.resultSummary || null,
      canStartOver: true,
    });
  } finally {
    stopProcessingProgress();
    submit.disabled = false;
    submit.textContent = "Generate PDF";
  }
});

function hideResult() {
  clearCurrentDownload();
  resultCard.classList.add("is-hidden");
  statusBox.textContent = "";
}

function resetForAnotherPdf() {
  clearCurrentDownload();
  fileInput.value = "";
  fileName.textContent = "No file selected";
  codeSearch.value = "";
  activeCategory = "All";
  showAllCodes = false;
  showManualEntry = false;
  manualCode.value = "";
  clearSelectedExtras();
  hideResult();
  updateCatalogControls();
  updateManualControls();
  uploadCard?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function clearSelectedExtras() {
  for (const state of Object.values(codeState)) {
    state.checked = false;
    state.quantity = state.quantity || "1";
    state.note = state.note || "";
  }
  document.querySelectorAll(".code-toggle").forEach((checkbox) => {
    checkbox.checked = false;
  });
  manualExtras = [];
  renderExtraCodes();
  renderSelectedExtras();
  updateSelectedCount();
  setManualMessage("", "");
}

function setStatus(title, message, kind, details = []) {
  resultCard.classList.remove("is-hidden");
  resultCard.scrollIntoView({ behavior: "smooth", block: "start" });
  statusBox.textContent = "";
  const groups = Array.isArray(details) ? { warnings: details } : details;
  const summary = document.createElement("div");
  summary.className = "status-summary";
  const badge = document.createElement("span");
  badge.className = "status-badge";
  badge.textContent = statusLabel(kind);
  const copy = document.createElement("div");
  const heading = document.createElement("strong");
  heading.textContent = title;
  const body = document.createElement("span");
  body.textContent = message;
  copy.appendChild(heading);
  copy.appendChild(body);
  summary.appendChild(badge);
  summary.appendChild(copy);
  statusBox.appendChild(summary);
  appendResultSummary(groups.resultSummary || null, groups.warnings || []);
  if (!groups.resultSummary) {
    appendList("Warnings", groups.warnings || []);
    appendList("Supported totals found", groups.supportedTotals || []);
    appendList("Needs manual interpretation", groups.unresolvedCallouts || []);
  }
  if (groups.canStartOver) {
    appendStatusActions(groups.download || null);
  }
  statusBox.className = kind ? `status ${kind}` : "status";
}

function appendList(title, items) {
  if (!items.length) return;
  const block = document.createElement("div");
  block.className = "status-group";
  const heading = document.createElement("strong");
  heading.textContent = title;
  block.appendChild(heading);
  const list = document.createElement("ul");
  for (const detail of items) {
    const item = document.createElement("li");
    item.textContent = detail;
    list.appendChild(item);
  }
  block.appendChild(list);
  statusBox.appendChild(block);
}

function appendResultSummary(summary, warnings = []) {
  if (!summary) return;
  const block = document.createElement("div");
  block.className = "included-summary";
  const heading = document.createElement("strong");
  heading.textContent = summary.output_name ? "Included in this PDF" : "Review summary";
  block.appendChild(heading);
  const rows = [];
  if (summary.output_name) {
    rows.push(["Output", summary.output_name]);
  }
  rows.push(
    ["Detected totals", countLabel(summary.detected_totals, "total")],
    ["Extra billing codes", countLabel(summary.extra_billing_codes, "code")],
  );
  if (Array.isArray(summary.materials) && summary.materials.length) {
    rows.push(["Materials", countLabel(summary.materials, "item")]);
  }
  for (const [label, value] of rows) {
    const row = document.createElement("div");
    row.className = "included-row";
    const rowLabel = document.createElement("span");
    rowLabel.textContent = label;
    const rowValue = document.createElement("b");
    rowValue.textContent = value;
    row.appendChild(rowLabel);
    row.appendChild(rowValue);
    block.appendChild(row);
  }
  appendDetailSection(block, "Detected totals details", summary.detected_totals);
  appendDetailSection(block, "Extra billing codes details", summary.extra_billing_codes);
  appendDetailSection(block, "Review items", warnings);
  appendDetailSection(block, "MKR Job Totals details", summary.result_lines);
  statusBox.appendChild(block);
}

function appendDetailSection(block, title, lines) {
  if (!Array.isArray(lines) || !lines.length) return;
  const details = document.createElement("details");
  details.className = "included-details";
  const summary = document.createElement("summary");
  summary.textContent = title;
  const pre = document.createElement("pre");
  pre.textContent = lines.filter(Boolean).join("\n");
  details.appendChild(summary);
  details.appendChild(pre);
  block.appendChild(details);
}

function appendStatusActions(download) {
  const block = document.createElement("div");
  block.className = "status-actions";
  if (download?.url && download?.filename) {
    const downloadButton = document.createElement("button");
    downloadButton.type = "button";
    downloadButton.className = "download-button";
    downloadButton.textContent = "Download PDF";
    downloadButton.addEventListener("click", () => triggerDownload(download));
    block.appendChild(downloadButton);
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "start-over-button";
  button.textContent = "Start another PDF";
  button.addEventListener("click", resetForAnotherPdf);
  block.appendChild(button);
  statusBox.appendChild(block);
}

function triggerDownload(download) {
  if (!download?.url || !download?.filename) return;
  const link = document.createElement("a");
  link.href = download.url;
  link.download = download.filename;
  link.rel = "noopener";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function clearCurrentDownload() {
  if (currentDownload?.url) {
    URL.revokeObjectURL(currentDownload.url);
  }
  currentDownload = null;
}

function statusLabel(kind) {
  if (kind === "done") return "Ready";
  if (kind === "warn") return "Check";
  if (kind === "error") return "Review";
  return "Status";
}

function startProcessingProgress() {
  processingStartedAt = Date.now();
  processingProgress = 0;
  updateProcessingProgress(4);
  processingOverlay.classList.add("is-visible");
  processingOverlay.setAttribute("aria-hidden", "false");
  window.clearInterval(processingTimer);
  processingTimer = window.setInterval(() => {
    const elapsedSeconds = (Date.now() - processingStartedAt) / 1000;
    const target = Math.min(94, 8 + elapsedSeconds * 3.8);
    const next = processingProgress + Math.max(0.4, (target - processingProgress) * 0.18);
    updateProcessingProgress(next);
  }, 550);
}

function stopProcessingProgress() {
  window.clearInterval(processingTimer);
  processingTimer = null;
  updateProcessingProgress(100);
  processingOverlay.classList.remove("is-visible");
  processingOverlay.setAttribute("aria-hidden", "true");
}

function updateProcessingProgress(value) {
  processingProgress = Math.max(0, Math.min(100, Math.round(value)));
  const step = processingSteps.reduce((current, candidate) => {
    return processingProgress >= candidate.at ? candidate : current;
  }, processingSteps[0]);
  const remaining = estimateRemainingSeconds(processingProgress);

  processingPercent.textContent = `${processingProgress}%`;
  processingTitle.textContent = step.title;
  processingMessage.textContent = step.message;
  processingPercent.parentElement?.style.setProperty("--progress", `${processingProgress}%`);
  processingBar.style.width = `${processingProgress}%`;
  processingEta.textContent =
    processingProgress >= 94
      ? "Almost done. Finalizing the PDF now."
      : `Estimated time left: about ${remaining} seconds`;
}

function estimateRemainingSeconds(progress) {
  const elapsedSeconds = Math.max(1, (Date.now() - processingStartedAt) / 1000);
  const estimatedTotal = Math.max(18, elapsedSeconds / Math.max(progress, 1) * 100);
  const remaining = Math.max(3, Math.round(estimatedTotal - elapsedSeconds));
  return Math.min(45, remaining);
}

function readWarnings(response) {
  const raw = response.headers.get("X-Telcyte-Warnings");
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? parsed.filter(Boolean) : [];
  } catch {
    return [];
  }
}

function readResultSummary(response) {
  const raw = response.headers.get("X-Telcyte-Result-Summary");
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

function countLabel(items, singular) {
  if (!Array.isArray(items)) return "None";
  const clean = items.filter(Boolean);
  if (!clean.length) return "None";
  const plural = singular === "code" ? "codes" : `${singular}s`;
  return `${clean.length} ${clean.length === 1 ? singular : plural}`;
}

async function loadExtraCodes() {
  codeList.innerHTML = `<div class="code-empty">Loading code list...</div>`;
  try {
    const response = await fetch("/api/extra-billing-codes");
    if (!response.ok) {
      throw new Error("Code list unavailable.");
    }
    const data = await response.json();
    extraCodeCategories = data.categories || [];
    renderCategoryTabs();
    updateCatalogControls();
    renderExtraCodes();
  } catch (error) {
    codeList.innerHTML = `<div class="code-empty">${escapeHtml(error.message)}</div>`;
  }
}

function renderCategoryTabs() {
  const names = ["All", ...extraCodeCategories.map((category) => category.name)];
  categoryTabs.innerHTML = names
    .map((name) => {
      const isActive = name === activeCategory ? "active" : "";
      return `<button class="category-tab ${isActive}" type="button" data-category="${escapeHtml(name)}">${escapeHtml(name)}</button>`;
    })
    .join("");
  categoryTabs.querySelectorAll(".category-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      activeCategory = tab.dataset.category || "All";
      renderCategoryTabs();
      updateCatalogControls();
      renderExtraCodes();
    });
  });
}

function renderExtraCodes() {
  const filter = codeSearch.value.trim().toLowerCase();
  captureCodeState();
  const groups = extraCodeCategories
    .filter((category) => showAllCodes || filter || _categoryHasSelectedCode(category))
    .filter((category) => !showAllCodes || activeCategory === "All" || category.name === activeCategory)
    .map((category) => {
      const codes = (category.codes || []).filter((item) => {
        const haystack = `${item.code} ${item.name} ${item.description} ${item.when_to_consider}`.toLowerCase();
        if (codeState[item.code]?.checked) return false;
        if (filter) return haystack.includes(filter);
        return showAllCodes;
      });
      return { name: category.name, codes };
    })
    .filter((category) => category.codes.length > 0);

  if (!groups.length) {
    const message = filter
      ? "No matching codes."
      : "Search for an extra billing code, or show the full catalog when you need to browse.";
    codeList.innerHTML = `
      <div class="code-empty">
        <span>${escapeHtml(message)}</span>
      </div>
    `;
    restoreCurrentCodeState();
    updateSelectedCount();
    renderSelectedExtras();
    return;
  }

  codeList.innerHTML = groups.map(renderCategory).join("");
  restoreCurrentCodeState();
  codeList.querySelectorAll(".code-toggle").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      updateCodeRowState(checkbox.closest(".code-row"));
      captureCodeState();
      renderSelectedExtras();
      updateSelectedCount();
      renderExtraCodes();
    });
    updateCodeRowState(checkbox.closest(".code-row"));
  });
  updateSelectedCount();
  renderSelectedExtras();
}

function renderCategory(category) {
  return `
    <div class="code-group">
      <h3>${escapeHtml(category.name)}</h3>
      ${category.codes.map(renderCodeRow).join("")}
    </div>
  `;
}

function renderCodeRow(item) {
  const code = escapeHtml(item.code);
  return `
    <article class="code-row" data-code="${code}">
      <label class="code-main">
        <input class="code-toggle" type="checkbox" value="${code}" />
        <span>
          <strong>${code}</strong>
          <b>${escapeHtml(item.name)}</b>
          <small>${escapeHtml(item.description)}</small>
          <em>${escapeHtml(item.when_to_consider)}</em>
        </span>
      </label>
    </article>
  `;
}

function updateCodeRowState(row) {
  if (!row) return;
  const checked = row.querySelector(".code-toggle").checked;
  row.classList.toggle("selected", checked);
}

function collectSelectedExtras() {
  const items = [];
  const seen = new Set();
  captureCodeState();
  for (const [code, state] of Object.entries(codeState)) {
    if (!state.checked) continue;
    const quantity = state.quantity.trim();
    const note = state.note.trim();
    if (!quantity) {
      return { ok: false, message: `Add a quantity for ${code}.`, items: [] };
    }
    if (!/^\d+(\.\d+)?(\s*('|sqft|hr|hrs|ea|each))?$/i.test(quantity)) {
      return { ok: false, message: `${code} quantity must be a number.`, items: [] };
    }
    if (seen.has(code)) {
      return { ok: false, message: `${code} is selected more than once.`, items: [] };
    }
    seen.add(code);
    items.push({ code, quantity, note });
  }
  for (const item of manualExtras) {
    const validation = validateManualExtra(item.code, item.quantity);
    if (!validation.ok) return { ok: false, message: validation.message, items: [] };
    if (seen.has(item.code)) {
      return { ok: false, message: `${item.code} is selected more than once.`, items: [] };
    }
    seen.add(item.code);
    items.push(item);
  }
  return { ok: true, message: "", items };
}

function captureCodeState() {
  document.querySelectorAll(".code-row").forEach((row) => {
    const code = row.dataset.code;
    const prior = codeState[code] || { quantity: "1", note: "" };
    codeState[code] = {
      checked: row.querySelector(".code-toggle").checked,
      quantity: prior.quantity || "1",
      note: prior.note || "",
    };
  });
}

function restoreCurrentCodeState() {
  document.querySelectorAll(".code-row").forEach((row) => {
    const prior = codeState[row.dataset.code];
    if (!prior) return;
    row.querySelector(".code-toggle").checked = prior.checked;
  });
}

function updateSelectedCount() {
  captureCodeState();
  const count = Object.values(codeState).filter((state) => state.checked).length + manualExtras.length;
  selectedCount.textContent = `${count} selected`;
  selectedCount.classList.toggle("has-selection", count > 0);
}

function updateCatalogControls() {
  toggleAll.textContent = showAllCodes ? "Hide full catalog" : "Show all codes";
  categoryTabs.classList.toggle("is-hidden", !showAllCodes);
  renderCategoryTabs();
}

function updateManualControls() {
  manualExtra.classList.toggle("is-hidden", !showManualEntry);
  toggleManual.textContent = showManualEntry ? "Hide manual" : "Manual code";
  toggleManual.classList.toggle("active", showManualEntry);
  toggleManual.setAttribute("aria-expanded", showManualEntry ? "true" : "false");
  if (!showManualEntry) {
    setManualMessage("", "");
  }
}

function _categoryHasSelectedCode(category) {
  return (category.codes || []).some((item) => codeState[item.code]?.checked);
}

function renderSelectedExtras() {
  const rows = selectedExtraRows();
  selectedExtras.classList.toggle("is-empty", rows.length === 0);
  if (!rows.length) {
    selectedRows.innerHTML = "";
    return;
  }
  selectedRows.innerHTML = rows
    .map(
      (item) => `
        <div class="selected-row">
          <div class="selected-code">
            <span>${escapeHtml(item.sourceLabel)}</span>
            <strong>${escapeHtml(item.code)}</strong>
            <small>${escapeHtml(item.name)}</small>
          </div>
          <input class="selected-quantity" type="text" inputmode="decimal" value="${escapeHtml(item.quantity)}" aria-label="${escapeHtml(item.code)} selected quantity" data-source="${escapeHtml(item.source)}" data-code="${escapeHtml(item.code)}" data-selected-field="quantity" />
          <input class="selected-note" type="text" maxlength="180" value="${escapeHtml(item.note)}" placeholder="Optional note" aria-label="${escapeHtml(item.code)} selected note" data-source="${escapeHtml(item.source)}" data-code="${escapeHtml(item.code)}" data-selected-field="note" />
          <button class="selected-remove" type="button" aria-label="Remove ${escapeHtml(item.code)}" data-remove-selected="true" data-source="${escapeHtml(item.source)}" data-code="${escapeHtml(item.code)}">Remove</button>
        </div>
      `,
    )
    .join("");
}

function selectedExtraRows() {
  captureCodeState();
  const rows = [];
  for (const [code, state] of Object.entries(codeState)) {
    if (!state.checked) continue;
    const item = catalogItemForCode(code);
    rows.push({
      source: "catalog",
      sourceLabel: "Catalog",
      code,
      name: item?.name || "Catalog extra",
      quantity: state.quantity || "1",
      note: state.note || "",
    });
  }
  for (const item of manualExtras) {
    rows.push({
      source: "manual",
      sourceLabel: "Manual",
      code: item.code,
      name: "Manual exception",
      quantity: item.quantity,
      note: item.note,
    });
  }
  return rows;
}

function catalogItemForCode(code) {
  for (const category of extraCodeCategories) {
    const match = (category.codes || []).find((item) => item.code === code);
    if (match) return match;
  }
  return null;
}

function updateSelectedExtra(source, code, field, value) {
  if (source === "manual") {
    const item = manualExtras.find((row) => row.code === code);
    if (item) item[field] = value;
    return;
  }
  codeState[code] = {
    checked: true,
    quantity: field === "quantity" ? value : codeState[code]?.quantity || "1",
    note: field === "note" ? value : codeState[code]?.note || "",
  };
}

function removeSelectedExtra(source, code) {
  if (source === "manual") {
    manualExtras = manualExtras.filter((item) => item.code !== code);
    return;
  }
  if (codeState[code]) {
    codeState[code].checked = false;
    syncVisibleCatalogRow(code);
  }
}

function syncVisibleCatalogRow(code) {
  for (const row of document.querySelectorAll(".code-row")) {
    if (row.dataset.code !== code || !codeState[code]) continue;
    row.querySelector(".code-toggle").checked = codeState[code].checked;
    updateCodeRowState(row);
  }
}

function normalizeManualCode(value) {
  return value.trim().toUpperCase().replace(/\s+/g, "").replace(/^([A-Z]{2,6})(\d)/, "$1-$2");
}

function validateManualExtra(code, quantity) {
  const codeValidation = validateManualCode(code);
  if (!codeValidation.ok) return codeValidation;
  if (!quantity) return { ok: false, message: `Add a quantity for ${code}.` };
  if (!/^\d+(\.\d+)?(\s*('|sqft|hr|hrs|ea|each))?$/i.test(quantity)) {
    return { ok: false, message: `${code} quantity must be a number.` };
  }
  return { ok: true, message: "" };
}

function validateManualCode(code) {
  if (!code) return { ok: false, message: "Add a billing code." };
  if (!/^[A-Z0-9][A-Z0-9-]{1,19}$/.test(code)) {
    return { ok: false, message: "Use letters, numbers, and hyphens only." };
  }
  return { ok: true, message: "" };
}

function setManualMessage(message, kind) {
  manualMessage.textContent = message;
  manualMessage.className = kind ? `manual-message ${kind}` : "manual-message";
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

loadExtraCodes();
