/* Shared Home/Finder explorer. Cards, map pins and previews are built into HTML.
   URL state supports deep links; no storage or network access. */
(function () {
  'use strict';
  var list = document.getElementById('finder-list');
  if (!list) return;
  var $ = function (id) { return document.getElementById(id); };
  var cards = Array.from(list.querySelectorAll('.venue-card'));
  var pins = Array.from(document.querySelectorAll('[data-pin]'));
  var regions = Array.from(document.querySelectorAll('[data-map-city]'));
  var form = document.querySelector('.filters');
  var preview = $('map-preview'), selected = null;
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
      if (fields[key].type === 'checkbox') fields[key].checked = st[key] === '1';
      else fields[key].value = st[key];
    });
  }
  function selectVenue(slug) {
    var template = $('preview-' + slug);
    if (selected !== slug) {
      preview.replaceChildren();
      if (template) preview.appendChild(template.content.cloneNode(true));
      else {
        var message = document.createElement('p');
        message.textContent = slug === null ? 'Select a pin to preview.' : 'No venues match these filters. Try fewer filters or start over.';
        preview.appendChild(message);
      }
    }
    selected = slug;
    $('map-selection').classList.toggle('has-selection', !!template);
    pins.forEach(function (pin) { pin.setAttribute('aria-pressed', String(pin.dataset.pin === slug)); });
  }
  pins.forEach(function (pin) {
    pin.addEventListener('click', function () {
      selectVenue(pin.dataset.pin);
    });
    pin.addEventListener('keydown', function (event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        selectVenue(pin.dataset.pin);
      } else if (['ArrowRight', 'ArrowDown', 'ArrowLeft', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        var visible = pins.filter(function (p) { return !p.hasAttribute('hidden') && !p.closest('[data-map-city]').hasAttribute('hidden'); });
        var index = visible.indexOf(pin);
        var next = event.key === 'Home' ? 0 : event.key === 'End' ? visible.length - 1 :
          (index + (event.key === 'ArrowRight' || event.key === 'ArrowDown' ? 1 : -1) + visible.length) % visible.length;
        if (visible[next]) visible[next].focus();
      }
    });
  });
  form.addEventListener('submit', function (event) { event.preventDefault(); });
  ['city', 'type'].forEach(function (group) {
    document.querySelectorAll('[data-seg="' + group + '"] button').forEach(function (button) {
      button.addEventListener('click', function () { st[group] = button.dataset.v; apply(true); });
    });
  });
  Object.keys(fields).forEach(function (key) {
    var field = fields[key];
    field.addEventListener(key === 'q' ? 'input' : 'change', function () {
      st[key] = field.type === 'checkbox' ? (field.checked ? '1' : '') : field.value;
      apply(true);
    });
  });
  document.querySelectorAll('[data-reset]').forEach(function (button) {
    button.addEventListener('click', function () {
      Object.keys(st).forEach(function (key) { st[key] = ''; });
      syncFields();
      selectVenue(null);
      apply(true);
    });
  });
  function apply(writeURL) {
    var ordered = cards.slice();
    if (st.sort === 'date') ordered.sort(function (a, b) { return b.dataset.date.localeCompare(a.dataset.date); });
    else if (st.sort === 'name') ordered.sort(function (a, b) { return a.dataset.title.localeCompare(b.dataset.title, 'en'); });
    ordered.forEach(function (card) { list.appendChild(card); });
    var visible = new Set(), query = st.q.trim().toLowerCase();
    ordered.forEach(function (card) {
      var d = card.dataset;
      var matches = ['city', 'type', 'sub', 'sunday'].every(function (key) { return !st[key] || d[key] === st[key]; }) &&
        ['price', 'hyrox', 'cf'].every(function (key) { return st[key] !== '1' || d[key] === '1'; }) &&
        (!query || d.name.includes(query));
      card.hidden = !matches;
      if (matches) visible.add(d.slug);
    });
    pins.forEach(function (pin) {
      // SVG elements need the hidden attribute, not HTMLElement.hidden.
      var hidden = !visible.has(pin.dataset.pin);
      pin.toggleAttribute('hidden', hidden);
      pin.setAttribute('tabindex', hidden ? '-1' : '0');
    });
    regions.forEach(function (region) {
      // SVG hidden must use attributes, just like the pins.
      var inactive = region.dataset.mapCity !== (st.city || 'all');
      region.toggleAttribute('hidden', inactive);
      region.querySelectorAll('[data-pin]').forEach(function (pin) {
        pin.setAttribute('tabindex', !inactive && visible.has(pin.dataset.pin) ? '0' : '-1');
      });
    });
    document.querySelectorAll('[data-seg] button').forEach(function (button) {
      var group = button.closest('[data-seg]').dataset.seg;
      button.setAttribute('aria-pressed', String(button.dataset.v === st[group]));
    });
    $('map-view').textContent = st.city ? st.city.charAt(0).toUpperCase() + st.city.slice(1) : 'All Korea';
    $('f-count').textContent = visible.size + (visible.size === 1 ? ' place' : ' places');
    $('f-empty').hidden = visible.size !== 0;
    var moreCount = ['sub', 'sunday', 'price', 'hyrox', 'cf', 'sort'].filter(function (key) { return !!st[key]; }).length;
    $('f-more-count').textContent = moreCount ? '(' + moreCount + ' applied)' : '';
    // Only an explicit pin activation expands a preview. Never select a result
    // as a side effect of loading, filtering, resetting or restoring the URL.
    if (!visible.size) selectVenue('');
    else if (!visible.has(selected)) selectVenue(null);
    if (writeURL) {
      var params = new URLSearchParams();
      Object.keys(st).forEach(function (key) { if (st[key]) params.set(key, st[key]); });
      var search = params.toString();
      history.replaceState(null, '', location.pathname + (search ? '?' + search : '') + location.hash);
    }
  }
  window.addEventListener('popstate', function () { readState(); apply(false); });
  readState();
  apply(false);
})();
