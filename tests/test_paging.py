# -*- coding: utf-8 -*-
"""翻页 / 窗口过滤 / 窗口已覆盖提前停 —— 全部用假 call，不联网。"""
import json

import pytest

from mihealth_api.client import ApiError, AuthError, MiHealthClient


def make_client(**kw):
    kw.setdefault("max_retries", 0)
    kw.setdefault("min_interval", 0)
    return MiHealthClient("c2VjcmV0", "token", "cuser", **kw)


class FakePages:
    """按页返回预置数据，并记录每次请求的 next_key。"""

    def __init__(self, pages, list_field="data_list", has_more_flags=None):
        self.pages, self.list_field = pages, list_field
        self.seen = []
        self.calls = 0

    def __call__(self, subpath, params, **kw):
        self.calls += 1
        nk = json.loads(params["data"]).get("next_key")
        self.seen.append(nk)
        idx = 0 if nk is None else int(nk)
        batch = self.pages[idx] if idx < len(self.pages) else []
        more = idx + 1 < len(self.pages)
        res = {self.list_field: batch, "has_more": more,
               "next_key": str(idx + 1) if more else None}
        return {"code": 0, "result": res}


def test_paged_all_walks_every_page():
    c = make_client()
    fake = FakePages([[{"time": 300}], [{"time": 200}], [{"time": 100}]])
    c.call = fake
    items, more = c._paged_all(
        lambda nk: c.call("data/x", {"data": json.dumps({"next_key": nk})}),
        "data_list", 0, 10**15, 10, stop_below_start=True)
    assert not more
    assert [i["_t"] for i in items] == [100, 200, 300]     # 去重后升序
    assert fake.seen == [None, "1", "2"]


def test_paged_all_filters_outside_window():
    c = make_client()
    c.call = FakePages([[{"time": 100}, {"time": 200}]])
    items, _ = c._paged_all(
        lambda nk: c.call("s", {"data": json.dumps({"next_key": nk})}),
        "data_list", 150_000, 250_000, 5, stop_below_start=True)
    assert [i["_t"] for i in items] == [200]


def test_paged_all_stops_when_page_older_than_window():
    """整页都比 start 更老 → 窗口已覆盖，立即停，不要继续翻。"""
    c = make_client()
    fake = FakePages([[{"time": 500}], [{"time": 50}]])
    c.call = fake
    items, more = c._paged_all(
        lambda nk: c.call("s", {"data": json.dumps({"next_key": nk})}),
        "data_list", 400_000, 600_000, 50, stop_below_start=True)
    assert [i["_t"] for i in items] == [500]
    assert fake.calls == 2 and not more


def test_paged_all_full_history_ignores_stop_rule():
    c = make_client()
    fake = FakePages([[{"time": 500}], [{"time": 50}]])
    c.call = fake
    items, more = c._paged_all(
        lambda nk: c.call("s", {"data": json.dumps({"next_key": nk})}),
        "data_list", 400_000, 600_000, 50, stop_below_start=False, filter_window=False)
    assert [i["_t"] for i in items] == [50, 500]


def test_paged_all_reports_partial_when_max_pages_hit():
    c = make_client()
    c.call = FakePages([[{"time": 300}], [{"time": 200}], [{"time": 100}]])
    items, more = c._paged_all(
        lambda nk: c.call("s", {"data": json.dumps({"next_key": nk})}),
        "data_list", 0, 10**15, 2, stop_below_start=True)
    assert more is True and len(items) == 2


def test_paged_all_on_page_streams_instead_of_accumulating():
    c = make_client()
    c.call = FakePages([[{"time": 300}], [{"time": 200}]])
    batches = []
    items, _ = c._paged_all(
        lambda nk: c.call("s", {"data": json.dumps({"next_key": nk})}),
        "data_list", 0, 10**15, 10, stop_below_start=True,
        on_page=lambda batch, nk, page: batches.append([b["_t"] for b in batch]))
    assert items == [] and batches == [[300], [200]]


