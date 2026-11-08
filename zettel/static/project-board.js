// Drag and drop on the Mesa (ADR-067). A shortcut only: every move is the same
// form POST the arrow buttons send, so the page works without this script.
(function () {
  const form = document.querySelector("[data-desk-move]");
  if (!form) return;
  let dragged = null;

  document.querySelectorAll("[draggable=true][data-block]").forEach(function (item) {
    item.addEventListener("dragstart", function (event) {
      dragged = item;
      item.classList.add("dragging");
      event.dataTransfer.effectAllowed = "move";
      event.dataTransfer.setData("text/plain", item.dataset.section + "." + item.dataset.block);
    });
    item.addEventListener("dragend", function () {
      item.classList.remove("dragging");
      dragged = null;
    });
  });

  function dropTarget(event) {
    const item = event.target.closest("[data-block]");
    const section = event.target.closest("[data-section]");
    return { item: item && item !== dragged ? item : null, section: section };
  }

  document.querySelectorAll(".desk-section").forEach(function (section) {
    section.addEventListener("dragover", function (event) {
      if (!dragged) return;
      event.preventDefault();
      section.classList.add("drop-over");
    });
    section.addEventListener("dragleave", function () {
      section.classList.remove("drop-over");
    });
    section.addEventListener("drop", function (event) {
      if (!dragged) return;
      event.preventDefault();
      section.classList.remove("drop-over");
      const target = dropTarget(event);
      form.elements.secao.value = dragged.dataset.section;
      form.elements.bloco.value = dragged.dataset.block;
      form.elements.destino.value = section.dataset.section;
      form.elements.antes.value = target.item ? target.item.dataset.block : "";
      form.submit();
    });
  });
})();
