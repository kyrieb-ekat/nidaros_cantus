// Keyboard shortcuts for the review screen. Ignored while typing in a field.
(function () {
  const main = document.querySelector("main.review");
  const current = document.querySelector(".list .current");
  if (current) current.scrollIntoView({ block: "center" });
  if (!main) return;
  document.addEventListener("keydown", function (event) {
    const t = event.target;
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (t.closest && t.closest("input, textarea, select, [contenteditable]")) return;
    if (event.key === "j" && main.dataset.next) { location.href = main.dataset.next; }
    else if (event.key === "k" && main.dataset.prev) { location.href = main.dataset.prev; }
    else if (event.key === "f") { const b = document.querySelector('[data-key="f"]'); if (b) b.click(); }
    else if (/^[1-9]$/.test(event.key)) {
      const b = document.querySelector('[data-confirm="' + event.key + '"]');
      if (b) b.click();
    }
  });
})();
