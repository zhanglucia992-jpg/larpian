# -*- coding: utf-8 -*-
"""S3 抽关键帧：ffmpeg 对每个镜头抽 1-2 张代表帧。

抽帧位置默认 12% 和 62% —— 避开开头的转场残留和结尾的黑场/叠化。
同一镜头内如果两张帧高度相似，自动去重只留一张（省 token）。
"""
from __future__ import annotations

from pathlib import Path

from .utils import ensure_dir, find_bin, log, run_cmd, save_json, sec_to_short, Timer


def _grab(ffmpeg: str, video: Path, t: float, out: Path, width: int, q: int) -> bool:
    """抽一帧。返回是否成功（抽到的是真画面而非纯黑空帧也粗略判断）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, t):.3f}",
        "-i", str(video),
        "-frames:v", "1",
        "-vf", f"scale={width}:-2",
        "-q:v", str(q),
        "-y", str(out),
    ]
    try:
        run_cmd(cmd)
    except RuntimeError:
        return False
    return out.exists() and out.stat().st_size > 1024


def _too_similar(a: Path, b: Path, threshold: float = 0.985) -> bool:
    """粗判两张灰度缩略图是否几乎一样。没有 numpy/PIL 就返回 False。"""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return False
    try:
        with Image.open(a) as ia, Image.open(b) as ib:
            sa = ia.convert("L").resize((64, 36))
            sb = ib.convert("L").resize((64, 36))
            va = np.asarray(sa, dtype="float32")
            vb = np.asarray(sb, dtype="float32")
        if va.std() < 1e-3 and vb.std() < 1e-3:
            return True
        # 归一化互相关
        va = (va - va.mean()) / (va.std() + 1e-6)
        vb = (vb - vb.mean()) / (vb.std() + 1e-6)
        corr = float((va * vb).mean())
        return corr >= threshold
    except Exception:  # noqa: BLE001
        return False


def extract(video: Path, work_dir: Path, cfg: dict, scenes: dict) -> list[dict]:
    """抽帧，返回每个镜头的帧列表并写入 keyframes.json。"""
    kf_cfg = cfg["keyframe"]
    n = max(1, min(4, int(kf_cfg.get("images_per_shot", 2))))
    positions = list(kf_cfg.get("positions") or [0.15, 0.6])[:n]
    while len(positions) < n:
        positions.append(0.5 + 0.1 * len(positions))
    width = int(kf_cfg.get("width", 720))
    q = int(kf_cfg.get("quality", 3))

    ffmpeg = find_bin(cfg["paths"].get("ffmpeg", "ffmpeg"))
    frames_dir = ensure_dir(work_dir / "keyframes")
    shots = scenes["shots"]
    total_dur = 0.0
    if shots:
        meta_dur = float(scenes.get("duration") or 0.0)
        total_dur = max(meta_dur, float(shots[-1].get("end") or 0.0))
    manifest: list[dict] = []

    log(f"抽帧策略：每镜 {n} 张 · 位置 {[f'{p:.0%}' for p in positions]} · 宽度 {width}px")

    with Timer("S3 抽取关键帧"):
        for idx, shot in enumerate(shots, start=1):
            s, e = float(shot["start"]), float(shot["end"])
            span = max(0.0, e - s)
            picked: list[Path] = []

            for pi, pos in enumerate(positions):
                t = s + span * float(pos)
                t = max(0.0, min(t, max(0.0, (total_dur or e) - 0.08)))
                fname = f"shot_{shot['shot_id']:04d}_{chr(97 + pi)}.jpg"
                out = frames_dir / fname
                if not _grab(ffmpeg, video, t, out, width, q):
                    continue
                if picked and _too_similar(picked[-1], out):
                    out.unlink(missing_ok=True)
                    continue
                picked.append(out)

            if not picked:
                # 兜底：退到镜头正中间再试一次
                mid = s + span / 2
                out = frames_dir / f"shot_{shot['shot_id']:04d}_m.jpg"
                if _grab(ffmpeg, video, mid, out, width, q):
                    picked.append(out)

            frames = [
                {"index": i, "path": str(p), "time": round(s + span * float(positions[min(i, len(positions) - 1)]), 3),
                 "time_short": sec_to_short(s + span * float(positions[min(i, len(positions) - 1)]))}
                for i, p in enumerate(picked)
            ]
            manifest.append({
                "shot_id": shot["shot_id"],
                "start": s, "end": e, "duration": shot["duration"],
                "frame_count": len(frames),
                "frames": frames,
            })

            if idx % 20 == 0 or idx == len(shots):
                log(f"  抽帧进度 {idx}/{len(shots)}", "dim")

    got = sum(m["frame_count"] for m in manifest)
    empty = [m["shot_id"] for m in manifest if m["frame_count"] == 0]
    log(f"共抽出 {got} 张关键帧，覆盖 {len(manifest) - len(empty)}/{len(manifest)} 个镜头", "ok")
    if empty:
        log(f"以下镜头抽帧失败（多为纯黑帧或时长过短）：{empty[:12]}", "warn")

    save_json({"frames_dir": str(frames_dir), "shots": manifest}, work_dir / "keyframes.json")
    return manifest
