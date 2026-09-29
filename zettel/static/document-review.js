(() => {
  const form = document.getElementById('document-review-form');
  if (!form) return;
  const type = document.getElementById('review-type');
  const status = document.getElementById('review-status');
  const reference = document.getElementById('review-reference');
  const button = document.getElementById('review-format');
  const more = document.getElementById('review-more');
  const fields = [...form.querySelectorAll('[data-review-field]')];

  function updateType() {
    let requiredInside = false;
    for (const label of fields) {
      const required = label.dataset.requiredTypes.split(' ').includes(type.value);
      label.querySelector('.required-mark').hidden = !required;
      if (required && more.contains(label)) requiredInside = true;
    }
    if (requiredInside) more.open = true;
  }
  type.addEventListener('change', updateType);
  updateType();

  form.addEventListener('input', (event) => {
    if (event.target === reference) return;
    status.textContent = 'Os campos foram alterados. Atualize a referência ou edite-a diretamente antes de confirmar.';
  });
  button.addEventListener('click', async () => {
    button.disabled = true;
    status.textContent = 'Montando a referência…';
    try {
      const response = await fetch(form.action + '/preview', {
        method: 'POST',
        body: new FormData(form),
      });
      const data = await response.json();
      if (!response.ok) {
        status.textContent = data.error || 'Não foi possível montar a referência.';
        return;
      }
      reference.value = data.abnt_reference;
      status.textContent = data.missing.length
        ? 'Ainda faltam: ' + data.missing.join(', ') + '.'
        : 'Referência atualizada. Confira o texto antes de confirmar.';
    } catch (_) {
      status.textContent = 'Não foi possível montar a referência.';
    } finally {
      button.disabled = false;
    }
  });
})();