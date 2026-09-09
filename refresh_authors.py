#!/usr/bin/env python3
"""Refresh missing public author and photo fields in the existing baseline."""
from __future__ import annotations

import argparse
import sys

import settings
from cian_parser import BASE, CollectionError, now
from monitor_core import Store, classify_author, load_settings, trusted_photo_urls
from watcher import CianBrowser, SingleInstance, export_baseline

DATA = BASE / "data"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Обновить авторов и первые фото в первичной базе без её пересоздания")
    parser.add_argument("--all", action="store_true",
                        help="Перепроверить все записи, даже если автор и три фото уже сохранены")
    args = parser.parse_args(argv)
    store = None
    try:
        cfg = load_settings(settings)
        if not (DATA / "monitor.sqlite3").exists():
            raise CollectionError("Нет data/monitor.sqlite3. Сначала завершите первичный обход.")
        with SingleInstance(DATA / "monitor.lock"):
            store = Store(DATA / "monitor.sqlite3")
            items = store.baseline_items()
            if not items:
                raise CollectionError("В первичной базе пока нет подходящих объявлений.")
            targets = [item for item in items if args.all
                       or item.get("author_type") in (None, "", "Не указан")
                       or len(item.get("photos") or []) < 3]
            if not targets:
                print("В первичной базе нет объявлений с пропущенным автором или фотографиями.")
                return 0

            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout, Error as PWError

            print(f"Перепроверяю авторов и фото: {len(targets)}. Монитор должен быть остановлен.")
            updated = unknown = photo_updates = errors = 0
            with sync_playwright() as pw:
                with pw.chromium.launch_persistent_context(
                        str(DATA / "browser_profile"), headless=False) as context:
                    browser = CianBrowser(context, cfg, lambda _key, message: print(message), PWTimeout)
                    for index, item in enumerate(targets, 1):
                        oid = str(item.get("id", ""))
                        try:
                            detail = browser.read_detail(item["url"])
                            author_type, author_name = classify_author(detail)
                            old_photos = trusted_photo_urls(item.get("photos"), 3)
                            fresh_photos = trusted_photo_urls(detail.get("photos"), 3)
                            photos = fresh_photos or old_photos
                            refreshed = dict(item, author_type=author_type, author_name=author_name,
                                             photos=photos, author_checked_at=now(),
                                             photos_checked_at=now())
                            store.update_item(oid, refreshed)
                            if photos != old_photos:
                                photo_updates += 1
                            if author_type == "Не указан":
                                unknown += 1
                            else:
                                updated += 1
                            print(f"[{index}/{len(targets)}] {oid}: {author_type} — "
                                  f"{author_name or 'имя не указано'}; фото: {len(photos)}")
                        except (CollectionError, KeyError, PWError) as exc:
                            errors += 1
                            print(f"[{index}/{len(targets)}] {oid}: не удалось прочитать ({exc})")
            export_baseline(store, DATA)
            print(f"Готово. Авторы: {updated}; автор не найден: {unknown}; "
                  f"фото обновлены: {photo_updates}; ошибок: {errors}.")
            return 0 if not errors else 2
    except KeyboardInterrupt:
        print("\nОстановлено. Уже обновлённые записи сохранены в базе.")
        if store is not None:
            export_baseline(store, DATA)
        return 130
    except (ValueError, OSError, CollectionError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
