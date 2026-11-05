# -*- coding: utf-8 -*-
"""全量导出：翻页、逐数据集落盘、单数据集失败不中断整体。"""
import json

import pytest

from mihealth_api import export as EX


class FakeClient:
    def __init__(self):
        self.calls = []
        self.pages = {}          # subpath -> [ {list_field: [...], next_key, has_more} ]
        self.fitness = {}
        self.sport = []
        self.fail = set()        # 让它抛异常的方法名/子路径

    def call(self, sub, params):
        self.calls.append((sub, json.loads(params["data"])))
        if sub in self.fail:
            raise RuntimeError(f"boom {sub}")
        queue = self.pages.get(sub)
        if not queue:
            return {"code": 0, "result": {"data_list": [], "has_more": False}}
        return {"code": 0, "result": queue.pop(0)}

    def get_fitness_data_all(self, key, start, end, max_pages=3000, full_history=False):
        if "fitness" in self.fail:
            raise RuntimeError("boom fitness")
        return {"data_list": self.fitness.get(key, []), "has_more": False}

    def get_sport_records_all(self, start, end, max_pages=500, full_history=False):
        if "sport_records" in self.fail:
            raise RuntimeError("boom sport")
        return {"sport_records": self.sport, "has_more": False}


@pytest.fixture
def exp(tmp_path, monkeypatch):
    monkeypatch.setattr(EX.time, "sleep", lambda *_: None)     # 别真等 0.3s
    fake = FakeClient()
    e = EX.Exporter(fake, str(tmp_path / "export"))
    e.fake = fake
    return e


def test_exporter_creates_out_dir(exp):
    import os
    assert os.path.isdir(exp.out)


def test_save_counts_and_manifest(exp):
    exp.save("x", {"data_list": [1, 2, 3]})
    assert exp.manifest["datasets"]["x"]["count"] == 3
    assert exp.manifest["datasets"]["x"]["bytes"] > 0


def test_save_accepts_explicit_count(exp):
    exp.save("y", {"weird": []}, count=7)
    assert exp.manifest["datasets"]["y"]["count"] == 7


def test_save_counts_sport_records_field(exp):
    exp.save("s", {"sport_records": [1, 2]})
    assert exp.manifest["datasets"]["s"]["count"] == 2


def test_walk_follows_next_key(exp):
    exp.fake.pages["sub"] = [
        {"data_list": [{"a": 1}], "next_key": "k1", "has_more": True},
        {"data_list": [{"a": 2}], "next_key": "k2", "has_more": True},
        {"data_list": [{"a": 3}], "next_key": None, "has_more": False},
    ]
    items, pages = exp._walk("sub", lambda nk: {"next_key": nk})
    assert [i["a"] for i in items] == [1, 2, 3] and pages == 3
    assert [c[1]["next_key"] for c in exp.fake.calls] == [None, "k1", "k2"]


def test_walk_stops_when_has_more_false(exp):
    exp.fake.pages["sub"] = [
        {"data_list": [1], "next_key": "k1", "has_more": False},
        {"data_list": [2], "next_key": "k2", "has_more": True},
    ]
    items, pages = exp._walk("sub", lambda nk: {"next_key": nk})
    assert items == [1] and pages == 1


def test_walk_honours_max_pages(exp):
    exp.fake.pages["sub"] = [{"data_list": [i], "next_key": f"k{i}", "has_more": True}
                            for i in range(10)]
    items, pages = exp._walk("sub", lambda nk: {"next_key": nk}, max_pages=3)
    assert pages == 3 and len(items) == 3


def test_walk_handles_missing_result(exp):
    exp.fake.call = lambda sub, params: None
    items, pages = exp._walk("sub", lambda nk: {})
    assert items == [] and pages == 1


def test_run_writes_all_datasets(exp, tmp_path):
    exp.fake.fitness = {"steps": [{"time": 1}]}
    exp.fake.sport = [{"time": 2}]
    manifest = exp.run(now=10**12)
    names = set(manifest["datasets"])
    assert "fitness_steps" in names
    assert "sport_records" in names
    assert "medical" in names and "project_data" in names
    assert "sport_summary" in names and "aggregated_daily" in names
    assert "stat_" + "energy" in names
    assert "meta_sport_categories" in names
    assert (tmp_path / "export" / "_manifest.json").exists()


def test_run_skips_failing_dataset_and_continues(exp, capsys):
    exp.fake.fail.update({"sport_records", "data/get_medical_data_by_time"})
    manifest = exp.run(now=10**12)
    assert "sport_records" not in manifest["datasets"]
    assert "fitness_steps" in manifest["datasets"]
    out = capsys.readouterr().out
    assert "[skip] sport" in out and "[skip] medical" in out


def test_run_survives_all_fitness_failures(exp):
    exp.fake.fail.add("fitness")
    manifest = exp.run(now=10**12)
    assert not [k for k in manifest["datasets"] if k.startswith("fitness_")]
    assert "meta_max_watermark" in manifest["datasets"]


def test_run_writes_manifest_file(exp, tmp_path):
    manifest = exp.run(now=10**12)
    on_disk = json.loads((tmp_path / "export" / "_manifest.json").read_text(encoding="utf-8"))
    assert on_disk["exported_at"] == manifest["exported_at"]
    assert set(on_disk["datasets"]) == set(manifest["datasets"])


def test_run_requests_full_history_for_fitness(exp):
    seen = {}

    def spy(key, start, end, max_pages=3000, full_history=False):
        seen.update(key=key, start=start, max_pages=max_pages, full=full_history)
        return {"data_list": []}

    exp.fake.get_fitness_data_all = spy
    exp.run(now=99)
    assert seen["start"] == 1 and seen["full"] is True and seen["max_pages"] == 3000


def test_main_rejects_incomplete_credentials(tmp_path, capsys):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ssecurity": "S"}), encoding="utf-8")
    with pytest.raises(SystemExit) as e:
        EX.main(["--config", str(cfg), "--out", str(tmp_path / "o")])
    assert "凭据不全" in str(e.value)


def test_main_runs_with_credentials(tmp_path, monkeypatch):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"ssecurity": "S", "service_token": "T",
                               "cuser_id": "C"}), encoding="utf-8")
    monkeypatch.setattr(EX, "MiHealthClient", lambda *a, **k: FakeClient())
    out = tmp_path / "o"
    assert EX.main(["--config", str(cfg), "--out", str(out)]) == 0
    assert (out / "_manifest.json").exists()
