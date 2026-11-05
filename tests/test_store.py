# -*- coding: utf-8 -*-
"""SQLite 存储层：幂等 upsert、物化去重表、游标、聚合、CSV。"""
import json

import pytest

from mihealth_api.store import Store


def _item(ts, steps=None, sid="watch", **extra):
    v = {} if steps is None else {"steps": steps}
    v.update(extra)
    return {"time": ts, "sid": sid, "value": json.dumps(v),
            "zone_offset": 480, "zone_name": "Asia/Shanghai", "update_time": ts}


def test_upsert_is_idempotent(store):
    rows = [_item(100, 10), _item(200, 20)]
    assert store.upsert("fitness", "steps", rows) == 2
    assert store.upsert("fitness", "steps", rows) == 2      # 重跑不新增
    assert store.count("fitness", "steps") == 2


def test_upsert_skips_items_without_time(store):
    assert store.upsert("fitness", "steps", [{"sid": "x"}]) == 0
    assert store.count() == 0


def test_dedup_keeps_largest_and_raw_rows_kept(store):
    store.upsert("fitness", "steps", [_item(100, 10, "watch"), _item(100, 25, "phone")])
    assert store.count("fitness", "steps", dedup=False) == 2
    ded = store.count("fitness", "steps", dedup=True)
    assert ded == 1
    got = store.rows("fitness", "steps", dedup=True)
    assert got[0]["steps"] == 25


def test_dedup_nulls_sort_last(store):
    """非数值行（num IS NULL）不能赢过有数值的行。"""
    store.upsert("fitness", "steps", [
        {"time": 500, "sid": "a", "value": "{}"},
        {"time": 500, "sid": "b", "value": json.dumps({"steps": 7})},
    ])
    ded = store.rows("fitness", "steps", dedup=True)
    assert len(ded) == 1 and ded[0]["steps"] == 7


def test_dedup_without_scalar_field_falls_back_to_rowid(store):
    """`goal` 不在 SCALAR_FIELDS 里，去重只能按 rowid 决胜（记录现状）。"""
    store.upsert("fitness", "goal", [
        {"time": 500, "sid": "a", "value": json.dumps({"goal_items": []})},
        {"time": 500, "sid": "b", "value": json.dumps({"goal_items": [1]})},
    ])
    ded = store.rows("fitness", "goal", dedup=True)
    assert len(ded) == 1 and ded[0]["goal_items"] == []


def test_upsert_keeps_epoch_zero_timestamp(store):
    """time=0 是合法时间戳，不能被 `or` 吞掉。"""
    assert store.upsert("fitness", "steps", [_item(0, 5)]) == 1
    assert store.rows("fitness", "steps")[0]["_t"] == 0


def test_rows_window_and_order(store):
    store.upsert("fitness", "steps", [_item(t, t) for t in (100, 200, 300, 400)])
    got = store.rows("fitness", "steps", 150_000, 350_000)     # 毫秒入参
    assert [r["_t"] for r in got] == [200, 300]
    desc = store.rows("fitness", "steps", order="desc", limit=2)
    assert [r["_t"] for r in desc] == [400, 300]


def test_rows_fill_meta_fields(store):
    store.upsert("sport", "outdoor_running", [_item(42, sid="phone", distance=5.0)])
    r = store.rows("sport", "outdoor_running")[0]
    assert r["_t"] == 42 and r["_sid"] == "phone" and r["_key"] == "outdoor_running"
    assert r["distance"] == 5.0


def test_rows_filter_by_sid(store):
    store.upsert("fitness", "steps", [_item(1, 1, "watch"), _item(2, 2, "phone")])
    assert len(store.rows("fitness", "steps", sid="phone", dedup=False)) == 1


def test_bad_json_value_does_not_crash(store):
    store.upsert("fitness", "steps", [{"time": 1, "sid": "", "value": "not-json"}])
    assert store.rows("fitness", "steps")[0]["_t"] == 1


