(() => {
  const form = document.getElementById('document-harvest-form');
  if (!form) return;
  const file = document.getElementById('selected-document');
  const settings = document.getElementById('document-settings');
  const paging = document.getElementById('document-paging');
  const mode = document.getElementById('paging-mode');
  const manual = document.getElementById('document-page-inputs');
  const startFile = document.getElementById('content-start-file');
  const startBook = document.getElementById('content-start-book');
  const hint = document.getElementById('paging-hint');
  const summary = document.getElementById('document-summary');
  const button = document.getElementById('document-start');

  function update() {
    const option = file.selectedOptions[0];
    const selected = !!file.value;
    const isPdf = selected && option.dataset.pdf === '1';
    settings.disabled = !selected;
    paging.disabled = !isPdf;
    manual.disabled = !isPdf || mode.value !== 'manual';
    startFile.required = !manual.disabled;
    startBook.required = !manual.disabled;
    button.disabled = !selected;
    hint.textContent = !isPdf
      ? 'Markdown e TXT não têm páginas de arquivo; estas opções só se aplicam a PDF.'
      : mode.value === 'manual'
        ? 'Informe as duas páginas. Páginas anteriores ao início informado não entram nos chunks.'
        : mode.value === 'first'
          ? 'Sem detecção: a página 1 do arquivo corresponde à página impressa 1.'
          : 'Sem detecção confiável, o padrão usa a página 1.';
    if (!selected) {
      summary.textContent = file.options.length > 1
        ? 'Selecione um arquivo para revisar as opções.'
        : 'Envie um arquivo primeiro; as opções ficam disponíveis quando houver um documento pendente.';
      return;
    }
    const duplicate = form.querySelector('[name="duplicate_action"]');
    const choice = duplicate.selectedOptions[0].textContent.trim();
    let pages = 'sem paginação de PDF';
    if (isPdf) {
      if (mode.value === 'auto') pages = 'detecção automática de páginas';
      if (mode.value === 'first') pages = 'começar na página 1';
      if (mode.value === 'manual') {
        pages = startFile.value
          ? `arquivo p.${startFile.value} = impressa p.${startBook.value || '?'}`
          : 'informe a página inicial do arquivo';
      }
    }
    const extras = [];
    if (form.querySelector('[name="skip_biblio"]').checked) extras.push('bibliografia incompleta permitida');
    if (form.querySelector('[name="dump_chunks"]').checked) extras.push('diagnóstico dos chunks');
    if (form.querySelector('[name="dump_extraction"]').checked) extras.push('Markdown extraído');
    summary.textContent = `${option.dataset.name} · ${choice} · ${pages}${extras.length ? ' · ' + extras.join(' · ') : ''}.`;
  }

  form.addEventListener('input', update);
  form.addEventListener('change', update);
  update();
})();