"""Unit tests for the pure geocoding helpers (no DB, no network).

Run from the repo root:  python -m pytest collectors/geocoding/ -v
"""
import unittest

from collectors.geocoding.addresses import (
    extract_house_number,
    is_numbered,
    normalize_address,
    split_numbered,
)
from collectors.geocoding.census import build_census_csv, parse_census_response
from collectors.geocoding.interpolate import build_known_points_by_street, interpolate


class NormalizeAddressTests(unittest.TestCase):
    def test_strips_condo_unit_suffixes(self):
        self.assertEqual(normalize_address("100 MARKET ST C001"), "100 MARKET ST")
        self.assertEqual(normalize_address("100 MARKET ST L0032"), "100 MARKET ST")

    def test_leaves_clean_address_and_trims(self):
        self.assertEqual(normalize_address("  123 MAIN ST  "), "123 MAIN ST")
        self.assertEqual(normalize_address("123 MAIN ST"), "123 MAIN ST")

    def test_empty(self):
        self.assertEqual(normalize_address(""), "")
        self.assertEqual(normalize_address(None), "")


class ExtractHouseNumberTests(unittest.TestCase):
    def test_numbered(self):
        self.assertEqual(extract_house_number("123 MAIN ST"), (123, "MAIN ST"))

    def test_uppercases_street(self):
        self.assertEqual(extract_house_number("123 main st"), (123, "MAIN ST"))

    def test_bare_street(self):
        self.assertEqual(extract_house_number("MAIN ST"), (None, "MAIN ST"))

    def test_empty(self):
        self.assertEqual(extract_house_number(""), (None, ""))

    def test_is_numbered_and_split(self):
        rows = [{"address": "1 A ST"}, {"address": "MAIN ST"}, {"address": "22 B AVE"}]
        self.assertTrue(is_numbered("9 X ST"))
        self.assertFalse(is_numbered("X ST"))
        numbered, bare = split_numbered(rows)
        self.assertEqual([r["address"] for r in numbered], ["1 A ST", "22 B AVE"])
        self.assertEqual([r["address"] for r in bare], ["MAIN ST"])


class InterpolateTests(unittest.TestCase):
    KNOWN = [(100, 35.0, -85.0), (200, 35.2, -85.2)]

    def test_midpoint_between_two_known_points(self):
        lat, lng = interpolate(150, self.KNOWN)
        self.assertAlmostEqual(lat, 35.1, places=9)
        self.assertAlmostEqual(lng, -85.1, places=9)

    def test_exact_match_returns_that_point(self):
        self.assertEqual(interpolate(100, self.KNOWN), (35.0, -85.0))
        self.assertEqual(interpolate(200, self.KNOWN), (35.2, -85.2))

    def test_single_point_returns_none(self):
        # Cannot bracket -> no fabrication.
        self.assertIsNone(interpolate(150, [(100, 35.0, -85.0)]))

    def test_out_of_range_returns_none(self):
        self.assertIsNone(interpolate(50, self.KNOWN))   # below all known
        self.assertIsNone(interpolate(300, self.KNOWN))  # above all known

    def test_empty_known_returns_none(self):
        self.assertIsNone(interpolate(150, []))

    def test_no_house_number_returns_none(self):
        self.assertIsNone(interpolate(None, self.KNOWN))

    def test_build_known_points_groups_and_sorts_by_street(self):
        rows = [
            ("p1", "200 OAK ST", 35.2, -85.2),
            ("p2", "100 OAK ST", 35.0, -85.0),
            ("p3", "OAK ST", 1.0, 1.0),       # no house number -> skipped
            ("p4", "50 ELM AVE", 36.0, -86.0),
        ]
        idx = build_known_points_by_street(rows)
        self.assertEqual(idx["OAK ST"], [(100, 35.0, -85.0), (200, 35.2, -85.2)])
        self.assertEqual(idx["ELM AVE"], [(50, 36.0, -86.0)])


class CensusParseTests(unittest.TestCase):
    SAMPLE = (
        '1,"123 MAIN ST, CHATTANOOGA, TN, 37402","Match","Exact",'
        '"123 MAIN ST","-85.30,35.05","123","R"\n'
        '2,"999 NOWHERE RD, X, TN","No_Match"\n'
    )

    def test_parses_only_matches_and_swaps_lng_lat(self):
        out = parse_census_response(self.SAMPLE)
        self.assertEqual(set(out), {1})           # No_Match row excluded
        lat, lng = out[1]
        self.assertAlmostEqual(lat, 35.05, places=6)   # column is lng,lat -> swapped
        self.assertAlmostEqual(lng, -85.30, places=6)

    def test_empty_response(self):
        self.assertEqual(parse_census_response(""), {})

    def test_build_census_csv_normalizes_and_quotes(self):
        rows = [{"id": 7, "address": "100 MARKET ST C001", "city": "Chattanooga",
                 "state": "TN", "postal_code": "37402"}]
        line = build_census_csv(rows).strip()
        self.assertEqual(line, '7,"100 MARKET ST","Chattanooga","TN","37402"')


if __name__ == "__main__":
    unittest.main()
