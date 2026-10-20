(function () {
  var outline = document.querySelector('input[name="outline_only"]');
  if (!outline) return;
  var reviews = document.querySelectorAll("[data-review]");
  function sync() {
    reviews.forEach(function (label) { label.hidden = outline.checked; });
  }
  outline.addEventListener("change", sync);
  sync();
})();
