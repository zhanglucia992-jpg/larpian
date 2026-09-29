# -*- coding: utf-8 -*-
"""S2 切镜头：PySceneDetect 检测镜头边界，输出 shots.json。"""
from __future__ import annotations

from pathlib import Path

from .utils import log, save_json, sec_to_short, sec_to_tc, Timer


def _build_detector(cfg: dict):
    from scenedetect import AdaptiveDetector, ContentDetector, HashDetector

    sc = cfg["scene"]
    kind = str(sc.get("detector", "content")).lower()
    if kind == "adaptive":
        return AdaptiveDetector(
            adaptive_threshold=float(sc.get("adaptive_threshold", 3.0)),
            min_scene_len=1,
        )
    if kind == "hash":
        return HashDetector(threshold=float(sc.get("threshold", 27.0)), min_scene_len=1)
    return ContentDetector(threshold=float(sc.get("threshold", 27.0)), min_scene_len=1)


def _merge_short(shots: list[dict], min_sec: float, gap_sec: float) -> list[dict]:
    """把过短的镜头并入前一个。

    注意：PySceneDetect 返回的场景是首尾相接的，相邻镜头 gap 天然为 0，
    所以不能拿 gap 当合并依据（那样会把所有镜头并成一个）。
    gap_sec 只用于处理「两个场景之间确实存在一段极短空隙」的情况，
    这类空隙通常已被检测器算进场景区间里，因此这里以长度阈值为主。
    """
    if not shots:
        return shots
    out: list[dict] = [shots[0]]
    for s in shots[1:]:
        prev = out[-1]
        too_short = (s["end"] - s["start"]) < min_sec
        # 只有当确实存在非零且极小的空隙时才合并（gap == 0 属于正常相邻，不合并）
        tiny_gap = 0 < (s["start"] - prev["end"]) <= gap_sec
        if too_short or tiny_gap:
            prev["end"] = s["end"]
            if "end_frame" in s:
                prev["end_frame"] = s["end_frame"]
        else:
            out.append(s)

    # 首镜头过短：并给第二个，避免出现一个无意义的开场碎片
    if len(out) > 1 and (out[0]["end"] - out[0]["start"]) < min_sec:
        out[1]["start"] = out[0]["start"]
        if "start_frame" in out[0]:
            out[1]["start_frame"] = out[0]["start_frame"]
        out.pop(0)
    return out


def detect(video: Path, work_dir: Path, cfg: dict, total_duration: float = 0.0) -> dict:
    """检测镜头边界，返回 scene 数据并写入 scenes.json。"""
    with Timer("S2 切镜头（PySceneDetect）"):
        try:
            import scenedetect  # noqa: F401
            from scenedetect import SceneManager, open_video
        except ImportError:
            log(
                "未安装 PySceneDetect，退回到基于 ffmpeg 的固定间隔切分。\n"
                "  建议安装：pip install 'scenedetect[opencv]'",
                "warn",
            )
            return _fallback_fixed_split(video, work_dir, cfg, total_duration)

        video_obj = open_video(str(video))
        fps = float(video_obj.frame_rate or 30.0)
        sm = SceneManager()
        sm.add_detector(_build_detector(cfg))
        sm.detect_scenes(video_obj, show_progress=False)
        scene_list = sm.get_scene_list()

        shots = []
        for i, (start_tc, end_tc) in enumerate(scene_list, start=1):
            s = start_tc.get_seconds()
            e = end_tc.get_seconds()
            shots.append({
                "shot_id": i,
                "start": round(s, 3),
                "end": round(e, 3),
                "start_frame": start_tc.get_frames(),
                "end_frame": end_tc.get_frames(),
            })

        if not shots:
            log("未检测到切点（可能是单镜头视频），整段作为一个镜头", "warn")
            dur = total_duration or 0.0
            shots = [{"shot_id": 1, "start": 0.0, "end": round(dur, 3),
                      "start_frame": 0, "end_frame": int(dur * fps)}]

        before = len(shots)
        shots = _merge_short(
            shots,
            float(cfg["scene"].get("min_shot_sec", 0.6)),
            float(cfg["scene"].get("merge_gap_sec", 0.15)),
        )

        for i, s in enumerate(shots, start=1):
            s["shot_id"] = i
            s["duration"] = round(s["end"] - s["start"], 3)
            s["start_tc"] = sec_to_tc(s["start"])
            s["end_tc"] = sec_to_tc(s["end"])
            s["start_short"] = sec_to_short(s["start"])
            s["end_short"] = sec_to_short(s["end"])

        data = {
            "video": str(video),
            "fps": fps,
            "duration": round(max(float(total_duration or 0.0),
                                  float(shots[-1]["end"]) if shots else 0.0), 3),
            "detector": cfg["scene"].get("detector", "content"),
            "threshold": cfg["scene"].get("threshold"),
            "raw_scene_count": before,
            "shot_count": len(shots),
            "shots": shots,
        }
        save_json(data, work_dir / "scenes.json")

        avg = sum(s["duration"] for s in shots) / len(shots)
        log(
            f"检测到 {len(shots)} 个镜头"
            + (f"（已合并 {before - len(shots)} 个过短片段）" if before != len(shots) else "")
            + f" · 平均镜头时长 {avg:.2f}s",
            "ok",
        )
    return data


def _fallback_fixed_split(video: Path, work_dir: Path, cfg: dict, total_duration: float) -> dict:
    """没有 PySceneDetect 时的兜底：按固定秒数切分。"""
    import json
    from .utils import find_bin, ffprobe_duration

    ffprobe = find_bin(cfg["paths"].get("ffprobe", "ffprobe"))
    dur = total_duration or ffprobe_duration(video, ffprobe)
    seg = float(cfg["scene"].get("min_shot_sec", 0.6)) * 10 or 5.0
    shots = []
    t, i = 0.0, 1
    while t < dur - 0.05:
        e = min(t + seg, dur)
        shots.append({
            "shot_id": i, "start": round(t, 3), "end": round(e, 3),
            "duration": round(e - t, 3),
            "start_tc": sec_to_tc(t), "end_tc": sec_to_tc(e),
            "start_short": sec_to_short(t), "end_short": sec_to_short(e),
        })
        t, i = e, i + 1
    data = {
        "video": str(video), "fps": None, "detector": "fixed_fallback",
        "shot_count": len(shots), "shots": shots,
    }
    save_json(data, work_dir / "scenes.json")
    log(f"兜底切分：{len(shots)} 段（每段 {seg}s）", "warn")
    return data