def test_get_fitness_data_by_time_params():
    c = make_client()
    seen = {}

    def fake(sub, params, **kw):
        seen.update(subpath=sub, payload=json.loads(params["data"]))
        return {"code": 0}

    c.call = fake
    c.get_fitness_data_by_time("steps", 1000, 2000, next_key="nk")
    assert seen["subpath"] == "data/get_fitness_data_by_time"
    assert seen["payload"] == {"key": "steps", "startTime": 1000, "endTime": 2000,
                               "reverse": True, "next_key": "nk"}


def test_sport_records_category_is_optional():
    c = make_client()
    seen = {}
    c.call = lambda sub, params, **kw: (seen.update(json.loads(params["data"])) or {"code": 0})
    c.get_sport_records_by_time(1, 2)
    assert "category" not in seen
    c.get_sport_records_by_time(1, 2, category="outdoor_running")
    assert seen["category"] == "outdoor_running"


def test_watermark_seek_returns_zero_and_is_treated_as_head():
    c = make_client()
    c.call = lambda sub, params, **kw: {"code": 0, "result": {
        "data_list": [], "watermark": 0, "has_more": True}}
    items, wm, more = c.sync_by_watermark("data/get_fitness_data_by_watermark", "data_list", 0)
    assert items == [] and wm == 0 and more is True


def test_watermark_never_persists_zero_over_cursor():
    c = make_client()
    pages = [{"data_list": [{"time": 1}], "watermark": 7, "has_more": True},
             {"data_list": [], "watermark": 0, "has_more": False}]
    c.call = lambda sub, params, **kw: {"code": 0, "result": pages.pop(0)}
    items, wm, more = c.sync_by_watermark("s", "data_list", 5)
    assert wm == 7 and not more          # 0 表示「到头」，不能覆盖 7
    assert [i["_t"] for i in items] == [1]


def test_auth_error_triggers_hook_and_retries_once():
    calls = {"n": 0, "hook": 0}

    c = make_client()

    class Resp:
        status_code = 401
        text = '{"code":2,"message":"auth err"}'

    def fake_send(method, url, q, retries):
        calls["n"] += 1
        return Resp()

    def hook(client, why):
        calls["hook"] += 1
        client.reload_credentials("bmV3", "new-token", "same-user")
        return True

    c._send = fake_send
    c.on_auth_error = hook
    with pytest.raises(AuthError):
        c.call("data/x", {"data": "{}"})
    assert calls == {"n": 2, "hook": 1}          # 刷新后重试一次，再失败就抛


def test_auth_error_without_hook_raises_immediately():
    c = make_client()
    c._send = lambda *a, **k: type("R", (), {"status_code": 401, "text": ""})()
    with pytest.raises(AuthError):
        c.call("data/x", {"data": "{}"})


def test_hook_returning_false_does_not_retry():
    c = make_client()
    n = {"v": 0}

    def fake_send(method, url, q, retries):
        n["v"] += 1
        return type("R", (), {"status_code": 403, "text": ""})()

    c._send = fake_send
    c.on_auth_error = lambda client, why: False
    with pytest.raises(AuthError):
        c.call("data/x", {"data": "{}"})
    assert n["v"] == 1


def test_http_error_raises_api_error():
    c = make_client()
    c._send = lambda *a, **k: type("R", (), {"status_code": 500, "text": "boom"})()
    with pytest.raises(ApiError, match="HTTP 500"):
        c.call("data/x", {"data": "{}"})


def test_reload_credentials_partial_update():
    c = make_client()
    c.reload_credentials(service_token="t2")
    assert (c.security, c.service_token, c.cuser) == ("c2VjcmV0", "t2", "cuser")


def test_region_and_host_resolution():
    assert make_client(region="abroad").base == "https://region.hlth.io.mi.com"
    assert make_client(host="https://example.test/").base == "https://example.test"
    assert make_client(region="xx").base == "https://hlth.io.mi.com"


def test_cookies_payload():
    c = make_client()
    assert c._cookies() == {"cUserId": "cuser", "serviceToken": "token", "locale": "zh_cn"}
