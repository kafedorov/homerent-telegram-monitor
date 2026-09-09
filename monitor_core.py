"""Persistent discovery and delivery state, independent of browser and Telegram."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from cian_parser import MONEY, PRICE_RE, ROOM_RE, amount, norm, now, parse_card, parse_cian_datetime
from telegram_client import TelegramError


def load_settings(module) -> dict:
    cfg = {
        "min_rent": module.MIN_RENT, "max_rent": module.MAX_RENT, "rooms": module.ROOMS,
        "station_map": module.METRO_STATIONS, "stations": list(module.METRO_STATIONS),
        "line_map": module.METRO_LINES,
        "max_walk_minutes": module.MAX_WALK_MINUTES, "min_floor": module.MIN_FLOOR,
        "allowed_repairs": module.ALLOWED_REPAIRS,
        "baseline_fresh_days": module.BASELINE_FRESH_DAYS,
        "interval": module.CHECK_INTERVAL_SECONDS, "page_delay_seconds": module.REQUEST_PAUSE_SECONDS,
        "max_pages": module.MAX_PAGES, "max_photos": module.MAX_PHOTOS,
        "detail_retries": module.DETAIL_RETRY_LIMIT,
    }
    bounds = {"min_rent": (0, 100_000_000), "max_rent": (1, 100_000_000),
              "max_walk_minutes": (1, 120), "baseline_fresh_days": (1, 30),
              "min_floor": (2, 150), "interval": (600, 86400), "page_delay_seconds": (5, 300),
              "max_pages": (1, 100), "max_photos": (0, 10), "detail_retries": (1, 10)}
    for name, (low, high) in bounds.items():
        if type(cfg[name]) is not int or not low <= cfg[name] <= high:
            raise ValueError(f"settings.py: {name} должно быть целым числом {low}–{high}")
    if cfg["min_rent"] > cfg["max_rent"]:
        raise ValueError("settings.py: MIN_RENT не может быть больше MAX_RENT")
    if not cfg["rooms"] or any(type(r) is not int or r not in (1, 2) for r in cfg["rooms"]):
        raise ValueError("settings.py: ROOMS — только 1 и/или 2")
    if not cfg["station_map"] or any(
        not isinstance(name, str) or not name.strip() or not isinstance(ids, list) or not ids
        or any(type(i) is not int or i <= 0 for i in ids)
        for name, ids in cfg["station_map"].items()
    ):
        raise ValueError("settings.py: METRO_STATIONS — словарь названий и списков числовых ID")
    if not cfg["allowed_repairs"] or any(norm(x) not in ("евроремонт", "дизайнерский") for x in cfg["allowed_repairs"]):
        raise ValueError("settings.py: ALLOWED_REPAIRS — Евроремонт и/или Дизайнерский")
    if set(cfg["line_map"]) != set(cfg["station_map"]):
        raise ValueError("settings.py: METRO_LINES должен содержать те же названия, что METRO_STATIONS")
    return cfg


def fingerprint(cfg: dict) -> str:
    keys = ("min_rent", "max_rent", "rooms", "station_map", "line_map",
            "max_walk_minutes", "min_floor", "allowed_repairs", "baseline_fresh_days")
    raw = json.dumps({k: cfg[k] for k in keys}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def offer_id(url: str) -> str | None:
    u = urlparse(url)
    host = u.hostname or ""
    m = re.fullmatch(r"/rent/flat/(\d+)/?", u.path)
    return m[1] if u.scheme == "https" and (host == "cian.ru" or host.endswith(".cian.ru")) and m else None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS offers (
                id TEXT PRIMARY KEY, first_seen TEXT NOT NULL, status TEXT NOT NULL,
                raw TEXT NOT NULL, item TEXT, reason TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
                telegram_result TEXT
            );
        """)
        # A crash after the send started has an unknown outcome. Never silently duplicate.
        with self.db:
            self.db.execute("UPDATE offers SET status='uncertain', reason='Перезапуск во время отправки' WHERE status='sending'")
            # Repair used to be a strict filter even when Cian did not expose the
            # value. Recheck only post-baseline offers rejected for that exact
            # reason; already sent and explicitly unsuitable repairs stay intact.
            self.db.execute("""
                UPDATE offers
                SET status='needs_detail', reason='', attempts=0, retry_at=0
                WHERE status='ignored'
                  AND reason='ремонт не подходит или не указан: нет данных'
            """)

    def close(self):
        self.db.close()

    def meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put_meta(self, key: str, value: str):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    def configure(self, cfg: dict) -> bool:
        changed = self.meta("filter") != fingerprint(cfg)
        if changed:
            # Cian shows publication time only to the minute. Start at that
            # minute boundary so a genuinely new offer from the same minute
            # is not lost because our process started a few seconds later.
            started = datetime.now(timezone.utc).replace(second=0, microsecond=0)
            cutoff = started - timedelta(days=cfg["baseline_fresh_days"])
            with self.db:
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('filter', ?)", (fingerprint(cfg),))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('baseline_started_at', ?)",
                                (started.isoformat(),))
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('baseline_cutoff', ?)",
                                (cutoff.isoformat(),))
                self.db.execute("DELETE FROM meta WHERE key='initialized'")
                # Rebuild a coherent baseline for the new filters. No apartment is sent
                # during that pass, so clearing only the discovery history cannot flood Telegram.
                self.db.execute("DELETE FROM offers")
        return changed

    @property
    def initialized(self):
        return self.meta("initialized") is not None

    def baseline_complete(self):
        self.put_meta("initialized", now())

    def observe(self, raw: dict, cfg: dict, baseline: bool) -> bool:
        oid = offer_id(raw.get("url", ""))
        if not oid or self.db.execute("SELECT 1 FROM offers WHERE id=?", (oid,)).fetchone():
            return False
        observed_at = now()
        item, reason = parse_card(raw, cfg, observed_at)
        published_at = (item or {}).get("published_at") or parse_cian_datetime(
            raw.get("updated_text", ""), observed_at)
        if baseline:
            cutoff = datetime.fromisoformat(self.meta("baseline_cutoff"))
            published = datetime.fromisoformat(published_at) if published_at else None
            recent = published is not None and published >= cutoff
            status = "baseline_candidate" if item and recent else "baseline_ignored"
            if not recent:
                reason = (
                    "дата публикации не распознана"
                    if not published
                    else f"объявление старше первичных {cfg['baseline_fresh_days']} суток"
                )
        elif item:
            started = datetime.fromisoformat(self.meta("baseline_started_at"))
            published = datetime.fromisoformat(published_at) if published_at else None
            status = "needs_detail" if published and published >= started else "ignored"
            if not published:
                reason = "дата публикации не распознана"
            elif published < started:
                reason = "объявление опубликовано до запуска мониторинга"
        else:
            status = "ignored"
        with self.db:
            self.db.execute("INSERT INTO offers(id,first_seen,status,raw,item,reason) VALUES(?,?,?,?,?,?)",
                            (oid, now(), status, json.dumps(raw, ensure_ascii=False),
                             json.dumps(item, ensure_ascii=False) if item else None, reason))
        return True

    def pending(self, status: str, timestamp=None):
        return self.db.execute("SELECT * FROM offers WHERE status=? AND retry_at<=? ORDER BY first_seen,id",
                               (status, time.time() if timestamp is None else timestamp)).fetchall()

    def set_status(self, oid, status, reason="", *, delay=0):
        with self.db:
            self.db.execute("UPDATE offers SET status=?,reason=?,retry_at=? WHERE id=?",
                            (status, reason, time.time() + delay, oid))

    def ready(self, oid: str, item: dict):
        with self.db:
            self.db.execute("UPDATE offers SET item=?,status='ready',reason='',retry_at=0 WHERE id=?",
                            (json.dumps(item, ensure_ascii=False), oid))

    def baseline_ready(self, oid: str, item: dict):
        with self.db:
            self.db.execute("UPDATE offers SET item=?,status='baseline',reason='',retry_at=0 WHERE id=?",
                            (json.dumps(item, ensure_ascii=False), oid))

    def baseline_items(self):
        return [json.loads(row[0]) for row in self.db.execute(
            "SELECT item FROM offers WHERE status='baseline' AND item IS NOT NULL ORDER BY first_seen,id")]

    def update_item(self, oid: str, item: dict):
        """Replace parsed fields without changing delivery/discovery state."""
        with self.db:
            self.db.execute("UPDATE offers SET item=? WHERE id=?",
                            (json.dumps(item, ensure_ascii=False), oid))

    def detail_failed(self, row, cfg: dict, reason: str):
        attempts = row["attempts"] + 1
        with self.db:
            self.db.execute("UPDATE offers SET attempts=? WHERE id=?", (attempts, row["id"]))
        self.set_status(row["id"], "review" if attempts >= cfg["detail_retries"] else "needs_detail",
                        reason, delay=cfg["interval"])

    def retry_uncertain(self):
        with self.db:
            self.db.execute("UPDATE offers SET status='ready',retry_at=0 WHERE status='uncertain'")

    def summary(self):
        return dict(self.db.execute("SELECT status,COUNT(*) FROM offers GROUP BY status").fetchall())


