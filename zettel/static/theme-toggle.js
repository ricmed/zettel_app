(() => {
  const button = document.getElementById('theme-toggle');
  const label = button.querySelector('.theme-label');
  const apply = theme => {
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme;
    localStorage.setItem('zettel-theme', theme);
    const dark = theme === 'dark';
    label.textContent = dark ? 'Claro' : 'Escuro';
    button.setAttribute('aria-label', dark ? 'Ativar modo claro' : 'Ativar modo escuro');
  };
  apply(document.documentElement.dataset.theme || 'light');
  button.addEventListener('click', () => {
    apply(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');
  });
})();
