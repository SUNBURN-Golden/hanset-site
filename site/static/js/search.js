/* Local search over published, escaped HTML records. No fetch or storage. */
(function () {
  'use strict';
  var input = document.getElementById('home-q');
  var results = document.getElementById('search-results');
  if (!input || !results) return;
  var records = Array.from(results.querySelectorAll('[data-search]'));
  var count = document.getElementById('search-count');
  function normalize(value) { return value.trim().toLowerCase().replace(/\s+/g, ' '); }
  function apply(writeURL) {
    var query = normalize(input.value), total = 0;
    records.forEach(function (record) {
      var matches = !!query && query.split(' ').every(function (word) {
        return normalize(record.dataset.search).includes(word);
      });
      record.hidden = !matches;
      if (matches) total++;
    });
    results.hidden = !query;
    count.textContent = !query ? '' : total === 0 ? 'No matching places.' : total + (total === 1 ? ' place' : ' places');
    if (writeURL) {
      var params = new URLSearchParams();
      if (query) params.set('q', input.value.trim());
      history.replaceState(null, '', location.pathname + (params.toString() ? '?' + params.toString() : '') + location.hash);
    }
  }
  function restore() { input.value = new URLSearchParams(location.search).get('q') || ''; apply(false); }
  input.addEventListener('input', function () { apply(true); });
  input.form.addEventListener('submit', function (event) { event.preventDefault(); apply(true); });
  window.addEventListener('popstate', restore);
  restore();
})();
