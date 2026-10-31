(() => {
  const form = document.getElementById('manual-note-form');
  const options = [...document.querySelectorAll('.type-option')];
  const useLlm = document.getElementById('use-llm');
  const force = document.getElementById('force-overwrite');
  const preview = document.getElementById('lit-preview');
  function mode() {
    const type = form.querySelector('input[name="note_type"]:checked').value;
    if (type === 'SRC') return 'SRC';
    if (type === 'LIT') {
      const g = form.querySelector('input[name="granular"]:checked');
      return g && g.value === '1' ? 'LIT_GRANULAR' : 'LIT_INDEX';
    }
    const origin = form.querySelector('input[name="ztl_origin"]:checked');
    return origin && origin.value === 'from_lit' ? 'ZTL_FROM_LIT' : 'ZTL_BLANK';
  }
  function slug(text) {
    return (text || '').normalize('NFKD').replace(/[\u0300-\u036f]/g, '')
      .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'nota';
  }
  function fieldValue(name) {
    const live = [...form.querySelectorAll(`[name="${name}"]`)].find(node => !node.disabled);
    return live ? live.value : '';
  }
  function updatePreview() {
    if (!preview) return;
    const on = mode() === 'LIT_GRANULAR';
    preview.hidden = !on;
    if (!on) return;
    const sourceId = fieldValue('source_id');
    const index = parseInt(form.querySelector('[name="chunk_index"]').value || '1', 10);
    const page = form.querySelector('[name="page_number"]').value;
    const topic = fieldValue('title');
    const padded = String(index).padStart(4, '0');
    const pageTok = page ? ('p' + String(page).padStart(3, '0')) : ('c' + padded);
    const citekey = (sourceId || '').replace(/^@/, '') || 'Citekey';
    preview.textContent = 'Arquivo: LIT - ' + citekey + ' - ' + pageTok + ' - ' + slug(topic) + '-' + padded + '.md';
  }
  function update() {
    const current = mode();
    form.dataset.mode = current;
    const type = form.querySelector('input[name="note_type"]:checked').value;
    options.forEach(option => option.classList.toggle('selected', option.querySelector('input').value === type));
    for (const el of form.querySelectorAll('[data-types]')) {
      const on = el.dataset.types.split(/\s+/).includes(current);
      el.hidden = !on;
      for (const input of el.querySelectorAll('input, select, textarea, button')) {
        if (input.matches('select') && input.closest('.combobox')?.dataset.enhanced === '1') {
          input.disabled = true;
          input.required = false;
          continue;
        }
        if (input.dataset.locked === '1') {
          input.disabled = true;
          continue;
        }
        input.disabled = !on;
        if (input.dataset.requiredFor)
          input.required = on && input.dataset.requiredFor.split(/\s+/).includes(current);
      }
    }
    const titleLabel = document.getElementById('title-label');
    const titleInput = [...form.querySelectorAll('[name="title"]')].find(node => !node.disabled);
    if (current === 'ZTL_BLANK') {
      if (titleLabel) titleLabel.textContent = 'Tese / título da ideia';
      if (titleInput) titleInput.placeholder = 'A ideia em uma frase';
    } else if (current === 'LIT_INDEX') {
      if (titleLabel) titleLabel.textContent = 'Título do índice';
      if (titleInput) titleInput.placeholder = 'Título da página-índice desta fonte';
    } else if (current !== 'LIT_GRANULAR') {
      if (titleLabel) titleLabel.textContent = 'Título';
      if (titleInput) titleInput.placeholder = 'Título da obra, como na capa';
    }
    form.querySelectorAll('.combobox[data-depends-on]').forEach(root => {
      const input = root.querySelector('.combobox-input');
      if (!input) return;
      const sectionOn = !root.closest('[hidden]');
      const ownerVal = fieldValue(root.dataset.dependsOn);
      input.disabled = !sectionOn || !ownerVal;
      if (sectionOn) input.placeholder = ownerVal ? 'Buscar…' : 'Escolha a fonte primeiro';
    });
    if (force && useLlm) force.disabled = Boolean(useLlm.checked) && current === 'ZTL_FROM_LIT';
    updatePreview();
  }
  form.querySelectorAll('input[name="note_type"], input[name="granular"], input[name="ztl_origin"]').forEach(input => input.addEventListener('change', update));
  form.addEventListener('input', updatePreview);
  if (useLlm) useLlm.addEventListener('change', update);
  form.addEventListener('combobox:change', event => {
    if (event.detail && event.detail.name === 'source_id') {
      const next = event.detail.item && event.detail.item.next_chunk_index;
      const indexInput = form.querySelector('[name="chunk_index"]');
      if (next && indexInput) indexInput.value = next;
      updatePreview();
    }
    update();
  });
  form.addEventListener('combobox:ready', update);
  update();

  const biblioStatus = document.getElementById('biblio-status');
  const ADVANCED = ['publisher', 'place', 'doi', 'url', 'journal', 'edition', 'institution', 'pages'];
  function setBiblioStatus(text, kind) {
    if (!biblioStatus) return;
    biblioStatus.hidden = !text;
    biblioStatus.textContent = text || '';
    biblioStatus.dataset.kind = kind || '';
  }
  function applyBiblio(payload) {
    const fields = ['abnt_reference', 'title', 'document_type', ...ADVANCED];
    for (const name of fields) {
      if (payload[name] == null || payload[name] === '') continue;
      const el = form.querySelector(`[name="${name}"]`);
      if (el) el.value = payload[name];
    }
    if (Array.isArray(payload.authors) && payload.authors.length) {
      const authors = form.querySelector('[name="authors"]');
      if (authors) authors.value = payload.authors.join('\n');
    }
    if (payload.year != null && payload.year !== '') {
      const year = form.querySelector('[name="year"]');
      if (year) year.value = payload.year;
    }
  }
  async function previewBiblio(enrich) {
    if (mode() !== 'SRC') return;
    setBiblioStatus(enrich ? 'Completando com o LLM…' : 'Montando a referência…', '');
    const data = new FormData(form);
    if (enrich) data.set('enrich', '1');
    else data.delete('enrich');
    const formatBtn = document.getElementById('biblio-format');
    const enrichBtn = document.getElementById('biblio-enrich');
    if (formatBtn) formatBtn.disabled = true;
    if (enrichBtn && enrichBtn.dataset.locked !== '1') enrichBtn.disabled = true;
    try {
      const response = await fetch('/notes/new/biblio-preview', { method: 'POST', body: data });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) {
        setBiblioStatus(payload.error || 'Não foi possível montar a referência.', 'error');
        return;
      }
      applyBiblio(payload);
      if (payload.warning) setBiblioStatus(payload.warning, 'error');
      else setBiblioStatus(
        payload.enriched
          ? 'Campos preenchidos. Revise e crie a nota se estiver certo.'
          : 'Referência montada. Revise e crie a nota se estiver certo.',
        'ok',
      );
    } catch (err) {
      setBiblioStatus('Não foi possível montar a referência.', 'error');
    } finally {
      update();
    }
  }
  document.getElementById('biblio-format')?.addEventListener('click', () => previewBiblio(false));
  document.getElementById('biblio-enrich')?.addEventListener('click', () => previewBiblio(true));
})();
