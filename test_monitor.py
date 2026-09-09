import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from urllib.parse import parse_qs, urlparse

import settings
from cian_parser import CollectionError, build_url, parse_card
from monitor_core import Store, classify_author, deliver, load_settings, validate_detail
from send_today import delivery_queue, select_today
from telegram_client import Telegram, TelegramError, format_listing, matching_private_chat
from watcher import SingleInstance, scan_cycle


def card(oid="123456789", price=65000):
    return {
        "url": f"https://www.cian.ru/rent/flat/{oid}/",
        "title": "2-комн. квартира, 37,6 м², 4/9 этаж",
        "price_text": f"{price} ₽/мес.\nОт года, без комиссии, залог 60000 ₽",
        "metro": [{"text": "Зюзино 7 минут пешком", "links": [{"text": "Зюзино", "href": "https://www.cian.ru/snyat-kvartiru-moskva-metro-zyuzino-389/"}]}],
        "address": "Москва, Севастопольский проспект, 46к3", "description": "Евроремонт",
        "updated_text": "только что",
    }


DETAIL = {"title": "Сдается 2-комн. квартира, 37,6 м²", "floor_text": "4 из 9",
          "price_text": "65 000 ₽/мес.", "repair": "Евроремонт", "prepayment": "1 месяц",
          "deposit": "60 000 ₽", "commission": "без комиссии", "house_year": "2018",
          "author_text": "Собственник\nАнна", "author_name": "Анна",
          "photos": ["https://images.cdn-cian.ru/images/123-1.jpg", "https://images.cdn-cian.ru/images/124-1.jpg"]}


class Bot:
    def __init__(self, error=None):
        self.items, self.error = [], error

    def listing(self, item, max_photos):
        self.items.append(item)
        if self.error:
            raise self.error
        return {"message_id": len(self.items)}


class Browser:
    def __init__(self, pages, detail=None):
        self.pages, self.detail = pages, detail or DETAIL
        self.details_read = []

    def read_search(self, number):
        page = self.pages[number - 1]
        if isinstance(page, Exception):
            raise page
        return page

    def read_detail(self, url):
        self.details_read.append(url)
        if isinstance(self.detail, Exception):
            raise self.detail
        return self.detail


