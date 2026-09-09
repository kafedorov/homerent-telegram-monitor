() => {
  // Only rendered listing DOM; no hidden APIs, cookies or application state.
  const text = (el) => el?.innerText?.trim() || '';
  const links = (el) => Array.from(el?.querySelectorAll('a[href]') || [])
    .map(a => ({text: text(a), href: a.href}));
  const cards = Array.from(document.querySelectorAll('article'))
    .filter(el => el.querySelector('a[href*="/rent/flat/"]'))
    .map(el => {
      const title = el.querySelector('[data-name="TitleComponent"]');
      const rows = Array.from(el.querySelectorAll('[data-name="GeneralInfoSectionRowComponent"]'));
      const priceRow = rows.find(row => /[₽Р]\/мес\.|₽\s*\/\s*мес/.test(text(row)));
      const geo = Array.from(el.querySelectorAll('[data-name="SpecialGeo"]'));
      const href = el.querySelector('a[href*="/rent/flat/"]').href;
      return {
        url: href.split('?')[0].split('#')[0],
        title: text(title),
        price_text: text(priceRow),
        address: Array.from(el.querySelectorAll('[data-name="GeoLabel"]')).map(text).join(', '),
        metro: geo.map(g => ({text: text(g), links: links(g)})),
        description: text(el.querySelector('[data-name="Description"]')),
        updated_text: text(el.querySelector('[data-name="TimeLabel"]')),
        text: text(el)
      };
    });
  const next = Array.from(document.querySelectorAll('a[href]'))
    .find(a => text(a) === 'Дальше');
  const headings = Array.from(document.querySelectorAll('h1,h2,h3,h4,h5')).map(text);
  const pageText = text(document.body);
  return {
    cards,
    next_url: next?.href || null,
    headings,
    empty: /Нет\s+вариантов\s+по\s+вашему\s+запросу|Найдено\s+0\s+объявлен/i.test(pageText),
    blocked: /Вы не робот|Подтвердите, что вы человек|Проверка браузера|Verify you are human|Checking your browser|Доступ ограничен/i.test(pageText),
    captured_at: new Date().toISOString()
  };
}
