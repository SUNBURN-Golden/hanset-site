/* Injected in the isolated world only after the worker has granted begin. */
globalThis.HANSETExtract = function (expectedURL, documentKey) {
  const P = globalThis.HANSETPrivacy;
  if (P.safeURL(location.href) !== expectedURL) throw Error('document_mismatch');
  const region = document.querySelector('main, article');
  if (!region) throw Error('missing_minimal_region');
  const blocked = 'input,textarea,[contenteditable],form,iframe,script,style,template,nav,footer,header,[hidden],[aria-hidden="true"],[class*="private"],[class*="profile"],[class*="comment"],[class*="message"],[class*="chat"]';
  if (region.closest(blocked) || !region.getClientRects().length || getComputedStyle(region).display === 'none' || getComputedStyle(region).visibility === 'hidden') throw Error('private_region');
  const copy = region.cloneNode(true);
  const originals = [region, ...region.querySelectorAll('*')];
  const clones = [copy, ...copy.querySelectorAll('*')];
  for (let i = 0; i < originals.length; i++) {
    const e = originals[i];
    const style = getComputedStyle(e);
    if (e.matches('input,textarea,[contenteditable],form,iframe,script,style,template,nav,footer,header,[hidden],[aria-hidden="true"],[class*="private"],[class*="profile"],[class*="comment"],[class*="message"],[class*="chat"]')
        || style.display === 'none' || style.visibility === 'hidden') clones[i].remove();
  }
  const excerpt = P.cleanText(copy.textContent.slice(0, 24000)).slice(0, 2000);
  if (!excerpt) throw Error('empty_region');
  if (P.safeURL(location.href) !== expectedURL) throw Error('document_mismatch');
  return {title: P.cleanText(document.title).slice(0, 240), excerpt, locator: region.tagName.toLowerCase(),
    facts: [], published_at: null, document_key: documentKey, final_url: expectedURL};
};
