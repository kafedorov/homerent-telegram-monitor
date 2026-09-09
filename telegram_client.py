"""Telegram Bot API via HTTPS. Credentials stay on the user's computer."""
from __future__ import annotations

import getpass
import html
import json
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


LINE_LABELS = {
    "1": "красная", "3": "синяя", "4": "голубая", "5": "коричневая",
    "6": "оранжевая", "7": "фиолетовая", "8A": "жёлтая",
    "11": "БКЛ", "14": "МЦК", "16": "изумрудная",
    "D1": "Д1", "D4": "Д4",
}


class TelegramError(RuntimeError):
    def __init__(self, code=0, *, uncertain=False, retry_after=0):
        self.code = code
        self.uncertain = uncertain
        self.retry_after = retry_after
        # Never include a request URL, token or raw response in logs.
        super().__init__(f"Telegram: код {code or 'сеть'}, " +
                         ("доставка не подтверждена" if uncertain else "запрос отклонён"))


class Telegram:
    def __init__(self, token: str, chat_id: int | None = None, opener=None, media_opener=None):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Некорректный формат токена Telegram")
        self._token = token
        self.chat_id = chat_id
        self._open = opener or urllib.request.urlopen
        self._media_open = media_opener or urllib.request.urlopen
        self.custom_emoji_ids = {}

    def _perform(self, req):
        try:
            with self._open(req, timeout=45) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            try:
                data = json.loads(e.read())
                retry = int(data.get("parameters", {}).get("retry_after", 0))
            except (ValueError, TypeError):
                retry = 0
            raise TelegramError(e.code, uncertain=e.code >= 500, retry_after=retry) from None
        except (OSError, urllib.error.URLError):
            # A timeout may happen AFTER Telegram accepted the message.
            raise TelegramError(uncertain=True) from None
        try:
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError()
        except (ValueError, TypeError):
            raise TelegramError(uncertain=True) from None
        if not result.get("ok"):
            code = result.get("error_code", 0)
            raise TelegramError(code, uncertain=not code or code >= 500,
                                retry_after=result.get("parameters", {}).get("retry_after", 0))
        return result["result"]

    def call(self, method: str, **payload):
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        return self._perform(req)

    def call_multipart(self, method: str, fields: dict, files: list[tuple[str, str, str, bytes]]):
        boundary = "----cianbot" + secrets.token_hex(16)
        body = bytearray()

        def line(value=b""):
            body.extend(value if isinstance(value, bytes) else str(value).encode("utf-8"))
            body.extend(b"\r\n")

        for name, value in fields.items():
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            line("--" + boundary)
            line(f'Content-Disposition: form-data; name="{name}"')
            line()
            line(value)
        for field_name, filename, content_type, content in files:
            line("--" + boundary)
            line(f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"')
            line(f"Content-Type: {content_type}")
            line()
            body.extend(content)
            body.extend(b"\r\n")
        line("--" + boundary + "--")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{self._token}/{method}", data=bytes(body),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"}, method="POST")
        return self._perform(req)

    def download_photo(self, url: str, referer: str):
        """Fetch one trusted Cian image locally so Telegram needn't fetch its URL."""
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if (parsed.scheme != "https" or not (host == "cdn-cian.ru" or host.endswith(".cdn-cian.ru"))
                or not parsed.path.startswith("/images/")):
            return None
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "image/avif,image/webp,image/png,image/jpeg,*/*;q=0.8",
            "Referer": referer,
        })
        try:
            with self._media_open(req, timeout=30) as response:
                content = response.read(10_000_001)
        except (OSError, urllib.error.URLError, urllib.error.HTTPError):
            return None
        if not content or len(content) > 10_000_000:
            return None
        if content.startswith(b"\xff\xd8\xff"):
            extension, content_type = "jpg", "image/jpeg"
        elif content.startswith(b"\x89PNG\r\n\x1a\n"):
            extension, content_type = "png", "image/png"
        elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            extension, content_type = "webp", "image/webp"
        else:
            return None
        return f"cian.{extension}", content_type, content

    def load_custom_emoji(self, pack_name: str):
        """Load the pack's public custom emoji IDs; text fallback remains on errors."""
        if not pack_name:
            return
        try:
            sticker_set = self.call("getStickerSet", name=pack_name)
        except TelegramError:
            return
        result = {}
        for sticker in sticker_set.get("stickers", []):
            custom_id = sticker.get("custom_emoji_id")
            alternative = str(sticker.get("emoji", ""))
            compact = re.sub(r"[^0-9A-ZА-Я]", "", alternative.upper().replace("А", "A"))
            match = re.search(r"(?:D)?(?:1|3|4|5|6|7|8A|8|11|14|16)(?!\d)", compact)
            if custom_id and match:
                line = "8A" if match[0] == "8" else match[0]
                result.setdefault(line, {"id": custom_id, "emoji": alternative or "🚇"})
        self.custom_emoji_ids = result

    def message(self, text: str, *, parse_mode=None):
        payload = {"chat_id": self.chat_id, "text": text,
                   "link_preview_options": {"is_disabled": True}}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self.call("sendMessage", **payload)

    def listing(self, item: dict, max_photos: int):
        caption = format_listing(item, self.custom_emoji_ids)
        urls = item.get("photos", [])[:min(max_photos, 3)]
        photos = []
        for index, url in enumerate(urls):
            downloaded = self.download_photo(url, item["url"])
            if downloaded:
                filename, content_type, content = downloaded
                photos.append((f"photo{index}", filename, content_type, content))
        if len(photos) >= 2:
            media = [{"type": "photo", "media": f"attach://{field}"}
                     for field, _filename, _content_type, _content in photos]
            media[0]["caption"] = caption
            media[0]["parse_mode"] = "HTML"
            try:
                return self.call_multipart("sendMediaGroup",
                                           {"chat_id": self.chat_id, "media": media}, photos)
            except TelegramError as e:
                if e.code != 400 or e.uncertain:
                    raise
                # Explicit rejection means no album was accepted. Try one file.
        if photos:
            _field, filename, content_type, content = photos[0]
            try:
                return self.call_multipart(
                    "sendPhoto", {"chat_id": self.chat_id, "caption": caption,
                                  "parse_mode": "HTML"},
                    [("photo", filename, content_type, content)])
            except TelegramError as e:
                if e.code != 400 or e.uncertain:
                    raise
        return self.message(caption + "\nФото доступны по ссылке на ЦИАН.", parse_mode="HTML")


