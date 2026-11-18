/* Event lifecycle by calendar date in Asia/Seoul. No network requests. */
(function () {
  'use strict';
  var koreaDate = new Intl.DateTimeFormat('en-US', {
    timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit'
  });

  function calendarDay(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || '');
    if (!match) return null;
    var year = +match[1], month = +match[2], day = +match[3];
    var date = new Date(0);
    date.setUTCFullYear(year, month - 1, day);
    if (year < 1 || date.getUTCFullYear() !== year ||
        date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) return null;
    return date.getTime() / 864e5;
  }

  function refresh() {
    var parts = {};
    koreaDate.formatToParts(new Date()).forEach(function (part) { parts[part.type] = part.value; });
    var today = calendarDay(parts.year + '-' + parts.month + '-' + parts.day);
    if (today === null) return;
    document.querySelectorAll('[data-event-lifecycle]').forEach(function (el) {
      var start = calendarDay(el.getAttribute('data-start'));
      var end = calendarDay(el.getAttribute('data-end'));
      if (start === null || end === null || end < start) return;
      var ended = today > end;
      var state = ended ? 'ended' : (today < start ? 'upcoming' : 'ongoing');
      var label = ended ? 'Ended' : (today < start ? 'D-' + (start - today) :
        (today === start ? 'D-DAY' : 'Live now'));
      el.setAttribute('data-event-lifecycle', state);
      var status = el.querySelector('[data-event-status]');
      if (status) status.textContent = label;
      el.querySelectorAll('[data-event-ended-text]').forEach(function (node) {
        var text = node.getAttribute(ended ? 'data-event-ended-text' : 'data-event-active-text');
        if (text !== null) node.textContent = text;
      });
      el.querySelectorAll('[data-event-ended-only]').forEach(function (node) { node.hidden = !ended; });
      if (el.hasAttribute('data-event-card')) el.hidden = ended;
      if (el.hasAttribute('data-event-page')) {
        var prefix = ended ? 'data-ended-' : 'data-active-';
        var title = el.getAttribute(prefix + 'title');
        var description = el.getAttribute(prefix + 'description');
        if (title) {
          document.title = title.indexOf('HANSET') === 0 ? title : title + ' · HANSET';
          var ogTitle = document.querySelector('meta[property="og:title"]');
          if (ogTitle) ogTitle.setAttribute('content', document.title);
        }
        if (description) {
          document.querySelectorAll('meta[name="description"], meta[property="og:description"]').forEach(function (node) {
            node.setAttribute('content', description);
          });
        }
      }
    });
    document.querySelectorAll('[data-events-empty]').forEach(function (empty) {
      var cards = empty.parentNode.querySelectorAll('[data-event-card]');
      empty.hidden = Array.prototype.some.call(cards, function (card) { return !card.hidden; });
    });
  }

  refresh();
  window.addEventListener('pageshow', refresh);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'visible') refresh();
  });
  window.setInterval(refresh, 60000);
})();
