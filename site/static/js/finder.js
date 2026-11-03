/* Shared Home/Finder explorer. Venue cards are built into HTML.
   URL state supports deep links; no storage or network access. */
(function () {
  'use strict';
  var list = document.getElementById('finder-list');
  if (!list) return;
  var $ = function (id) { return document.getElementById(id); };
  var cards = Array.from(list.querySelectorAll('.venue-card'));
  var form = document.querySelector('.filters');
  var fields = {sub: $('f-sub'), sort: $('f-sort'), q: $('f-q'), sunday: $('f-sunday'), price: $('f-price'), hyrox: $('f-hyrox'), cf: $('f-cf')};
  var choices = {city: ['', 'seoul', 'busan', 'jeju', 'gyeonggi'], type: ['', 'box', 'sauna'], sub: ['', 'hotel', 'boutique', 'spa'], sort: ['', 'date', 'name'], sunday: ['', 'yes', 'no', 'unk'], price: ['', '1'], hyrox: ['', '1'], cf: ['', '1']};
  var st;
  function readState() {
    var params = new URLSearchParams(location.search);
    st = {q: params.get('q') || ''};
    Object.keys(choices).forEach(function (key) {
      var value = params.get(key) || '';
      st[key] = choices[key].includes(value) ? value : '';
    });
    syncFields();
  }
  function syncFields() {
    Object.keys(fields).forEach(function (key) {
      if (!fields[key]) return;
      if (fields[key].type === 'checkbox') fields[key].checked = st[key] === '1';
      else fields[key].value = st[key];
    });
  }
  if (form) form.addEventListener('submit', function (event) { event.preventDefault(); });
  ['city', 'type'].forEach(function (group) {
    document.querySelectorAll('[data-seg="' + group + '"] button').forEach(function (button) {
      button.addEventListener('click', function () { st[group] = button.dataset.v; apply(true); });
    });
  });
  Object.keys(fields).forEach(function (key) {
    var field = fields[key];
    if (!field) return;
    field.addEventListener(key === 'q' ? 'input' : 'change', function () {
      st[key] = field.type === 'checkbox' ? (field.checked ? '1' : '') : field.value;
      apply(true);
    });
  });
  document.querySelectorAll('[data-reset]').forEach(function (button) {
    button.addEventListener('click', function () {
      Object.keys(st).forEach(function (key) { st[key] = ''; });
      syncFields();
      apply(true);
    });
  });
  function apply(writeURL) {
    var ordered = cards.slice();
    if (st.sort === 'date') ordered.sort(function (a, b) { return b.dataset.date.localeCompare(a.dataset.date); });
    else if (st.sort === 'name') ordered.sort(function (a, b) { return a.dataset.title.localeCompare(b.dataset.title, 'en'); });
    ordered.forEach(function (card) { list.appendChild(card); });
    var count = 0, query = st.q.trim().toLowerCase();
    ordered.forEach(function (card) {
      var d = card.dataset;
      var matches = ['city', 'type', 'sub', 'sunday'].every(function (key) { return !st[key] || d[key] === st[key]; }) &&
        ['price', 'hyrox', 'cf'].every(function (key) { return st[key] !== '1' || d[key] === '1'; }) &&
        (!query || d.name.includes(query));
      card.hidden = !matches;
      if (matches) count++;
    });
    document.querySelectorAll('[data-seg] button').forEach(function (button) {
      var group = button.closest('[data-seg]').dataset.seg;
      button.setAttribute('aria-pressed', String(button.dataset.v === st[group]));
    });
    if ($('f-count')) $('f-count').textContent = count + (count === 1 ? ' place' : ' places');
    if ($('f-empty')) $('f-empty').hidden = count !== 0;
    var moreCount = ['sub', 'sunday', 'price', 'hyrox', 'cf', 'sort'].filter(function (key) { return !!st[key]; }).length;
    if ($('f-more-count')) $('f-more-count').textContent = moreCount ? '(' + moreCount + ' applied)' : '';
    if (writeURL) {
      var params = new URLSearchParams();
      Object.keys(st).forEach(function (key) { if (st[key]) params.set(key, st[key]); });
      var search = params.toString();
      history.replaceState(null, '', location.pathname + (search ? '?' + search : '') + location.hash);
    }
  }
  window.addEventListener('popstate', function () { readState(); apply(false); });
  // Keep the open select list below its field so it never covers the labels above (mobile).
  if (window.CSS && CSS.supports('appearance', 'base-select')) {
    document.querySelectorAll('.filters select').forEach(function (sel) {
      sel.addEventListener('pointerdown', function () {
        var room = window.innerHeight - sel.getBoundingClientRect().bottom;
        var need = sel.options.length * 48 + 24;
        if (room < need) window.scrollBy(0, need - room);
      });
    });
  }
  readState();
  apply(false);
})();
