(() => {
  const jobId = window.__JOB_ID__;
  if (!jobId) return;

  const message = document.getElementById("statusMessage");
  const progress = document.getElementById("statusProgress");
  const percent = document.getElementById("statusPercent");
  const steps = Array.from(document.querySelectorAll("#statusSteps li"));

  function updateSteps(value) {
    steps.forEach((step, index) => {
      const threshold = Number(step.dataset.min || 0);
      const next = steps[index + 1];
      const nextThreshold = next ? Number(next.dataset.min || 100) : 100;
      step.classList.toggle("done", value >= nextThreshold);
      step.classList.toggle("active", value >= threshold && value < nextThreshold);
    });
  }

  async function tick() {
    try {
      const response = await fetch(`/api/jobs/${jobId}`, { cache: "no-store" });
      if (!response.ok) throw new Error("not found");
      const job = await response.json();
      if (message) message.textContent = job.message || "处理中…";
      if (progress) progress.style.width = `${job.progress || 0}%`;
      if (percent) percent.textContent = job.progress || 0;
      updateSteps(job.progress || 0);
      if (job.status === "done") {
        window.location.replace(`/doc/${job.id}`);
        return;
      }
      if (job.status === "error") {
        window.location.reload();
        return;
      }
      window.setTimeout(tick, 1500);
    } catch (error) {
      window.setTimeout(tick, 3000);
    }
  }

  tick();
})();