def classify_author(detail: dict) -> tuple[str, str | None]:
    """Return the requested public author category and displayed name."""
    author_text = re.sub(r"\s+", " ", str(detail.get("author_text", ""))).strip()
    author_name = re.sub(r"\s+", " ", str(detail.get("author_name", ""))).strip()
    author_norm = norm(author_text + " " + author_name)
    if "агентство недвижимости" in author_norm or "агентство" in author_norm:
        author_type = "Агентство недвижимости"
    elif re.search(r"ри[еэ]лтор|\bагент\b|маклер|представитель собственника", author_norm):
        author_type = "Риэлтор"
    elif "собственник" in author_norm:
        author_type = "Собственник"
    else:
        return "Не указан", None
    if not author_name:
        pattern = {
            "Собственник": r"собственник\s*[:—-]?\s*([^|,;]{2,100})",
            "Риэлтор": r"(?:ри[еэ]лтор|агент|маклер|представитель собственника)\s*[:—-]?\s*([^|,;]{2,100})",
            "Агентство недвижимости": r"агентство(?: недвижимости)?\s*[:—-]?\s*([^|,;]{2,100})",
        }[author_type]
        inferred = re.search(pattern, author_text, re.I)
        author_name = inferred[1].strip(" «»\"") if inferred else ""
    return author_type, author_name or None


