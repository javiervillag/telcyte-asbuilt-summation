const form = document.querySelector("#upload-form");
const fileInput = document.querySelector("#pdf-file");
const fileName = document.querySelector("#file-name");
const statusBox = document.querySelector("#status");
const submit = document.querySelector("#submit");

fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  fileName.textContent = file ? file.name : "No file selected";
  statusBox.textContent = "";
  statusBox.className = "status";
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
      try {
        const json = await response.json();
        const details = Array.isArray(json.warnings) && json.warnings.length
          ? ` ${json.warnings.join(" ")}`
          : "";
        message = `${json.detail || message}${details}`;
      } catch {
        // Keep default message.
      }
      throw new Error(message);
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
    setStatus(warnings.length ? `PDF ready. Review note: ${warnings.join(" ")}` : "PDF ready.", warnings.length ? "warn" : "done");
  } catch (error) {
    setStatus(error.message, "error");
  } finally {
    submit.disabled = false;
  }
});

function setStatus(message, kind) {
  statusBox.textContent = message;
  statusBox.className = kind ? `status ${kind}` : "status";
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
