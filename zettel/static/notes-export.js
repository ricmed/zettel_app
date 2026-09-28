(() => {
  async function copyText(text) {
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (_) {
        // The preview iframe may deny clipboard permission; try the legacy copy path.
      }
    }
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.position = 'fixed';
    field.style.opacity = '0';
    document.body.appendChild(field);
    field.select();
    try {
      if (!document.execCommand('copy')) throw new Error('clipboard');
    } finally {
      field.remove();
    }
  }

  document.addEventListener('click', async (event) => {
    const button = event.target.closest('.copy-markdown');
    if (!button || button.disabled) return;
    const status = button.parentElement.querySelector('.copy-status');
    button.disabled = true;
    status.textContent = 'Copiando…';
    status.classList.remove('error');
    try {
      const response = await fetch(button.dataset.url, { credentials: 'same-origin' });
      if (!response.ok || !response.headers.get('content-type')?.includes('text/markdown')) {
        throw new Error('markdown unavailable');
      }
      await copyText(await response.text());
      status.textContent = 'Markdown copiado.';
    } catch (_) {
      status.textContent = 'Não foi possível copiar. Verifique se o arquivo existe no vault.';
      status.classList.add('error');
    } finally {
      button.disabled = false;
    }
  });
})();