def test_upsert_batches(store):
    rows = [_item(t, t) for t in range(1, 26)]
    store.upsert("fitness", "steps", rows, batch=10)
    assert store.count("fitness", "steps") == 25


def test_state_roundtrip_and_partial_update(store):
    store.set_state("wm_fitness", last_wm=123)
    store.set_state("ts_steps", last_ts=999)
    store.set_state("wm_fitness", last_ts=111)             # 只更新其中一个字段
    assert store.get_state("wm_fitness", "last_wm") == 123
    assert store.get_state("wm_fitness", "last_ts") == 111
    assert store.get_state("nope") is None
    st = store.states()
    assert set(st) == {"wm_fitness", "ts_steps"} and st["ts_steps"]["last_ts"] == 999


def test_rebuild_dedup(store):
    store.upsert("fitness", "steps", [_item(10, 1, "a"), _item(10, 5, "b")])
    assert store.rebuild_dedup() == 1
    assert store.rows("fitness", "steps", dedup=True)[0]["steps"] == 5


def test_stats_and_keys_of(store):
    store.upsert("fitness", "steps", [_item(10, 1), _item(20, 2)])
    store.upsert("fitness", "calories", [_item(10, 3)])
    stats = store.stats()
    assert {(s["family"], s["key"]) for s in stats} == {("fitness", "steps"),
                                                         ("fitness", "calories")}
    keys = {k["key"]: k["count"] for k in store.keys_of("fitness")}
    assert keys == {"steps": 2, "calories": 1}


def test_sources(store):
    store.upsert("fitness", "steps", [_item(1, 1, "watch"), _item(2, 2, "phone"),
                                      _item(3, 3, "phone")])
    src = {s["sid"]: s["count"] for s in store.sources("fitness")}
    assert src == {"watch": 1, "phone": 2}


def test_aggregate_sum_and_buckets(store):
    store.upsert("fitness", "steps", [_item(t, t) for t in (0, 10, 20, 30)])
    pts = store.aggregate("fitness", "steps", "steps", bucket_s=20, agg="sum")
    assert pts == [{"t": 0, "v": 10.0}, {"t": 20, "v": 50.0}]


def test_aggregate_ops(store):
    store.upsert("fitness", "steps", [_item(0, 10), _item(1, 20)])
    assert store.aggregate("fitness", "steps", "steps", bucket_s=100, agg="max")[0]["v"] == 20
    assert store.aggregate("fitness", "steps", "steps", bucket_s=100, agg="min")[0]["v"] == 10
    assert store.aggregate("fitness", "steps", "steps", bucket_s=100, agg="avg")[0]["v"] == 15
    assert store.aggregate("fitness", "steps", "steps", bucket_s=100, agg="count")[0]["v"] == 2


def test_aggregate_rejects_unknown_agg(store):
    with pytest.raises(ValueError):
        store.aggregate("fitness", "steps", "steps", agg="median")


def test_aggregate_filters_window(store):
    store.upsert("fitness", "steps", [_item(100, 5), _item(200, 7)])
    pts = store.aggregate("fitness", "steps", "steps", bucket_s=100,
                          start_ms=150_000, end_ms=250_000)
    assert pts == [{"t": 200, "v": 7.0}]


def test_to_csv(store, tmp_path):
    store.upsert("fitness", "steps", [_item(10, 5, zone_name="Asia/Shanghai")])
    p = tmp_path / "out.csv"
    n = store.to_csv(str(p), "fitness", "steps")
    assert n == 1
    text = p.read_text(encoding="utf-8-sig")
    head, row = text.strip().splitlines()
    assert head.split(",")[:4] == ["family", "key", "time", "sid"]
    assert "fitness,steps,10" in row


def test_wal_mode_enabled(tmp_path):
    s = Store(str(tmp_path / "w.db"))
    try:
        assert s.db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    finally:
        s.close()


def test_v_dedup_view_exists(store):
    row = store.db.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='v_dedup'").fetchone()
    assert row and "from dedup" in row[0].lower()
