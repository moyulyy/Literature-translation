(() => {
  const form = document.getElementById("uploadForm");
  if (!form) return;

  const input = document.getElementById("pdfInput");
  const dropzone = document.getElementById("dropzone");
  const chip = document.getElementById("fileChip");
  const submit = document.getElementById("submitBtn");
  const errorBox = document.getElementById("formError");
  const overlay = document.getElementById("uploadOverlay");
  const overlayMessage = document.getElementById("overlayMessage");
  const overlayProgress = document.getElementById("overlayProgress");

  let file = null;
  let busy = false;

  function showError(message) {
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
  }

  function setFile(candidate) {
    if (!candidate) return;
    if (!/\.pdf$/i.test(candidate.name) && candidate.type !== "application/pdf") {
      showError("请选择 PDF 文件。");
      return;
    }
    file = candidate;
    const mb = candidate.size / (1024 * 1024);
    chip.textContent = `${candidate.name} · ${mb.toFixed(1)} MB`;
    chip.hidden = false;
    submit.disabled = false;
    showError("");
  }

  input.addEventListener("change", () => setFile(input.files && input.files[0]));

  ["dragenter", "dragover"].forEach((name) => {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.add("is-drag");
    });
  });
  ["dragleave", "drop"].forEach((name) => {
    dropzone.addEventListener(name, (event) => {
      event.preventDefault();
      dropzone.classList.remove("is-drag");
    });
  });
  dropzone.addEventListener("drop", (event) => {
    const dropped = event.dataTransfer && event.dataTransfer.files;
    if (dropped && dropped.length) setFile(dropped[0]);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (busy) return;
    if (!file) {
      showError("请先选择一个 PDF 文件。");
      return;
    }
    busy = true;
    submit.disabled = true;
    showError("");
    overlay.hidden = false;
    overlayMessage.textContent = "上传文件中…";
    overlayProgress.style.width = "4%";

    const data = new FormData();
    data.append("file", file, file.name);

    try {
      const response = await fetch("/api/translate", { method: "POST", body: data });
      if (!response.ok) {
        let detail = `上传失败（HTTP ${response.status}）`;
        try {
          const body = await response.json();
          if (body && body.detail) detail = body.detail;
        } catch (_) {
          /* ignore */
        }
        throw new Error(detail);
      }
      const body = await response.json();
      poll(body.job_id);
    } catch (error) {
      overlay.hidden = true;
      submit.disabled = false;
      busy = false;
      showError(error.message || "上传失败，请重试。");
    }
  });

  async function poll(jobId) {
    try {
      const response = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
      if (!response.ok) throw new Error("无法读取任务状态");
      const job = await response.json();
      overlayMessage.textContent = job.message || "处理中…";
      overlayProgress.style.width = `${Math.max(4, job.progress || 0)}%`;
      if (job.status === "done") {
        window.location.href = `/doc/${job.id}`;
        return;
      }
      if (job.status === "error") {
        overlay.hidden = true;
        submit.disabled = false;
        busy = false;
        showError(job.error || job.message || "处理失败");
        return;
      }
      window.setTimeout(() => poll(jobId), 1400);
    } catch (error) {
      window.setTimeout(() => poll(jobId), 2600);
    }
  }
})();
