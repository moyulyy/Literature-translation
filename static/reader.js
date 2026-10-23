(() => {
  const body = document.body;
  const buttons = Array.from(document.querySelectorAll("[data-view]"));

  function setView(view) {
    body.classList.toggle("view-zh", view === "zh");
    buttons.forEach((button) => {
      button.classList.toggle("active", button.dataset.view === view);
    });
  }

  let saved = "bi";
  try {
    saved = window.localStorage.getItem("paper-view") || "bi";
  } catch (_) {
    /* ignore */
  }
  setView(saved === "zh" ? "zh" : "bi");

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      const view = button.dataset.view === "zh" ? "zh" : "bi";
      setView(view);
      try {
        window.localStorage.setItem("paper-view", view);
      } catch (_) {
        /* ignore */
      }
    });
  });

  const printButton = document.getElementById("printBtn");
  if (printButton) printButton.addEventListener("click", () => window.print());

  const bar = document.getElementById("readingProgress");
  function updateProgress() {
    if (!bar) return;
    const height = document.documentElement.scrollHeight - window.innerHeight;
    const ratio = height > 0 ? window.scrollY / height : 0;
    bar.style.width = `${Math.min(100, Math.max(0, ratio * 100))}%`;
  }
  window.addEventListener("scroll", updateProgress, { passive: true });
  window.addEventListener("resize", updateProgress);
  updateProgress();

  const links = Array.from(document.querySelectorAll(".toc a"));
  const headings = Array.from(document.querySelectorAll(".section-title[id]"));
  if (links.length && headings.length && "IntersectionObserver" in window) {
    const lookup = new Map();
    links.forEach((link) => {
      const id = (link.getAttribute("href") || "").replace(/^#/, "");
      if (id) lookup.set(id, link);
    });
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (!entry.isIntersecting) return;
          links.forEach((link) => link.classList.remove("active"));
          const active = lookup.get(entry.target.id);
          if (active) active.classList.add("active");
        });
      },
      { rootMargin: "-96px 0px -68% 0px", threshold: 0 }
    );
    headings.forEach((heading) => observer.observe(heading));
  }

  function retryImage(img) {
    if (img.dataset.retried === "1") {
      const fallback = document.createElement("div");
      fallback.className = "img-fallback";
      fallback.textContent = "图片加载失败，请刷新页面；也可点击“下载译文”查看。";
      img.replaceWith(fallback);
      return;
    }
    img.dataset.retried = "1";
    const url = new URL(img.src, window.location.href);
    url.searchParams.set("retry", Date.now().toString());
    img.src = url.toString();
  }

  document.querySelectorAll("img[data-retry]").forEach((img) => {
    img.addEventListener("error", () => retryImage(img));
    if (img.complete && img.naturalWidth === 0) retryImage(img);
  });
})();
