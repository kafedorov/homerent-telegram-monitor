#!/usr/bin/env python3
"""CIAN rental collector: rendered public cards, explicit filters, CSV/JSON.

Python 3.10+. No private API, credentials, proxy rotation or CAPTCHA solver.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from zoneinfo import ZoneInfo

from settings import METRO_STATIONS as STATIONS

BASE = Path(__file__).resolve().parent
MOSCOW = ZoneInfo("Europe/Moscow")
MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5,
          "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10,
          "ноя": 11, "дек": 12}
MONEY = r"(\d[\d\s\u00a0\u202f]*)(?:[.,]00)?"
PRICE_RE = re.compile(MONEY + r"\s*₽\s*/\s*мес\.?", re.I)
ROOM_RE = re.compile(r"(?<!\d)([1-6])\s*[-–]?\s*комн(?:\.|атн\w*)?\s+(?:квартира|апартаменты)", re.I)
HEADERS = {
    "id": "ID", "status": "Изменение", "rooms": "Комнат", "area_m2": "Площадь, м²",
    "floor": "Этаж", "floors_total": "Этажей в доме", "rent_rub": "Аренда, ₽/мес",
    "metro": "Метро", "metro_minutes": "До метро, минут", "metro_mode": "Способ добраться",
    "address": "Адрес", "deposit_rub": "Залог, ₽", "commission_pct": "Комиссия, %",
    "commission_rub": "Комиссия, ₽", "prepayment_months": "Предоплата, месяцев",
    "entry_base_rub": "Цена входа, ₽", "house_year": "Год постройки",
    "author_type": "Тип автора", "author_name": "Автор",
    "utilities": "Коммунальные платежи", "terms": "Условия из карточки",
    "flags": "Что уточнить", "updated_text": "Дата в объявлении (текст Циана)",
    "published_at": "Дата публикации, UTC", "metro_lines": "Линии",
    "first_seen": "Впервые найдено парсером, UTC", "checked_at": "Проверено, UTC",
    "previous_rent_rub": "Предыдущая цена, ₽", "url": "Ссылка",
}


class CollectionError(RuntimeError):
    pass


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", str(s)).strip().lower().replace("ё", "е").replace("–", "-")


def amount(s: str) -> int:
    return int(re.sub(r"\s", "", s))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_cian_datetime(text: str, captured_at: str) -> str | None:
    """Parse the publication timestamp shown by Cian, interpreted as Moscow time."""
    try:
        captured = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, AttributeError):
        return None
    local = captured.astimezone(MOSCOW)
    value = norm(text)
    match = re.search(r"сегодня\s*,?\s*(\d{1,2}):(\d{2})", value)
    if match:
        result = local.replace(hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)
        return result.astimezone(timezone.utc).isoformat(timespec="seconds")
    match = re.search(r"вчера\s*,?\s*(\d{1,2}):(\d{2})", value)
    if match:
        result = (local - timedelta(days=1)).replace(
            hour=int(match[1]), minute=int(match[2]), second=0, microsecond=0)
        return result.astimezone(timezone.utc).isoformat(timespec="seconds")
    match = re.search(
        r"(?<!\d)(\d{1,2})\s+(янв|фев|мар|апр|май|мая|июн|июл|авг|сен|окт|ноя|дек)[а-я.]*"
        r"\s*,?\s*(\d{1,2}):(\d{2})(?:\s+(\d{4}))?", value)
    if match:
        year = int(match[5]) if match[5] else local.year
        try:
            result = datetime(year, MONTHS[match[2]], int(match[1]), int(match[3]), int(match[4]), tzinfo=MOSCOW)
            if not match[5] and result > local + timedelta(days=1):
                result = result.replace(year=year - 1)
            return result.astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            return None
    match = re.search(r"(\d+)\s+(минут\w*|час\w*|дн\w*)\s+назад", value)
    if match:
        count = int(match[1])
        delta = timedelta(minutes=count) if match[2].startswith("минут") else \
                timedelta(hours=count) if match[2].startswith("час") else timedelta(days=count)
        return (captured - delta).astimezone(timezone.utc).isoformat(timespec="seconds")
    if "только что" in value:
        return captured.astimezone(timezone.utc).isoformat(timespec="seconds")
    return None


def load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8-sig"))
    if type(cfg.get("max_rent")) is not int or cfg["max_rent"] <= 0:
        raise ValueError("max_rent должен быть положительным целым числом")
    if type(cfg.get("min_rent")) is not int or not 0 <= cfg["min_rent"] <= cfg["max_rent"]:
        raise ValueError("min_rent должен быть целым числом от 0 до max_rent")
    if not cfg.get("rooms") or any(type(x) is not int or x not in (1, 2) for x in cfg["rooms"]):
        raise ValueError("rooms: разрешены только 1 и 2; студии и трёхкомнатные исключены")
    aliases = {norm(k): k for k in STATIONS}
    try:
        cfg["stations"] = list(dict.fromkeys(aliases[norm(x)] for x in cfg["stations"]))
    except (KeyError, TypeError):
        raise ValueError("Неизвестная станция в config.json") from None
    if not cfg["stations"]:
        raise ValueError("Нужна хотя бы одна станция")
    walk = cfg.get("max_walk_minutes")
    if walk is not None and (type(walk) is not int or not 1 <= walk <= 120):
        raise ValueError("max_walk_minutes: null или целое число 1–120")
    if type(cfg.get("max_pages")) is not int or not 1 <= cfg["max_pages"] <= 100:
        raise ValueError("max_pages: целое число 1–100")
    if not isinstance(cfg.get("page_delay_seconds"), (int, float)) or not 5 <= cfg["page_delay_seconds"] <= 300:
        raise ValueError("page_delay_seconds: 5–300 секунд")
    cfg["_base"] = str(path.resolve().parent)
    return cfg


def build_url(cfg: dict, page: int = 1, *, include_price: bool = True) -> str:
    params = {
        "deal_type": "rent", "engine_version": 2, "offer_type": "flat", "type": 4,
        "region": 1, "currency": 2, "minprice": cfg["min_rent"], "maxprice": cfg["max_rent"],
        "sort": "creation_date_desc",
    }
    if not include_price:
        params.pop("minprice")
        params.pop("maxprice")
    for room in cfg["rooms"]:
        params[f"room{room}"] = 1
    station_map = cfg.get("station_map", STATIONS)
    ids = sorted({i for name in cfg["stations"] for i in station_map[name]})
    params.update({f"metro[{n}]": i for n, i in enumerate(ids)})
    # Walking distance is verified locally against the displayed metro as well.
    if cfg.get("max_walk_minutes") is not None:
        params.update(only_foot=2, foot_min=cfg["max_walk_minutes"])
    if page > 1:
        params["p"] = page
    return "https://www.cian.ru/cat.php?" + urlencode(params)


def next_page_number(url: str | None, current: int) -> int | None:
    if not url:
        return None
    u = urlparse(url)
    if u.scheme != "https" or u.hostname not in ("www.cian.ru", "cian.ru") or u.path != "/cat.php":
        raise CollectionError("Неожиданная ссылка следующей страницы; переход остановлен")
    try:
        n = int(parse_qs(u.query)["p"][0])
    except (KeyError, ValueError):
        raise CollectionError("Циан изменил разметку переключателя страниц") from None
    if n != current + 1:
        raise CollectionError("Непоследовательная пагинация; переход остановлен")
    return n


def metro_id(href: str) -> int | None:
    u = urlparse(href)
    values = parse_qs(u.query).get("metro[0]")
    if values and values[0].isdigit():
        return int(values[0])
    m = re.search(r"-metro-[^/]+-(\d+)/?$", u.path)
    return int(m[1]) if m else None


def parse_card(card: dict, cfg: dict, captured_at: str) -> tuple[dict | None, str]:
    """Fail closed for unknown price/room/station; never match address prose as metro."""
    u = urlparse(card.get("url", ""))
    match_id = re.fullmatch(r"/rent/flat/(\d+)/?", u.path)
    host = u.hostname or ""
    if u.scheme != "https" or not (host == "cian.ru" or host.endswith(".cian.ru")) or not match_id:
        return None, "не ссылка на аренду квартиры"
    title = card.get("title", "")
    if "студи" in norm(title):
        return None, "студия"
    room = ROOM_RE.search(title)
    if not room:
        return None, "не распознана комнатность"
    if int(room[1]) not in cfg["rooms"]:
        return None, "неподходящая комнатность"
    desc = card.get("description", "")
    # Catch explicit studio misclassification; do not reject a kitchen/living room alone.
    clean_desc = norm(desc)
    clean_desc = re.sub(r"не\s+(?:квартира[- ]?)?студия", "", clean_desc)
    if re.search(r"квартира[- ]студия|апартаменты[- ]студия|сдается\s+(?:(?:уютная|светлая|небольшая)\s+)?студия", clean_desc):
        return None, "в описании явно указана студия"
    price_text = card.get("price_text", "")
    price = PRICE_RE.search(price_text)
    if not price:
        return None, "не распознана месячная цена"
    rent = amount(price[1])
    if not cfg.get("min_rent", 0) <= rent <= cfg["max_rent"]:
        return None, "цена вне диапазона или некорректна"
    terms = price_text[price.end():].strip(" \n,.")
    if not re.search(r"от года|длительн", norm(terms)):
        return None, "долгосрочная аренда не подтверждена карточкой"
    station_map = cfg.get("station_map", STATIONS)
    wanted = {i: name for name in cfg["stations"] for i in station_map[name]}
    wanted_names = {norm(name): name for name in cfg["stations"]}
    candidates = []
    for g in card.get("metro", []):
        text = g.get("text", "")
        mins = re.search(r"(\d+)\s*мин", text)
        minutes = int(mins[1]) if mins else None
        mode = "пешком" if "пешком" in norm(text) else "транспорт" if "транспорт" in norm(text) else "не указан"
        for a in g.get("links", []):
            mid = metro_id(a.get("href", ""))
            # If an ID is present, use it: an identically named MCD is not a metro.
            name = wanted.get(mid) if mid is not None else wanted_names.get(norm(a.get("text", "")))
            if name:
                candidates.append((name, minutes, mode))
    if not candidates:
        return None, "на карточке не подтверждено нужное метро"
    walk = cfg.get("max_walk_minutes")
    if walk is not None:
        candidates = [c for c in candidates if c[2] == "пешком" and c[1] is not None and c[1] <= walk]
        if not candidates:
            return None, "не проходит время пешком до метро"
    station, minutes, mode = min(candidates, key=lambda c: (c[2] != "пешком", c[1] if c[1] is not None else 10**6))
    nterms = norm(terms)
    deposit_match = re.search(r"залог\s*[:—-]?\s*" + MONEY + r"\s*₽", terms, re.I)
    deposit = 0 if "без залога" in nterms else amount(deposit_match[1]) if deposit_match else None
    pct_match = re.search(r"комиссия\s*[:—-]?\s*(\d+(?:[.,]\d+)?)\s*%", terms, re.I)
    rub_match = re.search(r"комиссия\s*[:—-]?\s*" + MONEY + r"\s*₽", terms, re.I)
    pct = float(pct_match[1].replace(",", ".")) if pct_match else 0 if "без комиссии" in nterms else None
    fee = amount(rub_match[1]) if rub_match else round(rent * pct / 100) if pct is not None else None
    utility_match = re.search(r"комм\.\s*платежи[^,\n]*", terms, re.I)
    utilities = utility_match[0] if utility_match else "не указаны"
    area_match = re.search(r"(\d+(?:[.,]\d+)?)\s*м[²2]", title)
    floor_match = re.search(r"(\d+)\s*/\s*(\d+)\s*этаж", title)
    if cfg.get("min_floor") is not None:
        if not floor_match or int(floor_match[1]) < cfg["min_floor"]:
            return None, "первый этаж или этаж не подтверждён"
    flags = []
    if deposit is None or fee is None:
        flags.append("уточнить залог/комиссию")
    if deposit is not None and 0 < deposit < rent / 10:
        flags.append("в карточке очень маленький залог: уточнить сумму")
    if "без счетчиков" in norm(utilities):
        flags.append("счётчики оплачиваются сверху")
    elif "не включены" in norm(utilities) or utilities == "не указаны":
        flags.append("уточнить доплату за ЖКУ")
    if "апартамент" in norm(title):
        flags.append("апартаменты")
    if "московская область" in norm(card.get("address", "")):
        flags.append("Московская область: проверить удалённость")
    if re.search(r"кухня[- ]ниша|студийн|формат[ае]? студии", clean_desc):
        flags.append("проверить планировку по фото")
    if minutes is None or mode == "не указан":
        flags.append("уточнить путь до метро")
    published_at = parse_cian_datetime(card.get("updated_text", ""), captured_at)
    return {
        "id": match_id[1], "url": f"https://{host}/rent/flat/{match_id[1]}/",
        "rooms": int(room[1]), "title": title,
        "area_m2": float(area_match[1].replace(",", ".")) if area_match else None,
        "floor": int(floor_match[1]) if floor_match else None,
        "floors_total": int(floor_match[2]) if floor_match else None,
        "rent_rub": rent, "metro": station, "metro_minutes": minutes, "metro_mode": mode,
        "metro_lines": cfg.get("line_map", {}).get(station, []),
        "address": card.get("address", ""), "deposit_rub": deposit,
        "commission_pct": pct, "commission_rub": fee,
        "entry_base_rub": rent + deposit + fee if deposit is not None and fee is not None else None,
        "utilities": utilities, "terms": terms, "flags": "; ".join(flags),
        "description": desc, "updated_text": card.get("updated_text", ""),
        "published_at": published_at,
        "checked_at": captured_at,
    }, ""


def parse_pages(pages: list[dict], cfg: dict) -> tuple[list[dict], list[dict]]:
    result, excluded = {}, []
    for page in pages:
        for raw in page.get("cards", []):
            item, reason = parse_card(raw, cfg, page.get("captured_at") or now())
            if item:
                # Preserve the newest observation if an ad moved between pages.
                if item["id"] not in result or item["checked_at"] > result[item["id"]]["checked_at"]:
                    result[item["id"]] = item
            else:
                excluded.append({"url": raw.get("url", ""), "title": raw.get("title", ""), "reason": reason})
    return list(result.values()), excluded


def safe_csv(value):
    # Listing text is untrusted; prevent spreadsheet formula execution.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_csv(path: Path, items: list[dict], headers: dict = HEADERS) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(headers.values())
        for item in items:
            writer.writerow([safe_csv(item.get(key, "")) for key in headers])


def atomic_json(path: Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def apply_history(items: list[dict], previous: dict) -> dict:
    updated = dict(previous)
    for item in items:
        old = previous.get(item["id"])
        if old is None:
            item.update(status="новое", first_seen=item["checked_at"], previous_rent_rub=None)
        else:
            changed = old.get("rent_rub") != item["rent_rub"]
            item.update(status="цена изменилась" if changed else "без изменений",
                        first_seen=old.get("first_seen", item["checked_at"]),
                        previous_rent_rub=old.get("rent_rub") if changed else None)
        updated[item["id"]] = {k: item[k] for k in ("rent_rub", "first_seen", "checked_at")}
    # Absence from this run is NOT evidence of removal or rental.
    return updated


def save_result(pages: list[dict], cfg: dict, complete: bool, stop_reason: str,
                *, record_history: bool = True) -> tuple[Path, int]:
    root = Path(cfg["_base"]) / cfg.get("output_dir", "results")
    root.mkdir(parents=True, exist_ok=True)
    run = root / datetime.now(timezone.utc).strftime("%Y-%m-%d_%H-%M-%S_%f_UTC")
    run.mkdir()
    state_path = root / "seen.json"
    previous = {}
    if record_history and state_path.exists():
        previous = json.loads(state_path.read_text(encoding="utf-8"))
        if not isinstance(previous, dict):
            raise ValueError("Файл seen.json повреждён; история не перезаписана")
    items, excluded = parse_pages(pages, cfg)
    updated = apply_history(items, previous)
    if not record_history:
        for item in items:
            item["status"] = "снимок"
    events = [x for x in items if x["status"] in ("новое", "цена изменилась")]
    config_public = {k: v for k, v in cfg.items() if not k.startswith("_")}
    summary = {
        "finished_at": now(), "complete": complete, "stop_reason": stop_reason,
        "pages_collected": len(pages), "raw_cards": sum(len(p.get("cards", [])) for p in pages),
        "unique_matching_offers": len(items), "events": len(events),
        "excluded_reasons": dict(Counter(x["reason"] for x in excluded)),
        "site_headings": pages[0].get("headings", []) if pages else [],
        "search_url": build_url(cfg), "config": config_public,
        "scope": "Публичные карточки. Бюджет = аренда без доплат. Неподтверждённые карточки вынесены в excluded.csv.",
    }
    write_csv(run / "apartments.csv", items)
    write_csv(run / "new_and_changed.csv", events)
    write_csv(run / "excluded.csv", excluded, {"url": "Ссылка", "title": "Заголовок", "reason": "Причина исключения"})
    atomic_json(run / "apartments.json", items)
    atomic_json(run / "summary.json", summary)
    if record_history:
        atomic_json(state_path, updated)
    (root / "latest.txt").write_text(str(run.resolve()), encoding="utf-8")
    return run, len(items)


def read_page(page, extractor: str, pw_timeout) -> dict:
    # Wait for the result or an explicit empty/error state, not an arbitrary sleep.
    for attempt in range(2):
        try:
            page.wait_for_function("""() =>
              document.querySelector('article a[href*="/rent/flat/"]') ||
              /Нет\\s+вариантов\\s+по\\s+вашему\\s+запросу|Найдено\\s+0\\s+объявлен|Вы не робот|Verify you are human|Проверка браузера|Доступ ограничен/i.test(document.body?.innerText || '')
            """, timeout=30000)
        except pw_timeout:
            pass
        data = page.evaluate(extractor)
        data["source_url"] = page.url
        if "captcha" in page.url.lower() or (data.get("blocked") and not data.get("cards")):
            if attempt or not sys.stdin.isatty():
                raise CollectionError("Циан просит проверку человека. Автоматический сбор остановлен.")
            print("\nЦиан показывает проверку человека. Если хотите продолжить, выполните её вручную в открытом окне.")
            response = input("После проверки нажмите Enter. Для остановки введите q: ")
            if response.lower().strip() == "q":
                raise CollectionError("Остановлено пользователем на проверке Циана")
            continue
        if data.get("cards") or data.get("empty"):
            return data
        raise CollectionError("Карточки не найдены: страница не загрузилась или Циан изменил разметку. Это не ноль квартир.")
    raise CollectionError("Не удалось получить выдачу после ручной проверки")


def collect(cfg: dict) -> tuple[list[dict], bool, str]:
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    except ImportError:
        raise CollectionError("Установите зависимости: python -m pip install -r requirements.txt; затем python -m playwright install chromium") from None
    extractor = (BASE / "extract_cards.js").read_text(encoding="utf-8")
    pages, fingerprints = [], set()
    profile = Path(cfg["_base"]) / cfg.get("browser_profile_dir", "browser_profile")
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(profile), headless=False)
        page = context.new_page()
        page.set_default_timeout(30000)
        try:
            number = 1
            for _ in range(cfg["max_pages"]):
                print(f"Страница {number} …", flush=True)
                response = page.goto(build_url(cfg, number), wait_until="domcontentloaded", timeout=60000)
                if response and response.status in (403, 429):
                    raise CollectionError(f"Циан вернул HTTP {response.status}; автоматические повторы отключены")
                data = read_page(page, extractor, PlaywrightTimeout)
                fp = tuple(sorted({c["url"] for c in data["cards"]}))
                if fp and fp in fingerprints:
                    raise CollectionError("Повтор той же страницы; собранная часть сохранена")
                fingerprints.add(fp)
                pages.append(data)
                print(f"  Прочитано карточек: {len(data['cards'])}", flush=True)
                n = next_page_number(data.get("next_url"), number)
                if n is None or data.get("empty"):
                    return pages, True, "Достигнут конец выдачи"
                number = n
                if len(pages) < cfg["max_pages"]:
                    time.sleep(cfg["page_delay_seconds"])
            return pages, False, "Достигнут max_pages; выдача собрана частично"
        except KeyboardInterrupt:
            return pages, False, "Остановлено пользователем"
        except Exception as exc:
            return pages, False, str(exc)
        finally:
            try:
                context.close()
            except Exception:
                pass  # Preserve collected pages even if the user closed the browser.


def main(argv=None) -> int:
    cli = argparse.ArgumentParser(description="Квартиры ЦИАН: 55–70 тыс., 1/2 комнаты, выбранные станции")
    cli.add_argument("--config", type=Path, default=BASE / "config.json")
    cli.add_argument("--print-url", action="store_true", help="Только вывести ссылку поиска")
    cli.add_argument("--input-json", type=Path, help="Разобрать ранее сохранённый снимок DOM без браузера")
    cli.add_argument("--max-pages", type=int, help="Ограничить число страниц за один запуск")
    args = cli.parse_args(argv)
    try:
        cfg = load_config(args.config)
        if args.max_pages is not None:
            if not 1 <= args.max_pages <= 100:
                raise ValueError("--max-pages: 1–100")
            cfg["max_pages"] = args.max_pages
        if args.print_url:
            print(build_url(cfg))
            return 0
        if args.input_json:
            snapshot = json.loads(args.input_json.read_text(encoding="utf-8"))
            pages = snapshot["pages"]
            complete = bool(snapshot.get("complete", False))
            reason = snapshot.get("stop_reason", "Повторный разбор сохранённого снимка")
        else:
            pages, complete, reason = collect(cfg)
        if not pages:
            print(f"Сбор не выполнен: {reason}. Предыдущие результаты сохранены.", file=sys.stderr)
            return 2
        run, total = save_result(pages, cfg, complete, reason, record_history=not bool(args.input_json))
        print(f"\nПодходящих уникальных объявлений: {total}\n{reason}\nРезультаты: {run}\nОткройте apartments.csv в Excel.")
        return 0 if complete else 2
    except (CollectionError, ValueError, OSError, KeyError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Ошибка запуска: {exc}\nПроверьте установку Chromium: python -m playwright install chromium", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
