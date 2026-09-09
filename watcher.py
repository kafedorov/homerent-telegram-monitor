#!/usr/bin/env python3
"""Windows-friendly foreground Cian monitor. Run with run_windows.bat."""
from __future__ import annotations

import argparse
import csv
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys
import tempfile
import time

import settings
from cian_parser import (BASE, CollectionError, atomic_json, build_url,
                         next_page_number, now, parse_cian_datetime, write_csv)
from monitor_core import Store, deliver, load_settings, offer_id, validate_detail
from telegram_client import TelegramError, format_listing, load_or_setup

DATA = BASE / "data"
LOG = logging.getLogger("cian")
DETAIL_PHOTOS_READY_JS = """() =>
    document.querySelectorAll('[data-name="OfferGallery"] img[src*="cdn-cian.ru/images/"], #photos img[src*="cdn-cian.ru/images/"]').length > 0 ||
    (document.body?.innerText || '').split(String.fromCharCode(10)).some(line => /^0 фото$/i.test(line.trim()))
"""


class SingleInstance:
    """OS-owned file lock is released even if the process crashes."""
    def __init__(self, path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if not self.path.stat().st_size:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise CollectionError("Монитор уже запущен из этой папки. Используйте открытое окно.") from None
        return self

    def __exit__(self, *_):
        self.file.close()


class Alerts:
    def __init__(self, store, bot):
        self.store, self.bot = store, bot

    def send(self, key, message):
        LOG.info(message)
        previous = float(self.store.meta("alert:" + key) or 0)
        if time.time() - previous < 3600:
            return
        self.store.put_meta("alert:" + key, str(time.time()))
        try:
            self.bot.message(message)
        except TelegramError as e:
            LOG.warning("Не удалось отправить служебное сообщение: %s", e)


class CianBrowser:
    def __init__(self, context, cfg, alert, pw_timeout):
        self.cfg, self.alert, self.timeout = cfg, alert, pw_timeout
        self.search = context.pages[0] if context.pages else context.new_page()
        self.detail = context.new_page()
        self.last_navigation = 0.0
        self.cards_js = (BASE / "extract_cards.js").read_text(encoding="utf-8")
        self.detail_js = (BASE / "extract_detail.js").read_text(encoding="utf-8")

    def load(self, page, url, *, detail=False):
        delay = self.cfg["page_delay_seconds"] - (time.monotonic() - self.last_navigation)
        if delay > 0:
            time.sleep(delay)
        self.last_navigation = time.monotonic()
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        status = response.status if response else 0
        while True:
            if detail and status in (404, 410):
                return {"inactive": True}
            if status >= 500:
                raise CollectionError(f"Циан временно недоступен: HTTP {status}")
            try:
                selector = "'[data-name=PriceInfo]'" if detail else "'article a[href*=\"/rent/flat/\"]'"
                page.wait_for_function("""() => document.querySelector(""" + selector + """) ||
                    /Нет\\s+вариантов\\s+по\\s+вашему\\s+запросу|Найдено\\s+0\\s+объявлен|Вы не робот|Подтвердите, что вы человек|Verify you are human|Проверка браузера|Доступ ограничен|Объявление снято|Объявление удалено|Объявление в архиве/i.test(document.body?.innerText || '')
                """, timeout=30000)
            except self.timeout:
                pass
            if detail:
                # Price and gallery are rendered independently. Without this
                # short wait Cian can be parsed before its first photos exist.
                try:
                    page.wait_for_function(DETAIL_PHOTOS_READY_JS, timeout=5000)
                except self.timeout:
                    pass
            data = page.evaluate(self.detail_js if detail else self.cards_js)
            if data.get("blocked") or "captcha" in page.url.lower() or status in (403, 429):
                self.alert("captcha", "Циан просит ручную проверку или ограничил доступ. Монитор на паузе. Посмотрите окно браузера и консоль на ноутбуке.")
                print("Пройдите проверку в открытом браузере. При ограничении доступа дождитесь его снятия.")
                answer = input("После восстановления страницы нажмите Enter; q — завершить: ").strip().lower()
                if answer == "q":
                    raise KeyboardInterrupt
                # Re-read the manually restored page, no automatic CAPTCHA bypass.
                status = 0
                continue
            if detail:
                if data.get("inactive"):
                    return data
                if offer_id(page.url) != offer_id(url):
                    raise CollectionError("Карточка перенаправила на другой адрес")
                if not data.get("title") or not data.get("price_text"):
                    raise CollectionError("Страница квартиры не загрузилась полностью")
            elif not data.get("cards") and not data.get("empty"):
                raise CollectionError("Выдача не распознана: загрузка не завершена или изменилась разметка")
            return data

    def read_search(self, number):
        return self.load(self.search, build_url(self.cfg, number, include_price=True))

    def read_detail(self, url):
        try:
            return self.load(self.detail, url, detail=True)
        except self.timeout:
            raise CollectionError("Страница квартиры не ответила вовремя") from None


def process_pending(store, browser, bot, cfg, *, baseline=False):
    status = "baseline_candidate" if baseline else "needs_detail"
    for row in store.pending(status):
        try:
            detail = browser.read_detail(json.loads(row["item"])["url"])
        except CollectionError as e:
            if baseline:
                store.set_status(row["id"], "baseline_review", str(e))
            else:
                store.detail_failed(row, cfg, str(e))
            LOG.warning("ID %s: %s", row["id"], e)
            continue
        item, reason = validate_detail(json.loads(row["item"]), detail, cfg)
        if item:
            if baseline:
                store.baseline_ready(row["id"], item)
            else:
                store.ready(row["id"], item)
                deliver(store, bot, cfg, LOG.info)
        else:
            store.set_status(row["id"], "baseline_ignored" if baseline else "ignored", reason)
    if not baseline:
        deliver(store, bot, cfg, LOG.info)


def export_baseline(store, folder):
    items = store.baseline_items()
    atomic_json(folder / "baseline.json", items)
    write_csv(folder / "baseline.csv", items)
    return len(items)


def scan_cycle(store, browser, bot, cfg, *, baseline_folder=None):
    baseline = not store.initialized
    process_pending(store, browser, bot, cfg, baseline=baseline)
    seen_pages = set()
    number, new_ids = 1, 0
    for _ in range(cfg["max_pages"]):
        page = browser.read_search(number)
        ids = tuple(sorted({offer_id(c.get("url", "")) for c in page["cards"]} - {None}))
        if page["cards"] and not ids:
            raise CollectionError("Не распознаны ID объявлений")
        if ids and ids in seen_pages:
            raise CollectionError("Циан повторил страницу; обход неполный")
        seen_pages.add(ids)
        for raw in page["cards"]:
            new_ids += store.observe(raw, cfg, baseline)
        LOG.info("Страница %s: %s карточек; новых ID за обход: %s", number, len(page["cards"]), new_ids)
        process_pending(store, browser, bot, cfg, baseline=baseline)
        cutoff = store.meta("baseline_cutoff")
        page_is_old = bool(baseline and page["cards"] and cutoff)
        if page_is_old:
            times = [parse_cian_datetime(raw.get("updated_text", ""), page.get("captured_at") or now())
                     for raw in page["cards"]]
            page_is_old = all(value and value < cutoff for value in times)
        next_number = next_page_number(page.get("next_url"), number)
        if next_number is None or page.get("empty") or page_is_old:
            if baseline:
                store.baseline_complete()
                baseline_count = len(store.baseline_items())
                if baseline_folder is not None:
                    export_baseline(store, baseline_folder)
            else:
                baseline_count = None
            store.put_meta("last_complete_scan", now())
            return {"baseline": baseline, "pages": number, "new_ids": new_ids,
                    "baseline_count": baseline_count}
        number = next_number
    raise CollectionError("Достигнут MAX_PAGES: выдача собрана частично. Уменьшите список метро или увеличьте лимит до 100.")


def export_status(store, folder, *, last_error=""):
    atomic_json(folder / "status.json", {"updated_at": now(), "initialized": store.initialized,
        "last_complete_scan": store.meta("last_complete_scan"), "counts": store.summary(), "last_error": last_error})
    with (folder / "needs_attention.csv").open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["ID", "Состояние", "Причина", "Ссылка"])
        for row in store.db.execute(
                "SELECT * FROM offers WHERE status IN ('baseline_review','review','uncertain')"):
            writer.writerow([row["id"], row["status"], row["reason"], json.loads(row["raw"])["url"]])


