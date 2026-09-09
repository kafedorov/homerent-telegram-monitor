#!/usr/bin/env python3
"""One-time, duplicate-safe delivery of dated validated baseline listings."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import settings
from cian_parser import BASE, CollectionError, atomic_json
from telegram_client import TelegramError, load_or_setup
from watcher import SingleInstance


DATA = BASE / "data"
MOSCOW = ZoneInfo("Europe/Moscow")
STATE_PATH = DATA / "manual_today_state.json"


def moscow_date(item: dict) -> str | None:
    value = item.get("published_at")
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return stamp.astimezone(MOSCOW).date().isoformat()
    except (ValueError, TypeError):
        return None


def select_today(items: list[dict], date_iso: str) -> list[dict]:
    return sorted(
        (item for item in items if moscow_date(item) == date_iso),
        key=lambda item: (item.get("published_at", ""), item.get("id", "")),
    )


def delivery_queue(selected: list[dict], state: dict, *, resend=False):
    """Return pending items; resend explicitly starts a new manual delivery pass."""
    if resend:
        state["offers"] = {}
    pending = [item for item in selected
               if state["offers"].get(str(item.get("id"))) not in ("sent", "uncertain")]
    return pending, len(selected) - len(pending)


def load_state(date_iso: str) -> dict:
    if not STATE_PATH.exists():
        return {"date": date_iso, "offers": {}}
    data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("offers"), dict):
        raise ValueError("Повреждён data/manual_today_state.json")
    if data.get("date") != date_iso:
        return {"date": date_iso, "offers": {}}
    # A crash after starting a request has an unknown outcome; do not duplicate it.
    for oid, status in list(data["offers"].items()):
        if status == "sending":
            data["offers"][oid] = "uncertain"
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Разово отправить объявления за выбранный день из data/baseline.json")
    parser.add_argument("--yes", action="store_true", help="Не спрашивать подтверждение")
    parser.add_argument("--yesterday", action="store_true",
                        help="Взять предыдущую московскую дату вместо сегодняшней")
    retry = parser.add_mutually_exclusive_group()
    retry.add_argument(
        "--retry-uncertain", action="store_true",
        help="Повторить отправки с неизвестным результатом; возможны дубли")
    retry.add_argument(
        "--resend", action="store_true",
        help="Заново отправить все выбранные карточки, включая уже отправленные")
    args = parser.parse_args(argv)

    baseline_path = DATA / "baseline.json"
    if not baseline_path.exists():
        raise CollectionError("Нет data/baseline.json. Сначала дождитесь завершения первого обхода.")
    items = json.loads(baseline_path.read_text(encoding="utf-8"))
    if not isinstance(items, list):
        raise ValueError("Повреждён data/baseline.json")

    target_date = datetime.now(MOSCOW).date() - timedelta(days=1 if args.yesterday else 0)
    date_iso = target_date.isoformat()
    selected = select_today(items, date_iso)
    state = load_state(date_iso)
    if args.retry_uncertain:
        state["offers"] = {
            oid: status for oid, status in state["offers"].items() if status != "uncertain"
        }
    pending, skipped = delivery_queue(selected, state, resend=args.resend)
    day_label = "Вчера" if args.yesterday else "Сегодня"
    print(f"{day_label} по Москве ({date_iso}) в исходной выгрузке: {len(selected)}.")
    print(f"К отправке: {len(pending)}; уже отправлены или неподтверждены: {skipped}.")
    if not pending:
        return 0
    if not args.yes:
        action = "ПОВТОРНО отправить" if args.resend else "Отправить"
        answer = input(f"{action} эти карточки в подключённого бота? Введите ДА: ").strip().lower()
        if answer not in ("да", "yes", "y"):
            print("Отправка отменена.")
            return 0

    bot = load_or_setup(DATA / "telegram.json",
                        custom_emoji_pack=settings.TELEGRAM_EMOJI_PACK)
    sent = 0
    for number, item in enumerate(pending, 1):
        oid = str(item.get("id"))
        state["offers"][oid] = "sending"
        atomic_json(STATE_PATH, state)
        try:
            bot.listing(item, settings.MAX_PHOTOS)
        except TelegramError as error:
            if error.uncertain:
                state["offers"][oid] = "uncertain"
                atomic_json(STATE_PATH, state)
                print(f"ID {oid}: результат доставки неизвестен. Остановка против дублей.")
            else:
                state["offers"].pop(oid, None)
                atomic_json(STATE_PATH, state)
                print(f"ID {oid}: Telegram отклонил отправку (код {error.code}).")
            return 2
        state["offers"][oid] = "sent"
        atomic_json(STATE_PATH, state)
        sent += 1
        print(f"[{number}/{len(pending)}] Отправлено: {oid}")
        time.sleep(1.2)
    print(f"Готово. Отправлено карточек: {sent}.")
    return 0


if __name__ == "__main__":
    try:
        with SingleInstance(DATA / "monitor.lock"):
            raise SystemExit(main())
    except (CollectionError, ValueError, OSError, json.JSONDecodeError) as error:
        print(f"Ошибка: {error}")
        raise SystemExit(1)
