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
const processingOverlay = document.querySelector("#processing-overlay");

let extraCodeCategories = [];
let activeCategory = "All";
let codeState = {};

hideResult();

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  fileName.textContent = file ? file.name : "No file selected";
  hideResult();
});

codeSearch.addEventListener("input", () => {
  renderExtraCodes();
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
  showProcessing(true);
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
      try {
        const json = await response.json();
        warnings = Array.isArray(json.warnings) ? json.warnings.filter(Boolean) : [];
        supportedTotals = Array.isArray(json.supported_totals) ? json.supported_totals.filter(Boolean) : [];
        unresolvedCallouts = Array.isArray(json.unresolved_callouts) ? json.unresolved_callouts.filter(Boolean) : [];
        message = json.detail || message;
      } catch {
        // Keep default message.
      }
      throw Object.assign(new Error(message), { warnings, supportedTotals, unresolvedCallouts });
    }
    const warnings = readWarnings(response);
    const blob = await response.blob();
    const disposition = response.headers.get("Content-Disposition") || "";
    const match = disposition.match(/filename=\"?([^"]+)\"?/);
    const outputName = match ? match[1] : file.name.replace(/\.pdf$/i, "-telcyte-summary.pdf");
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = outputName;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
    setStatus("PDF ready", "Download started.", warnings.length ? "warn" : "done", warnings);
  } catch (error) {
    setStatus("Manual review", error.message, "error", {
      warnings: error.warnings || [],
      supportedTotals: error.supportedTotals || [],
      unresolvedCallouts: error.unresolvedCallouts || [],
    });
  } finally {
    showProcessing(false);
    submit.disabled = false;
    submit.textContent = "Generate PDF";
  }
});

function hideResult() {
  resultCard.classList.add("is-hidden");
  statusBox.textContent = "";
}

function setStatus(title, message, kind, details = []) {
  resultCard.classList.remove("is-hidden");
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
  appendList("Warnings", groups.warnings || []);
  appendList("Supported totals found", groups.supportedTotals || []);
  appendList("Needs manual interpretation", groups.unresolvedCallouts || []);
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

function statusLabel(kind) {
  if (kind === "done") return "Ready";
  if (kind === "warn") return "Check";
  if (kind === "error") return "Review";
  return "Status";
}

function showProcessing(isVisible) {
  processingOverlay.classList.toggle("is-visible", isVisible);
  processingOverlay.setAttribute("aria-hidden", isVisible ? "false" : "true");
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
      renderExtraCodes();
    });
  });
}

function renderExtraCodes() {
  const filter = codeSearch.value.trim().toLowerCase();
  captureCodeState();
  const groups = extraCodeCategories
    .filter((category) => activeCategory === "All" || category.name === activeCategory)
    .map((category) => {
      const codes = (category.codes || []).filter((item) => {
        const haystack = `${item.code} ${item.name} ${item.description} ${item.when_to_consider}`.toLowerCase();
        return codeState[item.code]?.checked || !filter || haystack.includes(filter);
      });
      return { name: category.name, codes };
    })
    .filter((category) => category.codes.length > 0);

  if (!groups.length) {
    codeList.innerHTML = `<div class="code-empty">No matching codes.</div>`;
    restoreCurrentCodeState();
    updateSelectedCount();
    return;
  }

  codeList.innerHTML = groups.map(renderCategory).join("");
  restoreCurrentCodeState();
  codeList.querySelectorAll(".code-toggle").forEach((checkbox) => {
    checkbox.addEventListener("change", () => {
      updateCodeRowState(checkbox.closest(".code-row"));
      captureCodeState();
      updateSelectedCount();
    });
    updateCodeRowState(checkbox.closest(".code-row"));
  });
  codeList.querySelectorAll(".code-quantity, .code-note").forEach((input) => {
    input.addEventListener("input", () => {
      captureCodeState();
      updateSelectedCount();
    });
  });
  updateSelectedCount();
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
      <div class="code-fields">
        <input class="code-quantity" type="text" inputmode="decimal" placeholder="Qty" aria-label="${code} quantity" disabled />
        <input class="code-note" type="text" maxlength="180" placeholder="Note" aria-label="${code} note" disabled />
        <span>${escapeHtml(item.unit)}</span>
      </div>
    </article>
  `;
}

function updateCodeRowState(row) {
  if (!row) return;
  const checked = row.querySelector(".code-toggle").checked;
  row.classList.toggle("selected", checked);
  row.querySelectorAll(".code-quantity, .code-note").forEach((input) => {
    input.disabled = !checked;
    if (checked && input.classList.contains("code-quantity") && !input.value) {
      input.value = "1";
    }
  });
}

function collectSelectedExtras() {
  const items = [];
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
    items.push({ code, quantity, note });
  }
  return { ok: true, message: "", items };
}

function captureCodeState() {
  document.querySelectorAll(".code-row").forEach((row) => {
    const code = row.dataset.code;
    codeState[code] = {
      checked: row.querySelector(".code-toggle").checked,
      quantity: row.querySelector(".code-quantity").value,
      note: row.querySelector(".code-note").value,
    };
  });
}

function restoreCurrentCodeState() {
  document.querySelectorAll(".code-row").forEach((row) => {
    const prior = codeState[row.dataset.code];
    if (!prior) return;
    row.querySelector(".code-toggle").checked = prior.checked;
    row.querySelector(".code-quantity").value = prior.quantity;
    row.querySelector(".code-note").value = prior.note;
  });
}

function updateSelectedCount() {
  captureCodeState();
  const count = Object.values(codeState).filter((state) => state.checked).length;
  selectedCount.textContent = `${count} selected`;
  selectedCount.classList.toggle("has-selection", count > 0);
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
