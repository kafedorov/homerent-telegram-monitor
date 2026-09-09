import copy
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import settings
from cian_parser import (
    BASE, CollectionError, apply_history, build_url, load_config, parse_cian_datetime,
    next_page_number, parse_card, parse_pages, save_result,
)


class ParserTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(BASE / "config.json")
        # Synthetic public-card shape, with NBSP and comma decimal separators.
        self.card = {
            "url": "https://www.cian.ru/rent/flat/123456789/?context=x",
            "title": "2-комн. квартира, 37,6 м², 4/9 этаж",
            "price_text": "65\u00a0000 ₽/мес.\nОт года, комм. платежи включены (без счётчиков), без комиссии, залог 60\u00a0000 ₽",
            "address": "Москва, Севастопольский проспект, 46К3",
            "metro": [{"text": "Зюзино\n7 минут пешком", "links": [{"text": "Зюзино", "href": "https://www.cian.ru/cat.php?metro%5B0%5D=389"}]}],
            "description": "Сдаётся двухкомнатная квартира. Кухня 7,4 м².",
            "updated_text": "сегодня, 15:30",
        }

    def parse(self, **fields):
        return parse_card(self.card | fields, self.cfg, "2026-09-08T13:00:00Z")

    def test_realistic_money_and_dimensions(self):
        r, why = self.parse()
        self.assertEqual(why, "")
        self.assertEqual((r["rent_rub"], r["deposit_rub"], r["commission_rub"], r["entry_base_rub"]), (65000, 60000, 0, 125000))
        self.assertEqual((r["rooms"], r["area_m2"], r["floor"], r["floors_total"]), (2, 37.6, 4, 9))
        self.assertNotIn("?", r["url"])

    def test_budget_boundary_and_percent_fee(self):
        r, _ = self.parse(price_text="70 000 ₽/мес.\nОт года, комиссия 50%, залог 70 000 ₽")
        self.assertEqual(r["entry_base_rub"], 175000)
        self.assertIsNotNone(self.parse(price_text="55 000 ₽/мес.\nОт года, без комиссии, без залога")[0])
        self.assertIsNone(self.parse(price_text="54 999 ₽/мес.\nОт года")[0])
        self.assertIsNone(self.parse(price_text="70 001 ₽/мес.\nОт года")[0])

    def test_unknown_costs_are_not_zero(self):
        r, _ = self.parse(price_text="65 000 ₽/мес.\nОт года")
        self.assertIsNone(r["deposit_rub"])
        self.assertIsNone(r["commission_rub"])
        self.assertIsNone(r["entry_base_rub"])

    def test_studios_rooms_and_daily_rent_rejected(self):
        for title in ("Квартира-студия, 28 м², 1/9 этаж", "Апартаменты-студия, 28 м²", "Комната, 20 м²", "4-комн. квартира, 90 м²"):
            self.assertIsNone(self.parse(title=title)[0], title)
        self.assertIsNone(self.parse(description="Сдается уютная студия в новом доме.")[0])
        self.assertIsNotNone(self.parse(description="Это не студия, кухня отдельная.")[0])
        self.assertIsNone(self.parse(price_text="5 000 ₽/сут.\nОт года")[0])
        self.assertIsNone(self.parse(price_text="65 000 ₽/мес.\nОт месяца")[0])

    def test_wrong_station_not_matched_from_street_or_description(self):
        self.assertIsNone(self.parse(metro=[], address="Москва, Профсоюзная улица", description="Беляево, рядом метро")[0])
        mcd = [{"text": "Аминьевская\n5 минут пешком", "links": [{"text": "Аминьевская", "href": "https://www.cian.ru/snyat-kvartiru-moskva-metro-aminevskaya-507/"}]}]
        self.assertEqual(self.parse(metro=mcd)[0]["metro"], "Аминьевская")
        wrong = copy.deepcopy(mcd)
        wrong[0]["links"][0]["href"] = "https://www.cian.ru/snyat-kvartiru-moskva-metro-aminevskaya-9999/"
        self.assertIsNone(self.parse(metro=wrong)[0])

    def test_walking_time_does_not_accept_transport(self):
        self.cfg["max_walk_minutes"] = 10
        self.assertIsNotNone(self.parse()[0])
        for text in ("Зюзино\n11 минут пешком", "Зюзино\n5 минут на транспорте", "Зюзино"):
            metro = copy.deepcopy(self.card["metro"])
            metro[0]["text"] = text
            self.assertIsNone(self.parse(metro=metro)[0])

    def test_zero_costs_and_fixed_ruble_commission(self):
        r, _ = self.parse(price_text="65 000 ₽/мес.\nОт года, без залога, комиссия 30 000 ₽")
        self.assertEqual((r["deposit_rub"], r["commission_rub"], r["entry_base_rub"]), (0, 30000, 95000))

    def test_regional_cian_domain_keeps_original_url(self):
        url = "https://zvenigorod.cian.ru/rent/flat/123456789/"
        self.assertEqual(self.parse(url=url)[0]["url"], url)
        self.assertIsNone(self.parse(url="https://cian.ru.evil.example/rent/flat/123456789/")[0])

    def test_search_filters_include_all_selected_stations_and_lines(self):
        query = parse_qs(urlparse(build_url(self.cfg, 2)).query)
        ids = {int(v[0]) for k, v in query.items() if k.startswith("metro[")}
        expected = {mid for mids in settings.METRO_STATIONS.values() for mid in mids}
        self.assertEqual(len(settings.METRO_STATIONS), 51)
        self.assertEqual(ids, expected)
        self.assertEqual([query[f"room{i}"][0] for i in (1, 2)], ["1", "1"])
        self.assertNotIn("room3", query)
        self.assertNotIn("room9", query)
        self.assertEqual(query["minprice"], ["55000"])
        self.assertEqual(query["maxprice"], ["70000"])
        self.assertEqual(query["p"], ["2"])
        self.assertEqual(query["sort"], ["creation_date_desc"])

    def test_cian_publication_time_is_normalized_to_utc(self):
        captured = "2026-09-08T13:00:00+00:00"
        self.assertEqual(parse_cian_datetime("сегодня, 15:30", captured),
                         "2026-09-08T12:30:00+00:00")
        self.assertEqual(parse_cian_datetime("вчера, 00:15", captured),
                         "2026-09-06T21:15:00+00:00")
        self.assertEqual(parse_cian_datetime("4 дня назад", captured),
                         "2026-09-04T13:00:00+00:00")

    def test_deduplication_and_repeated_run_price_change(self):
        p = {"cards": [self.card, self.card], "captured_at": "2026-09-08T13:00:00Z"}
        rows, _ = parse_pages([p, p], self.cfg)
        self.assertEqual(len(rows), 1)
        state = apply_history(rows, {})
        self.assertEqual(rows[0]["status"], "новое")
        apply_history(rows, state)
        self.assertEqual(rows[0]["status"], "без изменений")
        rows[0]["rent_rub"] = 64000
        apply_history(rows, state)
        self.assertEqual(rows[0]["status"], "цена изменилась")
        self.assertEqual(rows[0]["previous_rent_rub"], 65000)

    def test_pagination_does_not_follow_foreign_urls_or_repeat_pages(self):
        self.assertIsNone(next_page_number(None, 1))
        self.assertEqual(next_page_number("https://www.cian.ru/cat.php?p=2", 1), 2)
        for bad in ("https://evil.example/cat.php?p=2", "https://www.cian.ru/cat.php?p=1", "https://www.cian.ru/rent/flat/123/?p=2"):
            with self.assertRaises(CollectionError):
                next_page_number(bad, 1)

    def test_csv_formula_safety_partial_status_and_empty_rerun(self):
        with tempfile.TemporaryDirectory() as td:
            self.cfg["_base"] = td
            raw = self.card | {"address": "=HYPERLINK(\"https://example.com\")"}
            page = {"cards": [raw], "captured_at": "2026-09-08T13:00:00Z"}
            run, _ = save_result([page], self.cfg, False, "Остановлено")
            self.assertIn("'=HYPERLINK", (run / "apartments.csv").read_text(encoding="utf-8-sig"))
            self.assertFalse(json.loads((run / "summary.json").read_text())["complete"])
            state_before = json.loads((Path(td) / "results/seen.json").read_text())
            save_result([{"cards": [], "empty": True}], self.cfg, True, "Нет вариантов")
            self.assertEqual(state_before, json.loads((Path(td) / "results/seen.json").read_text()))


if __name__ == "__main__":
    unittest.main()
