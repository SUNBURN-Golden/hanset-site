/* All source text is untrusted data. No execution or raw quarantine. */
(function (root) {
  const privateContext = /direct message|private message|members?[-\s]+only\s+(conversation|chat|discussion|message|forum)|inbox|개인\s*프로필|쪽지|회원\s*전용\s*(대화|채팅|메시지|쪽지|게시판)|주거\s*주소|집\s*주소|자택|주민등록|계좌\s*번호|신용카드\s*번호|진료\s*기록|건강\s*기록|나의\s*진단|아동\s*개인|아이의\s*(이름|학교)|my\s*(diagnosis|bank account|home address)|child.{0,20}(name|school)|\bDM\b/i;
  const injection = /ignore\s+(all\s+)?(previous|prior|system)\s+instructions|이전\s*지시.*무시|이\s*명령을?\s*실행|system\s*prompt|execute\s+(this|the following)\s+command/i;
  const sensitive = /token|auth|password|passwd|secret|cookie|session|email|phone|address|input|textarea|contenteditable|profile|private/i;
  function cleanText(value) {
    let s = String(value).normalize('NFKC');
    if (privateContext.test(s) || /patient\s*(name|record|diagnosis)|medical\s*record|my\s*(medication|income|salary|balance)|credit\s*card\s*(number|details)|(?:제|저의)\s*(병명|진단|복용약|계좌|잔고)|(?:자녀|아동|미성년자).{0,16}(이름|학교|생년월일)|\b(son|daughter).{0,16}(name|school|born)/i.test(s)
        || /[가-힣]+(로|길)\s*\d+(-\d+)?\s*(번지|동|호)|\d+\s+\w+\s+(Street|Road|Avenue)\b/i.test(s)) throw Error('private_context');
    s = s.replace(/[\w.+-]+@[\w-]+\.[\w.-]+/g, m => m === 'hanset@soulbound.co.kr' ? m : '[redacted]');
    s = s.replace(/\b\d{6}[ -]?[1-8]\d{6}\b/g, '[redacted]')
      .replace(/(?:\+\d{1,3}[ .-]?)?(?:0\d{1,2})[ .()-]*\d{3,4}[ .-]*\d{4}\b/g, '[redacted]')
      .replace(/(?:\+\d{1,3}[ .-]?(?:\d[ .()-]*){7,12}\d|\(\d{3}\)[ .-]*\d{3}[ .-]*\d{4})(?!\d)/g, '[redacted]')
      .replace(/(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)/g, '[redacted]')
      .replace(/bearer\s+\S+|(?:password|passwd|cookie|token|secret|api[_-]?key|비밀번호)\s*[:=]\s*[^\s,;]+|eyJ[\w-]+\.[\w-]+\.[\w-]+/gi, '[redacted]');
    return s.split('\n').map(line => injection.test(line) ? '[untrusted instruction removed]' : line).join('\n').trim();
  }
  function safeURL(value) {
    if (typeof value !== 'string' || value.length > 2048 || /[\s\\\x00-\x1f]/.test(value)) throw Error('unsafe_url');
    let decoded;
    try { decoded = decodeURIComponent(decodeURIComponent(value)).normalize('NFKC'); } catch { throw Error('unsafe_url'); }
    const u = new URL(decoded);
    if (u.protocol !== 'https:' || u.username || u.password || u.port || /[\w.+-]+@[\w-]+\.[\w.-]+/.test(decoded) || /(^|\.)naver\.(com|net)$/.test(u.hostname)
        || /\/(login|signin|inbox|messages|account|profile)(\/|$)/i.test(u.pathname)
        || /calendar|\/search|[?&](q|search|page|offset)=/i.test(u.pathname + u.search) || decoded.includes('%')) throw Error('unsafe_url');
    if (cleanText(decoded) !== decoded) throw Error('unsafe_url');
    for (const [k, v] of [...u.searchParams]) {
      if (sensitive.test(k) || v.length > 200) throw Error('sensitive_url');
      if (/^(utm_|fbclid|gclid)/i.test(k)) u.searchParams.delete(k);
    }
    u.hostname = u.hostname.replace(/\.$/, '');
    u.hash = '';
    u.searchParams.sort();
    return u.href;
  }
  const api = {cleanText, safeURL};
  root.HANSETPrivacy = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