def trusted_photo_urls(urls, limit=3) -> list[str]:
    """Keep unique public Cian listing images in their displayed order."""
    photos = []
    for url in urls or []:
        u = urlparse(str(url))
        host = (u.hostname or "").lower()
        if (u.scheme == "https" and (host == "cdn-cian.ru" or host.endswith(".cdn-cian.ru"))
                and u.path.startswith("/images/") and url not in photos):
            photos.append(url)
            if len(photos) >= limit:
                break
    return photos


def validate_detail(item: dict, detail: dict, cfg: dict):
    if detail.get("inactive"):
        return None, "объявление снято с публикации"
    if detail.get("blocked"):
        return None, "проверка браузера"
    room = ROOM_RE.search(detail.get("title", ""))
    if "студи" in norm(detail.get("title", "")) or not room or int(room[1]) not in cfg["rooms"]:
        return None, "комнатность не подтверждена на странице"
    price = PRICE_RE.search(detail.get("price_text", ""))
    if not price or not cfg["min_rent"] <= amount(price[1]) <= cfg["max_rent"]:
        return None, "цена на странице не проходит лимит или не распознана"
    floor = re.fullmatch(r"(\d+)\s*(?:из|/)\s*(\d+)", detail.get("floor_text", "").strip())
    if not floor or int(floor[1]) < cfg["min_floor"]:
        return None, "этаж на странице не проходит фильтр или не распознан"
    repair = str(detail.get("repair", "") or "").strip()
    repair_missing = not repair
    if not repair_missing and norm(repair) not in {norm(x) for x in cfg["allowed_repairs"]}:
        return None, "ремонт не подходит: " + repair
    if repair_missing:
        repair = "не указан"
    photos = trusted_photo_urls(detail.get("photos"), 3)
    rent = amount(price[1])

    def cash(text, *, zero_words=()):
        value = norm(text)
        if value and any(word in value for word in zero_words):
            return 0
        match = re.search(MONEY + r"\s*₽", text or "", re.I)
        return amount(match[1]) if match else None

    deposit = cash(detail.get("deposit", ""), zero_words=("без залога", "нет"))
    if deposit is None:
        deposit = item.get("deposit_rub")
    commission_text = detail.get("commission", "")
    commission = cash(commission_text, zero_words=("без комиссии", "нет"))
    commission_pct = None
    pct = re.search(r"(\d+(?:[.,]\d+)?)\s*%", commission_text or "")
    if pct:
        commission_pct = float(pct[1].replace(",", "."))
        commission = round(rent * commission_pct / 100)
    elif commission is None:
        commission = item.get("commission_rub")
        commission_pct = item.get("commission_pct")

    prepayment = detail.get("prepayment", "").strip()
    months_match = re.search(r"(\d+)\s*месяц", norm(prepayment))
    prepayment_months = max(1, int(months_match[1])) if months_match else 1
    entry_estimated = not bool(months_match)
    entry = rent * prepayment_months + deposit + commission \
        if deposit is not None and commission is not None else None
    year_match = re.search(r"(?<!\d)(18\d{2}|19\d{2}|20\d{2}|21\d{2})(?!\d)", detail.get("house_year", ""))

    author_type, author_name = classify_author(detail)

    result = dict(item, rent_rub=rent, rooms=int(room[1]), floor=int(floor[1]),
                  floors_total=int(floor[2]), repair=repair,
                  repair_missing=repair_missing, photos=photos,
                  deposit_rub=deposit, commission_rub=commission,
                  commission_pct=commission_pct, prepayment=prepayment,
                  prepayment_months=prepayment_months, entry_base_rub=entry,
                  entry_estimated=entry_estimated, house_year=int(year_match[1]) if year_match else None,
                  author_type=author_type, author_name=author_name,
                  checked_at=now())
    return result, ""


def deliver(store: Store, bot, cfg: dict, log=print):
    for row in store.pending("ready"):
        store.set_status(row["id"], "sending")  # Durable before the network call.
        try:
            result = bot.listing(json.loads(row["item"]), cfg["max_photos"])
        except TelegramError as e:
            if e.uncertain:
                store.set_status(row["id"], "uncertain", str(e))
            elif e.code in (401, 403):
                store.set_status(row["id"], "ready", str(e), delay=cfg["interval"])
                raise  # Stop; token revoked or recipient blocked the bot.
            else:
                store.set_status(row["id"], "ready", str(e), delay=max(e.retry_after, cfg["interval"]))
            log(f"ID {row['id']}: {e}")
            if e.code == 429 or e.uncertain:
                break  # Avoid a burst of failing/ambiguous sends.
        else:
            with store.db:
                store.db.execute("UPDATE offers SET status='sent',reason='',telegram_result=? WHERE id=?",
                                 (json.dumps(result, ensure_ascii=False), row["id"]))
            log(f"Отправлено: {row['id']}")
