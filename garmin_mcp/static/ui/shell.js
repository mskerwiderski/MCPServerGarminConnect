/* msk-ui v1.0.0 — shell behaviour: the mobile "More" bottom sheet.
   Markup: button#more-btn in .tabbar-bottom, div#more-sheet.sheet, div#more-backdrop.
   Source of truth: claude-toolkit/plugins/msk-web/skills/msk-ui/assets/ — never edit the copy. */
(function () {
  function setup() {
    const btn = document.getElementById("more-btn");
    const sheet = document.getElementById("more-sheet");
    const backdrop = document.getElementById("more-backdrop");
    if (!btn || !sheet || !backdrop || btn.dataset.mskUi) return;
    btn.dataset.mskUi = "1";
    const open = (on) => {
      sheet.hidden = backdrop.hidden = !on;
      btn.setAttribute("aria-expanded", String(on));
      document.body.classList.toggle("sheet-open", on);
    };
    btn.addEventListener("click", () => open(sheet.hidden));
    backdrop.addEventListener("click", () => open(false));
    sheet.addEventListener("click", (e) => { if (e.target.closest("a")) open(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !sheet.hidden) open(false); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", setup);
  else setup();
  // htmx apps swap only parts of the page; re-run when the shell is re-rendered
  document.addEventListener("htmx:afterSettle", setup);
})();
