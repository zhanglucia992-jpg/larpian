#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 pipeline 产出的 feishu_payload.json 推送到飞书多维表格。

用法：
    python3 scripts/push_feishu.py <项目目录> [--base <app_token>] [--table <table_id>]

  <项目目录>  形如 output/demo，里面要有 feishu_payload.json
  --base      多维表格 app_token（不传则读 config.yaml 的 feishu.app_token）
  --table     数据表 id；表不存在时自动按 payload 的列创建（不传则读 config）

依赖：lark-cli 已登录（lark-cli auth status 检查）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from larpian.config import load_config  # noqa: E402

# 单选字段（值需要包成数组）
SELECT_FIELDS = {"景别", "运镜"}

TABLE_FIELDS = [
    {"name": "镜号", "type": "text"},
    {"name": "起始", "type": "text"},
    {"name": "结束", "type": "text"},
    {"name": "时长(s)", "type": "number"},
    {"name": "景别", "type": "select", "multiple": False, "options": [
        {"name": n} for n in
        ["大远景", "远景", "全景", "中景", "中近景", "近景", "特写", "大特写", "空镜"]]},
    {"name": "运镜", "type": "select", "multiple": False, "options": [
        {"name": n} for n in
        ["固定", "推", "拉", "摇", "移", "跟", "升降", "环绕",
         "手持晃动", "变焦", "甩镜", "组合运镜"]]},
    {"name": "画面内容", "type": "text"},
    {"name": "台词", "type": "text"},
    {"name": "情绪", "type": "text"},
    {"name": "表达核心", "type": "text"},
    {"name": "用户体验路径", "type": "text"},
    {"name": "视听分工", "type": "text"},
    {"name": "人工分析", "type": "text"},
]


def cli(args: list[str]) -> dict:
    p = subprocess.run(["lark-cli", *args], capture_output=True, text=True)
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        return {"ok": False, "raw": p.stdout[:300], "stderr": p.stderr[:300]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_dir")
    ap.add_argument("--base", dest="app_token", help="复用已有多维表格")
    ap.add_argument("--table", dest="table_id")
    ap.add_argument("--new-table", action="store_true", help="强制新建一张表（不复用 config 里的）")
    ap.add_argument("--config", default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    app_token = None if args.new_table else (args.app_token or cfg["feishu"].get("app_token"))
    table_id = None if args.new_table else (args.table_id or cfg["feishu"].get("table_id"))

    proj = Path(args.project_dir).expanduser().resolve()
    payload_path = proj / "feishu_payload.json"
    if not payload_path.exists():
        print(f"✗ 找不到 {payload_path}，请先跑 pipeline")
        return 1
    payload = json.loads(payload_path.read_text(encoding="utf-8"))

    def run(cmd_args: list[str]) -> dict:
        return cli(["base", *cmd_args, "--as", "user"])

    # 1) 校验 / 自动创建 Base 和表
    if not app_token:
        base_r = run(["+base-create", "--name", f"拉片表 · {payload.get('title') or '视频'}",
                      "--table-name", "拉片表", "--time-zone", "Asia/Shanghai",
                      "--fields", json.dumps(TABLE_FIELDS, ensure_ascii=False)])
        if not base_r.get("ok"):
            print("✗ 创建 Base 失败:", (base_r.get("error") or {}).get("message"))
            return 1
        app_token = base_r["data"]["base"]["base_token"]
        print(f"✓ 已创建多维表格：{base_r['data']['base']['url']}")

    if not table_id:
        tables = run(["+table-list", "--base-token", app_token]).get("data", {}).get("tables", [])
        hit = next((t for t in tables if t["name"] == "拉片表"), None)
        if hit:
            table_id = hit["id"]
        else:
            r = run(["+table-create", "--base-token", app_token, "--name", "拉片表",
                     "--fields", json.dumps(TABLE_FIELDS, ensure_ascii=False)])
            if not r.get("ok"):
                print("✗ 建表失败:", (r.get("error") or {}).get("hint") or (r.get("error") or {}).get("message"))
                return 1
            table_id = (r.get("data", {}).get("table") or {}).get("id")
            print(f"✓ 已创建数据表：{table_id}")

    # 2) 逐条 upsert（batch-create 在部分 Base 上校验异常，upsert 最稳）
    records = payload.get("records") or []
    ok_n, fail = 0, []
    for r in records:
        fields = dict(r.get("fields") or {})
        for k in SELECT_FIELDS:
            if fields.get(k):
                fields[k] = [fields[k]]
        fields = {k: v for k, v in fields.items() if v not in ("", None, [])}
        body = json.dumps(fields, ensure_ascii=False)
        d = run(["+record-upsert", "--base-token", app_token, "--table-id", table_id, "--json", body])
        if d.get("ok"):
            ok_n += 1
        else:
            err = (d.get("error") or {}).get("message", "") or str(d)[:120]
            fail.append((fields.get("镜号", "?"), err))

    print(f"✓ 写入完成：{ok_n}/{len(records)} 条")
    for shot, err in fail:
        print(f"  ✗ 镜 {shot}: {err}")

    base_url = f"https://my.feishu.cn/base/{app_token}?table={table_id}"
    print(f"\n表格地址：{base_url}")
    print("「人工分析」列已留空，去表里补人工判断。")
    return 0 if ok_n == len(records) else 1


if __name__ == "__main__":
    sys.exit(main())
