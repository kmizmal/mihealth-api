# -*- coding: utf-8 -*-
"""分页重叠 / 多数据源去重：保留 canonical 数值最大的那条。"""
from mihealth_api.client import SCALAR_FIELDS, canonical_number, dedup_items


def test_canonical_number_first_matching_field():
    assert canonical_number("steps", {"steps": 42}) == 42
    assert canonical_number("heart_rate", {"heart_rate": 71}) == 71
    assert canonical_number("heart_rate", {"bpm": 80, "heart_rate": 70}) == 80


def test_canonical_number_ignores_bool_and_none():
    assert canonical_number("steps", {"steps": True}) is None
    assert canonical_number("steps", {}) is None
    assert canonical_number("unknown_key", {"steps": 10}) is None


def test_scalar_fields_cover_documented_keys():
    for k in ("steps", "calories", "heart_rate", "spo2", "stress"):
        assert k in SCALAR_FIELDS


def test_dedup_collapses_same_timestamp_keeping_larger():
    items = [
        {"_t": 100, "steps": 10, "sid": "watch"},
        {"_t": 100, "steps": 25, "sid": "phone"},
        {"_t": 200, "steps": 3, "sid": "watch"},
    ]
    out = dedup_items(items, "steps")
    assert [x["_t"] for x in out] == [100, 200]
    assert out[0]["steps"] == 25


def test_dedup_merges_missing_fields_from_loser():
    items = [
        {"_t": 100, "steps": 10},
        {"_t": 100, "steps": 5, "calories": 7},
    ]
    out = dedup_items(items, "steps")
    assert len(out) == 1 and out[0]["calories"] == 7 and out[0]["steps"] == 10


def test_dedup_drops_items_without_time_and_sorts():
    items = [{"_t": 300}, {"bogus": 1}, {"_t": 100}, {"_t": 200}]
    assert [x["_t"] for x in dedup_items(items)] == [100, 200, 300]


def test_dedup_no_key_keeps_first_seen():
    items = [{"_t": 5, "v": "a"}, {"_t": 5, "v": "b"}]
    assert dedup_items(items)[0]["v"] == "a"
