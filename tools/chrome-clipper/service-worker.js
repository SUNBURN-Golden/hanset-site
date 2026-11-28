importScripts('privacy-filter.js', 'clipper.js');
const LOCAL = 'http://127.0.0.1:8766';
const LOCAL_PERMISSION = 'http://127.0.0.1/*';
const APPROVED_MANIFEST_ORIGINS = chrome.runtime.getManifest().optional_host_permissions
  .filter(p => p.startsWith('https://') && !p.slice(0, -2).includes('*')).map(p => p.slice(0, -2));
let serial = Promise.resolve(); // optimization only; storage is the source of recovery truth
let needsLegacyPrune = true;
let consentPump;
function startConsentPump() {
  if (consentPump) return;
  consentPump = (async () => {
    // No extraction happens here. A killed/offline service worker cannot answer
    // new challenges, so unattended execution stops at its next request.
    const until = Date.now() + 125000;
    let answers = [];
    while (Date.now() < until) {
      try {
        const result = await request('/consent', {answers});
        answers = [];
        for (const check of result.checks) {
          const allowed = APPROVED_MANIFEST_ORIGINS.includes(check.origin)
            && await chrome.permissions.contains({origins: [check.origin + '/*']});
          answers.push({id: check.id, allowed});
        }
        await new Promise(resolve => setTimeout(resolve, 250));
      } catch { break; }
    }
  })().finally(() => { consentPump = undefined; });
}
async function request(path, data) {
  if (!await chrome.permissions.contains({origins: [LOCAL_PERMISSION]})) throw Error('offline');
  let response;
  try {
    response = await fetch(LOCAL + path, {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(data), credentials: 'omit', redirect: 'error', signal: AbortSignal.timeout(5000)});
  } catch { throw Error('offline'); }
  let result;
  try { result = await response.json(); } catch { throw Error('offline'); }
  if (path === '/result') return {status: response.status, body: result};
  if (!response.ok) throw Error(result?.reason || 'worker_rejected');
  return result;
}
async function jobs() {
  const queue = (await chrome.storage.local.get('jobs')).jobs || {};
  if (needsLegacyPrune) {
    const terminalIds = Object.keys(queue).filter(id => ['acked', 'expired'].includes(queue[id].state));
    if (terminalIds.length) {
      for (const id of terminalIds) delete queue[id];
      await chrome.storage.local.set({jobs: queue});
    }
    needsLegacyPrune = false;
  }
  return queue;
}
async function save(job) {
  const queue = await jobs();
  queue[job.clip_id] = job;
  await chrome.storage.local.set({jobs: queue});
  return job;
}
async function removeJobs(...clipIds) {
  const queue = await jobs();
  const existing = clipIds.filter(id => Object.hasOwn(queue, id));
  if (!existing.length) return;
  for (const id of existing) delete queue[id];
  await chrome.storage.local.set({jobs: queue});
}
const clipper = new HANSETClipper({request, save, remove: removeJobs, extract: async job => {
  const target = {tabId: job.tab_id, documentIds: [job.document_id]};
  await chrome.scripting.executeScript({target, files: ['privacy-filter.js', 'extract.js']});
  const [result] = await chrome.scripting.executeScript({target,
    func: (url, key) => globalThis.HANSETExtract(url, key), args: [job.url, job.document_id]});
  if (!result || result.documentId !== job.document_id || !result.result) throw Error('document_mismatch');
  return result.result;
}});
async function currentIntent(tab) {
  if (!tab) [tab] = await chrome.tabs.query({active: true, currentWindow: true});
  const url = HANSETPrivacy.safeURL(tab.url);
  // Metadata only. No title, text, input value or quote is read before reservation.
  const [meta] = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: () => location.href});
  if (HANSETPrivacy.safeURL(meta.result) !== url) throw Error('document_mismatch');
  const old = Object.values(await jobs()).find(j => j.tab_id === tab.id && j.document_id === meta.documentId);
  if (old) return old;
  return save({clip_id: crypto.randomUUID(), tab_id: tab.id, document_id: meta.documentId, url, state: 'intent'});
}
async function handle(message) {
  if (message.action === 'clip') {
    const job = await currentIntent();
    return clipper.start(job);
  }
  if (message.action === 'resume') {
    for (const job of Object.values(await jobs())) await clipper.resume(job);
    await reconcileGrants();
    startConsentPump();
    return {state: 'resumed'};
  }
  if (message.action === 'retry') {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const tabJobs = Object.values(await jobs()).filter(job => job.tab_id === tab.id);
    const ready = tabJobs.find(job => job.state === 'ready');
    if (ready) return clipper.deliver(ready);

    for (const job of tabJobs) {
      if (job.reservation) {
        try {
          const result = await request('/revoke', {reservation: job.reservation});
          if (result?.drained !== true) return {state: 'uncertain'};
        } catch { return {state: 'offline'}; }
      }
    }
    if (tabJobs.length) await removeJobs(...tabJobs.map(job => job.clip_id));
    return clipper.start(await currentIntent(tab));
  }
  if (message.action === 'approve') {
    const origin = new URL(HANSETPrivacy.safeURL(message.origin + '/')).origin;
    if (!APPROVED_MANIFEST_ORIGINS.includes(origin) || !await chrome.permissions.contains({origins: [origin + '/*']})) throw Error('permission_not_granted');
    const policies = await request('/policies', {});
    if (!policies.origins.includes(origin)) throw Error('access_not_approved');
    await request('/grant', {origin, enabled: true});
    await chrome.storage.local.set({['grant:' + origin]: true});
    return {state: 'approved'};
  }
  if (message.action === 'submit') {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const url = HANSETPrivacy.safeURL(tab.url), origin = new URL(url).origin;
    if (!await chrome.permissions.contains({origins: [origin + '/*']})) throw Error('permission_not_granted');
    startConsentPump();
    return request('/submit', {url});
  }
  return {state: 'unknown_action'};
}
async function reconcileGrants() {
  const entries = await chrome.storage.local.get(null);
  for (const [key, enabled] of Object.entries(entries)) {
    if (!key.startsWith('grant:') || !enabled) continue;
    const origin = key.slice(6);
    if (!await chrome.permissions.contains({origins: [origin + '/*']})) {
      await request('/grant', {origin, enabled: false});
      await chrome.storage.local.remove(key);
    }
  }
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id || sender.url !== chrome.runtime.getURL('popup.html')) return false;
  serial = serial.catch(() => {}).then(() => handle(message));
  serial.then(result => reply({ok: true, state: result.state || 'submitted'}), () => reply({ok: false, state: 'stopped'}));
  return true;
});
chrome.permissions.onRemoved.addListener(() => { serial = serial.catch(() => {}).then(reconcileGrants); });
chrome.alarms?.onAlarm.addListener(() => { serial = serial.catch(() => {}).then(() => handle({action: 'resume'})); });
chrome.runtime.onStartup.addListener(() => { serial = serial.catch(() => {}).then(() => handle({action: 'resume'})); });
