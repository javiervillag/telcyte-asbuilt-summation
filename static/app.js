const form = document.querySelector("#upload-form");
const fileInput = document.querySelector("#pdf-file");
const fileName = document.querySelector("#file-name");
const statusBox = document.querySelector("#status");
const submit = document.querySelector("#submit");

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  fileName.textContent = file ? file.name : "No file selected";
  setStatus("", "");
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const file = fileInput.files[0];
  if (!file) {
    setStatus("Choose a PDF first.", "error");
    return;
  }

  submit.disabled = true;
  setStatus("Generating annotated PDF...", "");
  const data = new FormData();
  data.append("file", file);

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
    setStatus("PDF ready.", warnings.length ? "warn" : "done", warnings);
  } catch (error) {
    setStatus(error.message, "error", {
      warnings: error.warnings || [],
      supportedTotals: error.supportedTotals || [],
      unresolvedCallouts: error.unresolvedCallouts || [],
    });
  } finally {
    submit.disabled = false;
  }
});

function setStatus(message, kind, details = []) {
  statusBox.textContent = "";
  const groups = Array.isArray(details) ? { warnings: details } : details;
  if (message) {
    const summary = document.createElement("p");
    summary.textContent = message;
    statusBox.appendChild(summary);
  }
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
