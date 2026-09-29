# -*- coding: utf-8 -*-
"""S6 合并：镜头边界 × 关键帧 × 台词 × 多模态分析 -> 拉片表数据。

输出 shots_final.json，每行一个镜头，字段已按拉片表列序排列。
「人工分析」列固定留空，交给人工填写。
"""
from __future__ import annotations

from pathlib import Path

from .utils import load_json, log, save_json, Timer

# 最终拉片表的列定义：(输出列名, 数据来源键名, 宽度提示)
COLUMNS: list[tuple[str, str]] = [
    ("镜号", "shot_id"),
    ("起始", "start_short"),
    ("结束", "end_short"),
    ("时长(s)", "duration"),
    ("景别", "shot_size"),
    ("运镜", "camera_move"),
    ("画面内容", "visual_content"),
    ("关键帧", "keyframes"),
    ("台词", "dialogue"),
    ("情绪", "emotion"),
    ("表达核心", "core_message"),
    ("用户体验路径", "ux_path"),
    ("视听分工", "av_division"),
    ("人工分析", "manual_notes"),
]


def _join_dialogue(subs: list[dict], start: float, end: float) -> str:
    """取与镜头时间区间有重叠的所有字幕行，按时间拼接。"""
    hits = []
    for s in subs or []:
        if s["end"] <= start or s["start"] >= end:
            continue
        hits.append((s["start"], s["text"]))
    if not hits:
        # 兜底：用镜头中点所在行
        mid = (start + end) / 2
        for s in subs or []:
            if s["start"] <= mid <= s["end"]:
                return s["text"]
        return ""
    hits.sort(key=lambda x: x[0])
    return " ".join(t for _, t in hits)


def build(work_dir: Path, scenes: dict, kf: list[dict], subs: list[dict],
          vision: dict, meta: dict) -> dict:
    """组装最终拉片数据。"""
    with Timer("S6 合并拉片数据"):
        kf_map = {m["shot_id"]: m for m in kf}
        rows: list[dict] = []

        for shot in scenes["shots"]:
            sid = shot["shot_id"]
            sid_key = str(sid)
            m = kf_map.get(sid, {})
            v = vision.get(sid_key, {}) or {}
            if v.get("error"):
                v = {}

            frames = m.get("frames", [])
            rows.append({
                "shot_id": f"{sid:04d}",
                "shot_index": sid,
                "start": shot["start"],
                "end": shot["end"],
                "start_tc": shot["start_tc"],
                "end_tc": shot["end_tc"],
                "start_short": shot["start_short"],
                "end_short": shot["end_short"],
                "duration": shot["duration"],
                "keyframes": " | ".join(f["path"] for f in frames),
                "frame_count": len(frames),
                "dialogue": _join_dialogue(subs, shot["start"], shot["end"]),
                "shot_size": v.get("shot_size", ""),
                "camera_move": v.get("camera_move", ""),
                "visual_content": v.get("visual_content", ""),
                "emotion": v.get("emotion", ""),
                "core_message": v.get("core_message", ""),
                "ux_path": v.get("ux_path", ""),
                "av_division": v.get("av_division", ""),
                "manual_notes": "",   # ← 固定留空，交给人工
            })

        analyzed = sum(1 for r in rows if r["shot_size"] or r["visual_content"])
        with_dialogue = sum(1 for r in rows if r["dialogue"])

        data = {
            "meta": meta,
            "stats": {
                "shot_count": len(rows),
                "analyzed": analyzed,
                "with_dialogue": with_dialogue,
                "total_duration": round(
                    sum(r["duration"] for r in rows), 3
                ),
                "avg_shot_duration": round(
                    sum(r["duration"] for r in rows) / len(rows), 3
                ) if rows else 0,
            },
            "columns": [c for c, _ in COLUMNS],
            "rows": rows,
        }
        save_json(data, work_dir / "shots_final.json")
        log(
            f"合并完成：{len(rows)} 镜 · 已分析 {analyzed} 镜 · 含台词 {with_dialogue} 镜 · "
            f"平均镜头 {data['stats']['avg_shot_duration']}s",
            "ok",
        )
    return data
