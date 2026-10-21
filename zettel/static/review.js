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

document.addEventListener("click", function (event) {
  var button = event.target.closest("[data-review-confirm]");
  if (button && !confirmReviewAction(button)) event.preventDefault();
});