def page(*cards, next_url=None):
    return {"cards": list(cards), "next_url": next_url, "empty": not cards}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.store = Store(self.path)
        self.cfg = load_settings(settings)
        self.store.configure(self.cfg)
        self.bot = Bot()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def status(self, oid="123456789"):
        return self.store.db.execute("SELECT status FROM offers WHERE id=?", (oid,)).fetchone()[0]

    def enqueue(self, oid="123456789"):
        raw = card(oid)
        self.store.observe(raw, self.cfg, False)
        item, _ = parse_card(raw, self.cfg, "test")
        item, _ = validate_detail(item, DETAIL, self.cfg)
        self.store.ready(oid, item)

    def test_first_scan_validates_and_exports_fresh_baseline_without_notifications(self):
        browser = Browser([page(card(), card(price=80000))])
        result = scan_cycle(self.store, browser, self.bot, self.cfg,
                            baseline_folder=Path(self.temp.name))
        self.assertTrue(result["baseline"])
        self.assertEqual(result["baseline_count"], 1)
        self.assertTrue(self.store.initialized)
        self.assertEqual(self.status(), "baseline")
        self.assertEqual(browser.details_read, [card()["url"]])
        self.assertEqual(self.bot.items, [])
        self.assertEqual(len(json.loads((Path(self.temp.name) / "baseline.json").read_text())), 1)
        self.assertTrue((Path(self.temp.name) / "baseline.csv").exists())

    def test_baseline_ignores_listings_older_than_three_days(self):
        old = card() | {"updated_text": "4 дня назад"}
        result = scan_cycle(self.store, Browser([page(old)]), self.bot, self.cfg)
        self.assertEqual(result["baseline_count"], 0)
        self.assertEqual(self.status(), "baseline_ignored")
        self.assertEqual(self.bot.items, [])

    def test_new_only_duplicates_and_old_price_drop(self):
        scan_cycle(self.store, Browser([page(card(price=80000))]), self.bot, self.cfg)
        new = card("999")
        scan_cycle(self.store, Browser([page(card(price=65000), new, new)]), self.bot, self.cfg)
        scan_cycle(self.store, Browser([page(new, card())]), self.bot, self.cfg)
        self.assertEqual([x["id"] for x in self.bot.items], ["999"])

    def test_restart_keeps_history_and_sends_new_id(self):
        scan_cycle(self.store, Browser([page(card())]), self.bot, self.cfg)
        self.store.close()
        self.store = Store(self.path)
        self.assertFalse(self.store.configure(self.cfg))
        scan_cycle(self.store, Browser([page(card(), card("999"))]), self.bot, self.cfg)
        self.assertEqual([x["id"] for x in self.bot.items], ["999"])

    def test_partial_baseline_never_becomes_complete(self):
        browser = Browser([page(card(), next_url="https://www.cian.ru/cat.php?p=2"), CollectionError("offline")])
        with self.assertRaises(CollectionError):
            scan_cycle(self.store, browser, self.bot, self.cfg)
        self.assertFalse(self.store.initialized)
        scan_cycle(self.store, Browser([page(card(), card("999"))]), self.bot, self.cfg)
        self.assertEqual(self.bot.items, [])
        self.assertTrue(self.store.initialized)

    def test_max_pages_and_repeated_pages_are_incomplete(self):
        self.cfg["max_pages"] = 1
        with self.assertRaises(CollectionError):
            scan_cycle(self.store, Browser([page(card(), next_url="https://www.cian.ru/cat.php?p=2")]), self.bot, self.cfg)
        self.assertFalse(self.store.initialized)
        self.cfg["max_pages"] = 2
        with self.assertRaises(CollectionError):
            scan_cycle(self.store, Browser([page(card(), next_url="https://www.cian.ru/cat.php?p=2"), page(card())]), self.bot, self.cfg)
        self.assertFalse(self.store.initialized)

    def test_first_floor_and_unknown_floor_rejected(self):
        for title in ("1-комн. квартира, 30 м², 1/9 этаж", "1-комн. квартира, 30 м²"):
            self.assertIsNone(parse_card(card() | {"title": title}, self.cfg, "test")[0])

    def test_detail_strict_filters_and_photo_domain(self):
        item, _ = parse_card(card(), self.cfg, "test")
        for delta in ({"repair": "Косметический"}, {"repair": ""}, {"floor_text": "1 из 9"},
                      {"floor_text": ""}, {"price_text": "70001 ₽/мес."}, {"inactive": True},
                      {"title": "Квартира-студия"}, {"blocked": True}):
            self.assertIsNone(validate_detail(item, DETAIL | delta, self.cfg)[0], delta)
        result, _ = validate_detail(item, DETAIL | {"repair": "Дизайнерский", "photos": DETAIL["photos"] + ["https://cdn-cian.ru.evil.example/images/1.jpg"]}, self.cfg)
        self.assertEqual(result["photos"], DETAIL["photos"])
        self.assertEqual(result["prepayment"], "1 месяц")
        self.assertEqual((result["entry_base_rub"], result["house_year"]), (125000, 2018))
        self.assertEqual((result["author_type"], result["author_name"]), ("Собственник", "Анна"))

    def test_author_is_classified_without_affecting_filters(self):
        item, _ = parse_card(card(), self.cfg, "test")
        cases = (
            ({"author_text": "Риэлтор", "author_name": "Иван Петров"}, "Риэлтор", "Иван Петров"),
            ({"author_text": "Агентство недвижимости", "author_name": "Этажи"},
             "Агентство недвижимости", "Этажи"),
            ({"author_text": "", "author_name": ""}, "Не указан", None),
        )
        for delta, kind, name in cases:
            result, _ = validate_detail(item, DETAIL | delta, self.cfg)
            self.assertEqual((result["author_type"], result["author_name"]), (kind, name))

    def test_cian_company_profile_has_priority_over_realtor_text(self):
        detail = {
            "author_text": "АГЕНТСТВО НЕДВИЖИМОСТИ\nИНКОМ-Недвижимость Аренда",
            "author_name": "ИНКОМ-Недвижимость Аренда",
        }
        self.assertEqual(classify_author(detail),
                         ("Агентство недвижимости", "ИНКОМ-Недвижимость Аренда"))

    def test_edit_metro_list_works_and_expensive_ids_discovered(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["station_map"] = {"Зюзино": [389]}
        cfg["stations"] = ["Зюзино"]
        query = parse_qs(urlparse(build_url(cfg, include_price=False)).query)
        self.assertNotIn("maxprice", query)
        self.assertEqual(query["metro[0]"], ["389"])
        self.assertEqual(query["foot_min"], ["10"])

    def test_config_change_rebaselines_without_flood(self):
        scan_cycle(self.store, Browser([page(card())]), self.bot, self.cfg)
        self.enqueue("999")
        cfg = self.cfg | {"max_rent": 69000}
        self.assertTrue(self.store.configure(cfg))
        self.assertFalse(self.store.initialized)
        self.assertIsNone(self.store.db.execute("SELECT 1 FROM offers WHERE id='999'").fetchone())
        scan_cycle(self.store, Browser([page(card(), card("888"))]), self.bot, cfg)
        self.assertEqual(self.bot.items, [])

    def test_send_success_is_persisted_and_not_repeated(self):
        self.enqueue()
        deliver(self.store, self.bot, self.cfg, lambda _: None)
        deliver(self.store, self.bot, self.cfg, lambda _: None)
        self.assertEqual(self.status(), "sent")
        self.assertEqual(len(self.bot.items), 1)

    def test_ambiguous_network_error_is_not_automatically_replayed(self):
        self.enqueue()
        bot = Bot(TelegramError(uncertain=True))
        deliver(self.store, bot, self.cfg, lambda _: None)
        self.assertEqual(self.status(), "uncertain")
        deliver(self.store, bot, self.cfg, lambda _: None)
        self.assertEqual(len(bot.items), 1)
        self.store.retry_uncertain()
        deliver(self.store, self.bot, self.cfg, lambda _: None)
        self.assertEqual(self.status(), "sent")

    def test_crash_while_sending_is_marked_uncertain_on_restart(self):
        self.enqueue()
        self.store.set_status("123456789", "sending")
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(self.status(), "uncertain")

    def test_rate_limit_keeps_outbox_and_defers_delivery(self):
        self.enqueue()
        deliver(self.store, Bot(TelegramError(429, retry_after=1200)), self.cfg, lambda _: None)
        self.assertEqual(self.status(), "ready")
        self.assertEqual(self.store.pending("ready"), [])
        self.store.set_status("123456789", "ready")
        deliver(self.store, self.bot, self.cfg, lambda _: None)
        self.assertEqual(self.status(), "sent")

    def test_revoked_token_preserves_queue_and_stops(self):
        self.enqueue()
        with self.assertRaises(TelegramError):
            deliver(self.store, Bot(TelegramError(401)), self.cfg, lambda _: None)
        self.assertEqual(self.status(), "ready")

    def test_detail_errors_retry_then_go_to_review(self):
        self.store.baseline_complete()
        browser = Browser([page(card())], detail=CollectionError("offline"))
        for i in range(3):
            if i:
                self.store.set_status("123456789", "needs_detail")
            scan_cycle(self.store, browser, self.bot, self.cfg)
        self.assertEqual(self.status(), "review")
        self.assertEqual(self.bot.items, [])

    def test_single_instance_blocks_second_process_and_releases(self):
        path = Path(self.temp.name) / "monitor.lock"
        with SingleInstance(path):
            with self.assertRaises(CollectionError):
                with SingleInstance(path):
                    self.fail("second lock should not be allowed")
        with SingleInstance(path):
            pass

    def test_manual_today_selection_uses_moscow_date_and_sorts(self):
        rows = [
            {"id": "2", "published_at": "2026-09-08T22:30:00+00:00"},
            {"id": "1", "published_at": "2026-09-08T21:00:00+00:00"},
            {"id": "old", "published_at": "2026-09-07T20:00:00+00:00"},
            {"id": "unknown", "published_at": None},
        ]
        self.assertEqual([row["id"] for row in select_today(rows, "2026-09-09")], ["1", "2"])

    def test_manual_resend_explicitly_requeues_sent_and_uncertain(self):
        selected = [{"id": "1"}, {"id": "2"}, {"id": "3"}]
        state = {"date": "2026-09-08", "offers": {
            "1": "sent", "2": "uncertain"
        }}
        pending, skipped = delivery_queue(selected, state)
        self.assertEqual(([item["id"] for item in pending], skipped), (["3"], 2))
        pending, skipped = delivery_queue(selected, state, resend=True)
        self.assertEqual(([item["id"] for item in pending], skipped), (["1", "2", "3"], 0))
        self.assertEqual(state["offers"], {})


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_settings(settings)
        item, _ = parse_card(card(), self.cfg, "test")
        self.item, _ = validate_detail(item, DETAIL, self.cfg)
        self.token = "12345:" + "x" * 30

    def recording_bot(self, outcomes):
        bot = Telegram(self.token, 123)
        bot.calls = []
        bot.download_photo = lambda url, referer: ("cian.jpg", "image/jpeg", b"\xff\xd8\xfftest")

        def outcome(method, payload):
            bot.calls.append((method, payload))
            value = outcomes.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        def call(method, **payload):
            return outcome(method, payload)

        def call_multipart(method, fields, files):
            return outcome(method, fields | {"_files": files})

        bot.call = call
        bot.call_multipart = call_multipart
        return bot

    def test_album_single_photo_and_link_fallbacks(self):
        bot = self.recording_bot([TelegramError(400), TelegramError(400), {"message_id": 1}])
        bot.listing(self.item, 5)
        self.assertEqual([x[0] for x in bot.calls], ["sendMediaGroup", "sendPhoto", "sendMessage"])
        media = bot.calls[0][1]["media"]
        self.assertEqual(len(media), 2)
        self.assertIn(self.item["url"], media[0]["caption"])
        self.assertEqual(len(bot.calls[0][1]["_files"]), 2)

    def test_exactly_first_three_photos_are_uploaded_as_files(self):
        bot = self.recording_bot([[{"message_id": 1}]])
        photos = [f"https://images.cdn-cian.ru/images/{index}-1.jpg" for index in range(4)]
        bot.listing(self.item | {"photos": photos}, 10)
        method, payload = bot.calls[0]
        self.assertEqual(method, "sendMediaGroup")
        self.assertEqual(len(payload["media"]), 3)
        self.assertEqual(len(payload["_files"]), 3)
        self.assertEqual([entry["media"] for entry in payload["media"]],
                         ["attach://photo0", "attach://photo1", "attach://photo2"])

    def test_no_photo_still_sends_link(self):
        bot = self.recording_bot([{}])
        bot.listing(self.item | {"photos": []}, 5)
        self.assertEqual(bot.calls[0][0], "sendMessage")
        self.assertIn(self.item["url"], bot.calls[0][1]["text"])

    def test_ambiguous_error_and_rate_limit_never_fallback_to_duplicate(self):
        for err in (TelegramError(uncertain=True), TelegramError(429), TelegramError(401)):
            bot = self.recording_bot([err])
            with self.assertRaises(TelegramError):
                bot.listing(self.item, 5)
            self.assertEqual(len(bot.calls), 1)

    def test_caption_under_api_limit_even_with_long_text(self):
        msg = format_listing(self.item | {"address": "А" * 2000, "flags": "🙂" * 2000, "utilities": "Б" * 2000})
        self.assertLessEqual(len(msg.encode("utf-16-le")) // 2, 1024)
        self.assertTrue(msg.endswith(self.item["url"]))

    def test_card_contains_requested_fields_and_line_emoji(self):
        msg = format_listing(self.item)
        for expected in ("Аренда: 65 000 ₽/мес.", "Цена входа: 125 000 ₽",
                         "Площадь: 37.6 м²", "Комнат: 2", "4 этаж из 9",
                         "Год постройки: 2018", "Севастопольский проспект",
                         "Зюзино (БКЛ)", "7 мин пешком", "Автор: Собственник — Анна",
                         "Опубликовано:"):
            self.assertIn(expected, msg)

    def test_custom_emoji_pack_ids_are_loaded_and_sent_as_html(self):
        bot = self.recording_bot([{"stickers": [
            {"emoji": "1️⃣1️⃣", "custom_emoji_id": "123456789"},
        ]}])
        bot.load_custom_emoji("mosgortransport")
        self.assertEqual(bot.custom_emoji_ids["11"]["id"], "123456789")
        msg = format_listing(self.item, bot.custom_emoji_ids)
        self.assertIn('<tg-emoji emoji-id="123456789">', msg)
        self.assertNotIn("(БКЛ)", msg)

    def test_pairing_ignores_old_start_wrong_nonce_and_groups(self):
        def update(text="/start nonce", kind="private", sender=10):
            return {"message": {"text": text, "chat": {"id": 10, "type": kind}, "from": {"id": sender}}}
        for wrong in (update("/start"), update("/start other"), update(kind="group"), update(sender=11)):
            self.assertIsNone(matching_private_chat([wrong], "nonce"))
        self.assertEqual(matching_private_chat([update()], "nonce")["id"], 10)

    def test_http_payload_and_redacted_errors(self):
        requests = []
        def success(req, timeout):
            requests.append(req)
            return io.BytesIO(b'{"ok":true,"result":{"message_id":5}}')
        bot = Telegram(self.token, 123, opener=success)
        self.assertEqual(bot.message("hello")["message_id"], 5)
        payload = json.loads(requests[0].data)
        self.assertEqual((payload["chat_id"], payload["text"]), (123, "hello"))
        def timeout(req, timeout):
            raise urllib.error.URLError(req.full_url)
        bot._open = timeout
        with self.assertRaises(TelegramError) as cm:
            bot.message("hello")
        self.assertTrue(cm.exception.uncertain)
        self.assertNotIn(self.token, str(cm.exception))

    def test_multipart_photo_upload_and_cian_download_validation(self):
        requests = []
        def success(req, timeout):
            requests.append(req)
            return io.BytesIO(b'{"ok":true,"result":[{"message_id":5}]}')
        bot = Telegram(self.token, 123, opener=success,
                       media_opener=lambda req, timeout: io.BytesIO(b"\xff\xd8\xffphoto"))
        downloaded = bot.download_photo("https://images.cdn-cian.ru/images/123-1.jpg",
                                        self.item["url"])
        self.assertEqual(downloaded[1:], ("image/jpeg", b"\xff\xd8\xffphoto"))
        self.assertIsNone(bot.download_photo("https://cdn-cian.ru.evil.test/images/123.jpg",
                                             self.item["url"]))
        bot.call_multipart("sendMediaGroup", {"chat_id": 123, "media": [
            {"type": "photo", "media": "attach://photo0"}
        ]}, [("photo0", "cian.jpg", "image/jpeg", downloaded[2])])
        self.assertIn("multipart/form-data", requests[0].headers["Content-type"])
        self.assertIn(b'attach://photo0', requests[0].data)
        self.assertIn(b'filename="cian.jpg"', requests[0].data)

    def test_http_429_retry_after_parsed(self):
        def limited(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 429, "rate limit", {}, io.BytesIO(b'{"parameters":{"retry_after":17}}'))
        with self.assertRaises(TelegramError) as cm:
            Telegram(self.token, 123, opener=limited).message("test")
        self.assertEqual(cm.exception.retry_after, 17)
        self.assertFalse(cm.exception.uncertain)


if __name__ == "__main__":
    unittest.main()
