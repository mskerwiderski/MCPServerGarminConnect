/* msk-ui v1.1.0 — suggestions while typing (addresses, places, anything with a label).
   Markup: <span class="ac"><input></span> - the list is added next to the input.
   mskAutocomplete(input, {url(q) -> address of a JSON list [{label, ...}], onPick(hit), onType()})
   onType runs on every keystroke, so whatever a pick filled in can be cleared again.
   Source of truth: claude-toolkit/plugins/msk-web/skills/msk-ui/assets/ — never edit the copy. */
window.mskAutocomplete = (function () {
  const MIN_CHARS = 3, WAIT_MS = 250;
  let lists = 0;

  return function (input, { url, onPick, onType }) {
    const list = document.createElement("ul");
    list.className = "ac-list";
    list.id = "ac-list-" + ++lists;
    list.setAttribute("role", "listbox");
    list.hidden = true;
    input.parentNode.appendChild(list);
    input.autocomplete = "off";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-controls", list.id);
    input.setAttribute("aria-expanded", "false");
    let timer, seq = 0, active = -1, hits = [];

    const close = () => {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      active = -1;
    };
    const choose = (i) => {
      if (!hits[i]) return;
      input.value = hits[i].label;
      onPick(hits[i]);
      close();
    };
    const mark = (i) => {
      active = i;
      Array.from(list.children).forEach((li, j) => {
        li.classList.toggle("active", j === i);
        li.setAttribute("aria-selected", String(j === i));
      });
    };
    const show = (found) => {
      hits = found;
      list.textContent = "";
      found.forEach((hit, i) => {
        const li = document.createElement("li");
        li.setAttribute("role", "option");
        li.textContent = hit.label;
        // mousedown comes before the input loses the focus, and on a phone only
        // after a tap - pointerdown would pick when a finger scrolls the list.
        li.addEventListener("mousedown", (e) => { e.preventDefault(); choose(i); });
        list.appendChild(li);
      });
      list.hidden = !found.length;
      input.setAttribute("aria-expanded", String(!!found.length));
      active = -1;
    };

    input.addEventListener("input", () => {
      onType();
      clearTimeout(timer);
      const q = input.value.trim();
      const mine = ++seq;               // an answer still on its way is out of date
      if (q.length < MIN_CHARS) { close(); return; }
      timer = setTimeout(() => {
        fetch(url(q), { credentials: "same-origin" })
          .then((r) => (r.ok ? r.json() : []))
          .then((found) => { if (mine === seq) show(found); })
          .catch(close);
      }, WAIT_MS);
    });
    input.addEventListener("keydown", (e) => {
      if (list.hidden) return;
      if (e.key === "ArrowDown") { e.preventDefault(); mark(Math.min(active + 1, hits.length - 1)); }
      else if (e.key === "ArrowUp") { e.preventDefault(); mark(Math.max(active - 1, 0)); }
      else if (e.key === "Enter" && active >= 0) { e.preventDefault(); choose(active); }
      else if (e.key === "Escape") { close(); }
    });
    input.addEventListener("blur", () => setTimeout(close, 150));
  };
})();
