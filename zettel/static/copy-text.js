(() => {
  async function writeClipboard(text) {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_) {
        // A preview frame may deny the async clipboard; the legacy path still works.
      }
    }
    const field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.top = "0";
    field.style.left = "0";
    document.body.appendChild(field);
    field.focus();
    field.select();
    field.setSelectionRange(0, field.value.length);
    try {
      if (!document.execCommand("copy")) throw new Error("clipboard");
    } finally {
      field.remove();
    }
  }

  function sourceText(source) {
    if (!source) return "";
    const text = "value" in source ? source.value : source.textContent;
    return (text || "").replace(/\n$/, "");
  }

  document.addEventListener("click", async (event) => {
    const button = event.target.closest(".copy-text");
    if (!button || button.disabled) return;
    event.preventDefault();
    event.stopPropagation();
    const status = button.parentElement.querySelector(".copy-status");
    const text = sourceText(document.getElementById(button.dataset.copy));
    if (!text) {
      if (status) {
        status.textContent = "Nada para copiar.";
        status.classList.add("error");
      }
      return;
    }
    button.disabled = true;
    if (status) {
      status.textContent = "Copiando…";
      status.classList.remove("error");
    }
    try {
      await writeClipboard(text);
      if (status) status.textContent = "Copiado.";
    } catch (_) {
      if (status) {
        status.textContent = "Não foi possível copiar.";
        status.classList.add("error");
      }
    } finally {
      button.disabled = false;
    }
  });
})();
