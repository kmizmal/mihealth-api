#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""小米运动健康 全量健康数据导出 —— 所有云端数据落盘 export/*.json（不落 SQLite）。

入口：`mihealth-export` / `python -m mihealth_api.export`
凭据：默认读当前目录 config.json，可用 `--config` 或 MIH_CONFIG 覆盖。
输出：默认 ./export，可用 `--out` 或 MIH_EXPORT_DIR 覆盖。
"""
import argparse, json, os, time

from . import paths
from .client import MiHealthClient, load_config

FITNESS_KEYS = ["steps", "calories", "sleep", "heart_rate", "stress", "spo2",
                "intensity", "valid_stand", "energy", "goal", "pai",
                "blood_pressure", "blood_sugar", "headset", "weight",
                "vo2_max", "menstruation"]


def J(x):
    return json.dumps(x, separators=(",", ":"))


class Exporter:
    def __init__(self, client, out_dir):
        self.client = client
        self.out = out_dir
        os.makedirs(self.out, exist_ok=True)
        self.manifest = {"exported_at": int(time.time() * 1000), "datasets": {}}

    def save(self, name, obj, count=None):
        p = os.path.join(self.out, name + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
        n = count if count is not None else len(
            obj.get("data_list") or obj.get("sport_records") or obj.get("items") or [])
        self.manifest["datasets"][name] = {"count": n, "bytes": os.path.getsize(p)}
        print(f"[saved] {name}: {n} items, "
              f"{self.manifest['datasets'][name]['bytes'] // 1024}KB", flush=True)

    def _walk(self, sub, mk_params, list_field="data_list", max_pages=500):
        """Follow next_key to the end; returns the merged list."""
        items, nk, page = [], None, 0
        while page < max_pages:
            r = self.client.call(sub, {"data": J(mk_params(nk))})
            res = (r or {}).get("result") or {}
            items += res.get(list_field) or []
            nk = res.get("next_key")
            page += 1
            if not res.get("has_more") or not nk:
                break
        return items, page

    def run(self, now=None):
        now = now or int(time.time() * 1000)

        # 1) fitness 全部键 × 全历史
        for key in FITNESS_KEYS:
            try:
                res = self.client.get_fitness_data_all(key, 1, now, max_pages=3000,
                                                       full_history=True)
                self.save("fitness_" + key, {"data_list": res["data_list"]})
            except Exception as e:
                print(f"[skip] fitness {key}: {e}", flush=True)
            time.sleep(0.3)

        # 2) 医疗
        try:
            items, _ = self._walk("data/get_medical_data_by_time",
                                  lambda nk: {"key": None, "startTime": 1, "endTime": now,
                                              "reverse": True, "next_key": nk})
            self.save("medical", {"data_list": items})
        except Exception as e:
            print("[skip] medical:", e, flush=True)

        # 3) 项目数据
        try:
            items, _ = self._walk("data/get_project_data_by_time",
                                  lambda nk: {"key": None, "startTime": 1, "endTime": now,
                                              "reverse": True, "next_key": nk})
            self.save("project_data", {"data_list": items})
        except Exception as e:
            print("[skip] project:", e, flush=True)

        # 4) 运动记录
        try:
            res = self.client.get_sport_records_all(1, now, max_pages=500, full_history=True)
            self.save("sport_records", {"sport_records": res["sport_records"]})
        except Exception as e:
            print("[skip] sport:", e, flush=True)

        # 5) 运动汇总
        try:
            items, _ = self._walk("statistics/scan_sport_summary",
                                  lambda nk: {"startTime": 1, "endTime": now,
                                              "next_key": nk}, max_pages=200)
            self.save("sport_summary", {"data_list": items})
        except Exception as e:
            print("[skip] sport_summary:", e, flush=True)

        # 6) 聚合日报
        try:
            items, _ = self._walk("data/get_aggregated_fitness_data_by_time",
                                  lambda nk: {"tag": "daily", "key": None, "startTime": 1,
                                              "endTime": now, "cursor": nk, "reverse": True},
                                  max_pages=200)
            self.save("aggregated_daily", {"data_list": items})
        except Exception as e:
            print("[skip] aggregated:", e, flush=True)

        # 7) 统计数据
        for key in ["energy", "steps", "sleep", "vitality", "pai"]:
            try:
                items, _ = self._walk("statistics/get_stat_data_by_time",
                                      lambda nk, k=key: {"tag": "daily", "key": k,
                                                         "next_key": nk, "startTime": 1,
                                                         "endTime": now, "limit": 50,
                                                         "reverse": True}, max_pages=200)
                self.save("stat_" + key, {"data_list": items})
            except Exception as e:
                print(f"[skip] stat {key}:", e, flush=True)

        # 8) 类别与水位等元数据
        try:
            self.save("meta_sport_categories",
                      self.client.call("data/get_sport_category_list", {"data": "{}"}))
            self.save("meta_max_watermark",
                      self.client.call("data/get_max_watermark", {"data": J({"type": 0})}))
        except Exception as e:
            print("[skip] meta:", e, flush=True)

        with open(os.path.join(self.out, "_manifest.json"), "w", encoding="utf-8") as f:
            json.dump(self.manifest, f, ensure_ascii=False, indent=2)
        print("DONE ->", self.out, flush=True)
        return self.manifest


def main(argv=None):
    ap = argparse.ArgumentParser(description="全量导出小米运动健康数据为 JSON")
    ap.add_argument("--config", default=paths.config_path())
    ap.add_argument("--out", default=paths.export_dir())
    a = ap.parse_args(argv)
    cfg = load_config(a.config)
    if not cfg["ssecurity"] or not cfg["service_token"] or not cfg["cuser_id"]:
        raise SystemExit(f"凭据不全：{a.config} 里缺 ssecurity / service_token / cuser_id")
    client = MiHealthClient(cfg["ssecurity"], cfg["service_token"], cfg["cuser_id"],
                            region=cfg.get("region") or "cn", host=cfg.get("host"),
                            proxies=cfg.get("proxies"))
    Exporter(client, a.out).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