class PreviewBot:
    def __init__(self, folder):
        self.items, self.folder = [], folder

    def message(self, text):
        LOG.info("Проверочный режим: %s", text)

    def listing(self, item, max_photos):
        self.items.append(item)
        atomic_json(self.folder / "preview.json", self.items)
        LOG.info("ПРОСМОТР: %s\nФото: %s", format_listing(item), min(len(item.get("photos", [])), max_photos))
        return {"preview": True}


def run(args, cfg, store, bot, folder):
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Error as PWError
    alerts = Alerts(store, bot)
    if not store.initialized:
        LOG.info("Первый обход создаёт проверенную базу за последние %s суток. Рассылка квартир начнётся после него.",
                 cfg["baseline_fresh_days"])
    else:
        LOG.info("История загружена. Уже известные ID повторно не отправляются.")
    with sync_playwright() as pw:
        with pw.chromium.launch_persistent_context(str(folder / "browser_profile"), headless=False) as context:
            browser = CianBrowser(context, cfg, alerts.send, PWTimeout)
            while True:
                started, error = time.monotonic(), ""
                try:
                    result = scan_cycle(store, browser, bot, cfg, baseline_folder=folder)
                    LOG.info("Обход завершён: %s", result)
                    if result["baseline"]:
                        alerts.send("baseline", f"Первый обход завершён: в исходной выгрузке {result['baseline_count']} подходящих объявлений не старше {cfg['baseline_fresh_days']} суток. Теперь слежу за новыми квартирами: {cfg['min_rent']:,}–{cfg['max_rent']:,} ₽, комнаты {cfg['rooms']}, этаж от {cfg['min_floor']}, {', '.join(cfg['allowed_repairs'])}, до {cfg['max_walk_minutes']} минут пешком. Проверка каждые {cfg['interval'] // 60} минут.")
                    if any(store.summary().get(key) for key in ("baseline_review", "uncertain", "review")):
                        alerts.send("attention", "Есть объявления с неподтверждённой доставкой или ошибкой чтения. Посмотрите data/needs_attention.csv на ноутбуке.")
                except PWTimeout:
                    error = f"Циан не ответил вовремя. Повторю обход через {cfg['interval'] // 60} минут."
                    alerts.send("timeout", error)
                except CollectionError as e:
                    error = str(e)
                    alerts.send("scan_error", error)
                except PWError:
                    if browser.search.is_closed() or browser.detail.is_closed():
                        alerts.send("browser_closed", "Браузер закрыт или завершился с ошибкой. Монитор остановлен; запустите run_windows.bat на ноутбуке.")
                        return 2
                    error = "Страница не открылась: проверьте интернет. Следующий обход выполнится через заданный интервал."
                    alerts.send("network_error", error)
                finally:
                    export_status(store, folder, last_error=error)
                if args.once or args.dry_run:
                    return 2 if error else 0
                elapsed = time.monotonic() - started
                if elapsed > cfg["interval"]:
                    alerts.send("slow", "Обход Циана занимает больше заданного интервала. Уведомления могут запаздывать; подробности в data/monitor.log.")
                wait = cfg["interval"] if error else max(5, cfg["interval"] - elapsed)
                LOG.info("Следующий обход через %.0f секунд. Остановка: Ctrl+C.", wait)
                time.sleep(wait)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Новые квартиры Циана → Telegram, Windows, 10 минут")
    parser.add_argument("--setup", action="store_true", help="Заново подключить Telegram, затем завершить")
    parser.add_argument("--once", action="store_true", help="Один обход с обычной историей и рассылкой")
    parser.add_argument("--dry-run", action="store_true", help="Текущие квартиры в data/preview.json; без Telegram, отдельная история")
    parser.add_argument("--status", action="store_true", help="Показать сохранённый статус без запуска браузера")
    parser.add_argument("--print-url", action="store_true", help="Показать ссылку поиска с лимитом цены")
    parser.add_argument("--retry-uncertain", action="store_true", help="Повторить неподтверждённые отправки; возможны дубли")
    args = parser.parse_args(argv)
    try:
        cfg = load_settings(settings)
        if args.print_url:
            print(build_url(cfg))
            return 0
        if args.status:
            print((DATA / "status.json").read_text(encoding="utf-8") if (DATA / "status.json").exists() else "Монитор ещё не выполнял обход.")
            return 0
        DATA.mkdir(exist_ok=True)
        handler = RotatingFileHandler(DATA / "monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", handlers=[handler, logging.StreamHandler()])
        with SingleInstance(DATA / "monitor.lock"):
            if args.setup:
                load_or_setup(DATA / "telegram.json", force=True,
                              custom_emoji_pack=settings.TELEGRAM_EMOJI_PACK)
                return 0
            bot = PreviewBot(DATA) if args.dry_run else load_or_setup(
                DATA / "telegram.json", custom_emoji_pack=settings.TELEGRAM_EMOJI_PACK)
            # Dry run gets a fresh DB and browser profile and never edits normal history.
            with tempfile.TemporaryDirectory(prefix="cian-preview-") as temp:
                folder = Path(temp) if args.dry_run else DATA
                store = Store(folder / "monitor.sqlite3")
                try:
                    store.configure(cfg)
                    if args.dry_run:
                        atomic_json(DATA / "preview.json", [])
                        store.baseline_complete()
                    if args.retry_uncertain and not args.dry_run:
                        store.retry_uncertain()
                    return run(args, cfg, store, bot, folder)
                finally:
                    store.close()
    except KeyboardInterrupt:
        print("\nМонитор остановлен. История сохранена.")
        return 0
    except (ValueError, KeyError, OSError, CollectionError, TelegramError) as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        # Don't expose network URLs or secrets from dependency exceptions.
        print(f"Ошибка запуска ({type(e).__name__}). Проверьте Python 3.10+, установку Chromium и README.md.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
