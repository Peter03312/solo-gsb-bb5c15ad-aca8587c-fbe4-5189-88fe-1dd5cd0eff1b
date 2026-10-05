"""路线查询负载（含停靠点）校验测试。"""
import unittest

from app.schemas import ValidationError, normalize_query

NODES = {"GATE", "PLAZA", "TOWER"}
BASE = {
    "origin": "GATE",
    "destination": "TOWER",
    "departure_time": "2026-10-05T08:00:00Z",
}


class QueryValidationTests(unittest.TestCase):
    def test_no_stops_defaults_empty(self):
        q = normalize_query(BASE, NODES)
        self.assertEqual(q["stops"], [])

    def test_null_stops_treated_as_empty(self):
        q = normalize_query({**BASE, "stops": None}, NODES)
        self.assertEqual(q["stops"], [])

    def test_valid_stop_normalized(self):
        q = normalize_query({**BASE, "stops": [{
            "node": "PLAZA",
            "earliest_start": "2026-10-05T09:00:00Z",
            "latest_start": "2026-10-05T09:10:00Z",
            "dwell_seconds": 120,
        }]}, NODES)
        (stop,) = q["stops"]
        self.assertEqual(stop["node"], "PLAZA")
        self.assertEqual(stop["dwell_seconds"], 120)
        self.assertLess(stop["earliest_start"], stop["latest_start"])

    def test_zero_dwell_allowed(self):
        q = normalize_query({**BASE, "stops": [{
            "node": "PLAZA",
            "earliest_start": "2026-10-05T09:00:00Z",
            "latest_start": "2026-10-05T09:00:00Z",
            "dwell_seconds": 0,
        }]}, NODES)
        self.assertEqual(q["stops"][0]["dwell_seconds"], 0)

    def test_stops_must_be_array(self):
        with self.assertRaises(ValidationError):
            normalize_query({**BASE, "stops": {"node": "PLAZA"}}, NODES)

    def test_stop_must_be_object(self):
        with self.assertRaises(ValidationError):
            normalize_query({**BASE, "stops": ["PLAZA"]}, NODES)

    def test_unknown_stop_node(self):
        with self.assertRaises(ValidationError):
            normalize_query({**BASE, "stops": [{
                "node": "GHOST",
                "earliest_start": "2026-10-05T09:00:00Z",
                "latest_start": "2026-10-05T09:10:00Z",
                "dwell_seconds": 0,
            }]}, NODES)

    def test_negative_dwell_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_query({**BASE, "stops": [{
                "node": "PLAZA",
                "earliest_start": "2026-10-05T09:00:00Z",
                "latest_start": "2026-10-05T09:10:00Z",
                "dwell_seconds": -1,
            }]}, NODES)

    def test_float_and_bool_dwell_rejected(self):
        template = {
            "node": "PLAZA",
            "earliest_start": "2026-10-05T09:00:00Z",
            "latest_start": "2026-10-05T09:10:00Z",
        }
        for bad in (1.5, True, "60"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError):
                    normalize_query({**BASE, "stops": [{**template, "dwell_seconds": bad}]}, NODES)

    def test_window_earliest_after_latest_rejected(self):
        with self.assertRaises(ValidationError):
            normalize_query({**BASE, "stops": [{
                "node": "PLAZA",
                "earliest_start": "2026-10-05T09:10:00Z",
                "latest_start": "2026-10-05T09:00:00Z",
                "dwell_seconds": 0,
            }]}, NODES)

    def test_equal_window_endpoints_allowed(self):
        q = normalize_query({**BASE, "stops": [{
            "node": "PLAZA",
            "earliest_start": 0,
            "latest_start": 0,
            "dwell_seconds": 0,
        }]}, NODES)  # 纪元整数也合法
        self.assertEqual(q["stops"][0]["earliest_start"], 0)

    def test_bad_time_rejected(self):
        for field in ("earliest_start", "latest_start"):
            with self.subTest(field=field):
                stop = {
                    "node": "PLAZA",
                    "earliest_start": "2026-10-05T09:00:00Z",
                    "latest_start": "2026-10-05T09:10:00Z",
                    "dwell_seconds": 0,
                }
                stop[field] = "not-a-time"
                with self.assertRaises(ValidationError):
                    normalize_query({**BASE, "stops": [stop]}, NODES)

    def test_missing_field_rejected(self):
        complete = {
            "node": "PLAZA",
            "earliest_start": "2026-10-05T09:00:00Z",
            "latest_start": "2026-10-05T09:10:00Z",
            "dwell_seconds": 0,
        }
        for field in complete:
            partial = {k: v for k, v in complete.items() if k != field}
            with self.subTest(field=field):
                with self.assertRaises(ValidationError):
                    normalize_query({**BASE, "stops": [partial]}, NODES)


if __name__ == "__main__":
    unittest.main()
