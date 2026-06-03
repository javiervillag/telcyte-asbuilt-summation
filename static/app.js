const form = document.querySelector("#upload-form");
const fileInput = document.querySelector("#pdf-file");
const fileName = document.querySelector("#file-name");
const statusBox = document.querySelector("#status");
const submit = document.querySelector("#submit");

setReadyState();

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  fileName.textContent = file ? file.name : "No file selected";
  setReadyState();
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) {
    setStatus("Missing PDF", "Choose a PDF first.", "error");
    return;
  }

  submit.disabled = true;
  setStatus("Processing", "Generating annotated PDF.", "working");
  const data = new FormData();
  data.append("file", file);

  try {
    const response = await fetch("/api/summarize", { method: "POST", body: data });
    if (!response.ok) {
      let message = "The PDF could not be processed.";
      let warnings = [];
      let supportedTotals = [];
      let unresolvedCallouts = [];
      let unresolvedCalloutSummary = [];
      try {
        const json = await response.json();
        warnings = Array.isArray(json.warnings) ? json.warnings.filter(Boolean) : [];
        supportedTotals = Array.isArray(json.supported_totals) ? json.supported_totals.filter(Boolean) : [];
        unresolvedCallouts = Array.isArray(json.unresolved_callouts) ? json.unresolved_callouts.filter(Boolean) : [];
        unresolvedCalloutSummary = Array.isArray(json.unresolved_callout_summary)
          ? json.unresolved_callout_summary.map(formatCalloutSummary).filter(Boolean)
          : [];
        message = json.detail || message;
      } catch {
        // Keep default message.
      }
      throw Object.assign(new Error(message), {
        warnings,
        supportedTotals,
        unresolvedCallouts,
        unresolvedCalloutSummary,
      });
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
      unresolvedCalloutSummary: error.unresolvedCalloutSummary || [],
      unresolvedCallouts: error.unresolvedCallouts || [],
    });
  } finally {
    submit.disabled = false;
  }
});

function setReadyState() {
  setStatus("Ready", "Waiting for PDF.", "empty");
}

function setStatus(title, message, kind, details = []) {
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
  appendList("Callout groups", groups.unresolvedCalloutSummary || []);
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
  if (kind === "working") return "Live";
  return "Idle";
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

function formatCalloutSummary(summary) {
  if (!summary || typeof summary !== "object") return "";
  const count = Number(summary.count || 0);
  const type = summary.callout_type || "Callout";
  const cable = summary.cable_count ? ` - ${summary.cable_count}` : "";
  const footage = summary.total_footage ? ` - ${summary.total_footage}` : "";
  return `${type}${cable}: ${count}${footage}`;
}
