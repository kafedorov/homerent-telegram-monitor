() => {
  const text = e => e?.innerText?.trim() || '';
  const bodyText = text(document.body);
  const fields = {};
  for (const item of document.querySelectorAll('[data-name="OfferSummaryInfoItem"]')) {
    const p = Array.from(item.querySelectorAll('p')).map(text);
    if (p.length >= 2) fields[p[0]] = p.slice(1).join(' ');
  }
  const photos = [];
  const used = new Set();
  const gallery = document.querySelector(
    '[data-name="OfferGallery"] [data-name="Scroller"], #photos [data-name="Scroller"], [data-name="OfferGallery"], #photos'
  );
  for (const img of gallery?.querySelectorAll('img') || []) {
    const src = img.currentSrc || img.src || img.getAttribute('data-src');
    if (!src) continue;
    let u;
    try { u = new URL(src); } catch { continue; }
    if (u.protocol !== 'https:' || !/(^|\.)cdn-cian\.ru$/.test(u.hostname) || !u.pathname.startsWith('/images/')) continue;
    const key = u.pathname.replace(/-\d+(\.(?:jpe?g|png|webp))$/i, '$1');
    if (!used.has(key)) { photos.push(src); used.add(key); }
    if (photos.length === 3) break;
  }
  // Only DOM-visible labelled fields. These lines are also useful if a markup name changes.
  const lines = bodyText.split(/\n+/).map(s => s.trim()).filter(Boolean);
  const labelValue = label => { const i = lines.indexOf(label); return i >= 0 ? lines[i+1] || '' : ''; };
  // Cian currently renders the author below the price/contact card. That block
  // has no stable data-name, but profile links have stable public URL shapes.
  const visible = e => e && e.getClientRects().length > 0;
  const profileLinks = Array.from(document.querySelectorAll('a[href]')).filter(link => {
    if (!visible(link)) return false;
    try { return /^\/(?:company|agents)\/\d+\/?$/.test(new URL(link.href).pathname); }
    catch { return false; }
  });
  const companyLink = profileLinks.find(link => new URL(link.href).pathname.startsWith('/company/'));
  const agentLink = profileLinks.find(link => new URL(link.href).pathname.startsWith('/agents/'));
  // An agency may be followed by a specific employee. The requested card has
  // one author field, so the agency (the listing's organisation) takes priority.
  const authorNameNode = companyLink || agentLink || document.querySelector(
    '[data-name="AgentName"], [data-name="AuthorName"], [data-name="CompanyName"]'
  );
  const labelPattern = /(^|\n)(агентство недвижимости|собственник|ри[еэ]лтор|агент)(\n|$)/i;
  let authorRoot = authorNameNode;
  for (let node = authorNameNode?.parentElement, depth = 0; node && depth < 8; node = node.parentElement, depth++) {
    if (labelPattern.test(text(node))) { authorRoot = node; break; }
  }
  if (!authorRoot) {
    authorRoot = Array.from(document.querySelectorAll(
      '[data-name="AuthorAside"], [data-name="AuthorCard"], [data-name="OfferCard"]'
    )).find(node => labelPattern.test(text(node))) || null;
  }
  let authorText = text(authorRoot);
  let authorName = text(authorNameNode);
  if (!authorText) {
    const priorities = [/^агентство недвижимости$/i, /^собственник$/i, /^ри[еэ]лтор$/i, /^агент$/i];
    for (const pattern of priorities) {
      const i = lines.findIndex(line => pattern.test(line));
      if (i < 0) continue;
      const candidate = lines.slice(i + 1, i + 5).find(line =>
        line.length <= 120 && !/^(суперагент|показать телефон|написать|проверено|быстро отвечает)/i.test(line)
      ) || '';
      authorText = [lines[i], candidate].filter(Boolean).join('\n');
      authorName = candidate;
      break;
    }
  }
  return {
    title: text(document.querySelector('h1')),
    repair: fields['Ремонт'] || '',
    house_year: fields['Год постройки'] || labelValue('Год постройки'),
    price_text: text(document.querySelector('[data-name="PriceInfo"]')),
    floor_text: labelValue('Этаж'),
    deposit: fields['Залог'] || labelValue('Залог'),
    commission: fields['Комиссия'] || labelValue('Комиссия'),
    prepayment: fields['Предоплата'] || labelValue('Предоплата'),
    author_text: authorText,
    author_name: authorName,
    photos,
    blocked: /Вы не робот|Подтвердите, что вы человек|Проверка браузера|Verify you are human|Checking your browser|Доступ ограничен/i.test(bodyText),
    inactive: /Объявление снято с публикации|Объявление удалено|Объявление неактуально|Объявление в архиве/i.test(bodyText),
    captured_at: new Date().toISOString()
  };
}