def format_listing(item: dict, custom_emoji_ids=None) -> str:
    def money(value):
        return f"{value:,}".replace(",", " ") + " ₽" if value is not None else "не указан"

    def safe(value, limit=180):
        return html.escape(str(value or ""))[:limit]

    custom_emoji_ids = custom_emoji_ids or {}
    rendered, missing = [], []
    for raw_line in item.get("metro_lines", []):
        line = str(raw_line)
        custom = custom_emoji_ids.get(line)
        if custom:
            custom_id = custom.get("id") if isinstance(custom, dict) else custom
            alternative = custom.get("emoji", "🚇") if isinstance(custom, dict) else "🚇"
            rendered.append(f'<tg-emoji emoji-id="{safe(custom_id, 40)}">{safe(alternative, 12)}</tg-emoji>')
        else:
            missing.append(LINE_LABELS.get(line, line))
    metro_prefix = (" ".join(rendered) + " ") if rendered else ""
    metro_suffix = f" ({', '.join(missing)})" if missing else ""
    metro = metro_prefix + safe(item.get("metro") or "не указано", 100) + metro_suffix
    published = item.get("updated_text") or "не указана"
    if item.get("published_at"):
        try:
            stamp = datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            published = stamp.astimezone(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y, %H:%M МСК")
        except (ValueError, TypeError):
            pass

    entry = item.get("entry_base_rub")
    if entry is None:
        entry_text = "не удалось точно рассчитать"
    else:
        prefix = "ориентировочно " if item.get("entry_estimated") else ""
        rent_part = item["rent_rub"] * item.get("prepayment_months", 1)
        entry_text = (f"{prefix}{money(entry)} (аренда авансом {money(rent_part)} + "
                      f"залог {money(item.get('deposit_rub'))} + комиссия {money(item.get('commission_rub'))})")

    lines = [
        "🏠 Новая квартира",
        *(["<b>❗❗ РЕМОНТ НЕ УКАЗАН ❗❗</b>"] if item.get("repair_missing") else []),
        f"💰 Аренда: {money(item['rent_rub'])}/мес.",
        f"💳 Цена входа: {entry_text}",
        f"📐 Площадь: {item.get('area_m2') or 'не указана'} м²",
        f"🚪 Комнат: {item['rooms']}",
        f"🏢 Этаж: {item['floor']} этаж из {item.get('floors_total') or '?'}",
        f"🏗 Год постройки: {item.get('house_year') or 'не указан'}",
        f"📍 Адрес: {safe(item.get('address') or 'не указан', 180)}",
        f"🚇 Метро: {metro}",
        f"🚶 До метро: {item['metro_minutes']} мин пешком",
        f"🛠 Ремонт: {safe(item.get('repair') or 'не указан', 80)}",
        f"👤 Автор: {safe(item.get('author_type') or 'не указан', 40)} — "
        f"{safe(item.get('author_name') or 'имя не указано', 100)}",
        f"🕒 Опубликовано: {published}",
    ]
    # Stay below Telegram's 1024-character album/photo caption limit.
    return "\n".join(lines) + "\n🔗 " + safe(item["url"], 180)


def matching_private_chat(updates: list, nonce: str):
    for update in updates:
        msg = update.get("message", {})
        chat = msg.get("chat", {})
        sender = msg.get("from", {})
        if (msg.get("text") == f"/start {nonce}" and chat.get("type") == "private"
                and chat.get("id") == sender.get("id") and not sender.get("is_bot")):
            return chat
    return None


def setup(path: Path) -> Telegram:
    print("Создайте отдельного бота: https://t.me/BotFather → /newbot.")
    print("Токен вводится только здесь, в консоли. В чат с помощником его отправлять не нужно.")
    token = getpass.getpass("Токен бота (ввод скрыт): ").strip()
    bot = Telegram(token)
    info = bot.call("getMe")
    if bot.call("getWebhookInfo").get("url"):
        raise ValueError("У этого бота настроен webhook. Создайте отдельного бота через /newbot.")
    nonce = secrets.token_urlsafe(18)
    link = f"https://t.me/{info['username']}?start={nonce}"
    print("Откройте ссылку и нажмите СТАРТ в личном чате с ботом:")
    print(link)
    deadline, offset = time.monotonic() + 600, 0
    while time.monotonic() < deadline:
        updates = bot.call("getUpdates", offset=offset, timeout=25, allowed_updates=["message"])
        if updates:
            offset = max(u["update_id"] for u in updates) + 1
        chat = matching_private_chat(updates, nonce)
        if chat:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(".tmp")
            # Restricts access on Unix. On Windows keep this personal folder private.
            temp.touch(mode=0o600, exist_ok=True)
            temp.write_text(json.dumps({"token": token, "chat_id": chat["id"]}), encoding="utf-8")
            temp.replace(path)
            bot.chat_id = chat["id"]
            print(f"Подключён личный чат: {chat.get('first_name', '')} (ID {chat['id']}).")
            bot.message("Бот подключён. Первый обход сформирует исходную выгрузку из объявлений за последние 3 суток; затем пришлю только новые подходящие квартиры.")
            return bot
    raise ValueError("За 10 минут не получена команда СТАРТ. Запустите настройку ещё раз.")


def load_or_setup(path: Path, force=False, *, custom_emoji_pack="") -> Telegram:
    if force or not path.exists():
        bot = setup(path)
    else:
        data = json.loads(path.read_text(encoding="utf-8"))
        if type(data.get("chat_id")) is not int:
            raise ValueError("Повреждён data/telegram.json. Запустите watcher.py --setup.")
        bot = Telegram(data["token"], data["chat_id"])
    bot.load_custom_emoji(custom_emoji_pack)
    return bot
