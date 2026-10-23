var REVIEW_ITEM_SELECTOR =
  'input[type="checkbox"][name="edges"], input[type="checkbox"][name="chunk_ids"], input[type="checkbox"][name="concept_ids"]';

function reviewItemCheckboxes() {
  return Array.from(document.querySelectorAll(REVIEW_ITEM_SELECTOR));
}

function syncSelectAllState() {
  var toggle = document.querySelector("[data-select-all]");
  if (!toggle) return;
  var boxes = reviewItemCheckboxes();
  var checkedCount = boxes.filter(function (box) {
    return box.checked;
  }).length;
  toggle.checked = boxes.length > 0 && checkedCount === boxes.length;
  toggle.indeterminate = checkedCount > 0 && checkedCount < boxes.length;
}

function confirmReviewAction(button) {
  var action = button.form.querySelector("[name=action]").value;
  var message = action === "discard"
    ? "Apagar os chunks selecionados do banco? Isso não pode ser desfeito."
    : action === "force"
    ? "O próximo extract vai gerar a nota destes trechos, sem a trava de rejeição. Confirma?"
    : action === "keep_duplicate"
    ? "Manter os conceitos selecionados? Eles seguem para o connect."
    : action === "discard_duplicate"
    ? "Registrar os conceitos selecionados como duplicata?"
    : button.form.dataset.confirm;
  return window.confirm(message);
}

document.addEventListener("change", function (event) {
  if (event.target.matches("[data-select-all]")) {
    var checked = event.target.checked;
    reviewItemCheckboxes().forEach(function (box) {
      box.checked = checked;
    });
    event.target.indeterminate = false;
    return;
  }
  if (event.target.matches(REVIEW_ITEM_SELECTOR)) {
    syncSelectAllState();
  }
});

document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-review-confirm]");
  if (button && !confirmReviewAction(button)) event.preventDefault();
});
