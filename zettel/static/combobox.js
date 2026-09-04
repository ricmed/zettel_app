(() => {
  const DEBOUNCE_MS = 200;

  function liveField(scope, name) {
    const root = scope && scope.querySelectorAll ? scope : document;
    return [...root.querySelectorAll(`[name="${name}"]`)].find(node => !node.disabled) || null;
  }

  function liveValue(scope, name) {
    const field = liveField(scope, name);
    return field ? field.value : "";
  }

  function optionLabel(select, value) {
    const option = [...select.options].find(item => item.value === value);
    return option ? option.textContent : "";
  }

  function restoreSelect(root, select) {
    select.hidden = false;
    select.disabled = false;
    const extras = root.querySelectorAll(".combobox-input, .combobox-list, .combobox-status, input[type=hidden][data-combobox-hidden]");
    extras.forEach(node => node.remove());
    const hint = root.querySelector(".combobox-hint");
    if (hint) hint.hidden = false;
  }

  function enhance(root) {
    const select = root.querySelector("select");
    if (!select || root.dataset.enhanced === "1") return;
    const endpoint = root.dataset.endpoint;
    if (!endpoint) return;
    const name = select.name;
    const dependsOn = root.dataset.dependsOn || "";
    root.dataset.enhanced = "1";

    const hidden = document.createElement("input");
    hidden.type = "hidden";
    hidden.name = name;
    hidden.dataset.comboboxHidden = "1";

    const input = document.createElement("input");
    input.type = "search";
    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-expanded", "false");
    input.setAttribute("autocomplete", "off");
    input.className = "combobox-input";
    if (select.dataset.requiredFor) {
      hidden.dataset.requiredFor = select.dataset.requiredFor;
      input.dataset.requiredFor = select.dataset.requiredFor;
    }

    const listId = `${name}-listbox`;
    input.setAttribute("aria-controls", listId);
    const list = document.createElement("ul");
    list.id = listId;
    list.setAttribute("role", "listbox");
    list.className = "combobox-list";
    list.hidden = true;

    const status = document.createElement("div");
    status.className = "combobox-status";
    status.setAttribute("aria-live", "polite");

    let items = [];
    let active = -1;
    let confirmedLabel = "";
    let timer = 0;
    let controller = null;

    function setValue(value, label, item) {
      hidden.value = value || "";
      select.value = value || "";
      input.value = label || "";
      confirmedLabel = label || "";
      if (item && item.next_chunk_index != null) hidden.dataset.nextChunkIndex = String(item.next_chunk_index);
      root.dispatchEvent(new CustomEvent("combobox:change", {
        bubbles: true,
        detail: { name, value: hidden.value, item: item || null },
      }));
    }

    function close() {
      list.hidden = true;
      input.setAttribute("aria-expanded", "false");
      input.removeAttribute("aria-activedescendant");
      active = -1;
    }

    function highlight() {
      [...list.children].forEach((child, index) => {
        child.setAttribute("aria-selected", index === active ? "true" : "false");
        if (index === active) {
          input.setAttribute("aria-activedescendant", child.id);
          child.scrollIntoView({ block: "nearest" });
        }
      });
    }

    function render(rows, truncated) {
      list.replaceChildren();
      items = rows;
      rows.forEach((row, index) => {
        const li = document.createElement("li");
        li.id = `${listId}-opt-${index}`;
        li.setAttribute("role", "option");
        li.setAttribute("aria-selected", "false");
        li.textContent = row.label;
        li.addEventListener("mousedown", event => {
          event.preventDefault();
          const value = row.source_id || row.ref || row.id || "";
          setValue(value, row.label, row);
          close();
        });
        list.append(li);
      });
      status.textContent = rows.length
        ? `${rows.length} resultado(s)${truncated ? ", há mais" : ""}`
        : "Nenhum resultado";
      list.hidden = false;
      input.setAttribute("aria-expanded", "true");
      active = rows.length ? 0 : -1;
      highlight();
    }

    function failOver() {
      restoreSelect(root, select);
    }

    async function search(query) {
      if (controller) controller.abort();
      controller = new AbortController();
      const params = new URLSearchParams();
      params.set("q", query);
      if (dependsOn) {
        const scope = liveValue(root.form || document, dependsOn);
        if (!scope) {
          render([], false);
          status.textContent = "Escolha a fonte primeiro";
          return;
        }
        params.set("source_id", scope);
      }
      try {
        const response = await fetch(`${endpoint}?${params.toString()}`, {
          signal: controller.signal,
        });
        if (response.status === 401) {
          list.replaceChildren();
          const login = document.createElement("li");
          login.setAttribute("role", "option");
          login.innerHTML = '<a href="/login">Faça login para buscar</a>';
          list.append(login);
          list.hidden = false;
          input.setAttribute("aria-expanded", "true");
          status.textContent = "Sessão expirada";
          return;
        }
        if (!response.ok) throw new Error("picker failed");
        const payload = await response.json();
        render(payload.items || [], Boolean(payload.truncated));
      } catch (error) {
        if (error && error.name === "AbortError") return;
        failOver();
      }
    }

    function schedule(query) {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => search(query), DEBOUNCE_MS);
    }

    select.hidden = true;
    select.disabled = true;
    const hint = root.querySelector(".combobox-hint");
    if (hint) hint.hidden = true;
    select.insertAdjacentElement("afterend", hidden);
    hidden.insertAdjacentElement("afterend", input);
    input.insertAdjacentElement("afterend", list);
    list.insertAdjacentElement("afterend", status);

    if (select.value) {
      setValue(select.value, optionLabel(select, select.value), {
        next_chunk_index: select.selectedOptions[0] && select.selectedOptions[0].dataset.nextChunkIndex,
      });
    }

    if (dependsOn && !hidden.value) {
      input.placeholder = "Escolha a fonte primeiro";
      input.disabled = !liveValue(root.form || document, dependsOn);
    }

    input.addEventListener("input", () => schedule(input.value));
    input.addEventListener("focus", () => schedule(input.value));
    input.addEventListener("keydown", event => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (list.hidden) schedule(input.value);
        else if (items.length) {
          active = (active + 1) % items.length;
          highlight();
        }
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        if (items.length) {
          active = (active - 1 + items.length) % items.length;
          highlight();
        }
      } else if (event.key === "Home") {
        if (items.length) { active = 0; highlight(); }
      } else if (event.key === "End") {
        if (items.length) { active = items.length - 1; highlight(); }
      } else if (event.key === "Enter") {
        if (!list.hidden && active >= 0 && items[active]) {
          event.preventDefault();
          const row = items[active];
          setValue(row.source_id || row.ref || row.id || "", row.label, row);
          close();
        }
      } else if (event.key === "Escape") {
        input.value = confirmedLabel;
        close();
      } else if (event.key === "Tab") {
        close();
      }
    });
    input.addEventListener("blur", () => {
      window.setTimeout(() => {
        if (!hidden.value) input.value = "";
        else input.value = confirmedLabel;
        close();
      }, 120);
    });

    if (dependsOn) {
      document.addEventListener("combobox:change", event => {
        if (!event.detail || event.detail.name !== dependsOn) return;
        input.disabled = !event.detail.value;
        input.placeholder = event.detail.value ? "Buscar…" : "Escolha a fonte primeiro";
        setValue("", "", null);
        if (event.detail.value) schedule("");
        else close();
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".combobox").forEach(enhance);
    document.getElementById("manual-note-form")?.dispatchEvent(new Event("combobox:ready"));
  });
})();
