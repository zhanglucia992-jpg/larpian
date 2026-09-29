# -*- coding: utf-8 -*-
"""拉片工具 · 主编排入口。

用法：
    python3 pipeline.py <视频URL或本地路径> [选项]

示例：
    # 全流程（默认停在「生成分析任务单」，等你回填分析结果）
    python3 pipeline.py https://www.bilibili.com/video/BVxxxx

    # 本地文件，每镜抽 1 张帧
    python3 pipeline.py ./samples/demo.mp4 --images-per-shot 1

    # 只要切镜+抽帧+字幕，不做画面分析
    python3 pipeline.py ./demo.mp4 --skip-vision

    # 已有分析结果（vision_results.json）后合并导出
    python3 pipeline.py ./output/xxx --resume

    # 用多模态 API 全自动分析
    LARPIAN_VISION_PROVIDER=api python3 pipeline.py ./demo.mp4
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from larpian import s1_ingest, s2_scene, s3_keyframes, s4_subtitle, s5_vision, s6_assemble, s7_export
from larpian.config import load_config, resolve_work_dir
from larpian.utils import die, load_json, log, save_json, slugify, step


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pipeline.py",
        description="视频拉片工具：切镜头 -> 抽帧 -> 多模态分析 -> 台词对齐 -> 拉片表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("source", help="视频 URL 或本地文件路径；也可以是已有项目目录（配合 --resume）")
    p.add_argument("--name", help="项目名（默认取视频标题）")
    p.add_argument("--config", help="配置文件路径（默认 ./config.yaml）")
    p.add_argument("--out", help="输出根目录（默认 config 里的 work_dir）")

    g = p.add_argument_group("切镜")
    g.add_argument("--threshold", type=float, help="切镜灵敏度阈值，越小越敏感（默认 27）")
    g.add_argument("--detector", choices=["content", "adaptive", "hash"], help="检测器")
    g.add_argument("--min-shot", type=float, help="最短镜头秒数，短于此并入前镜（默认 0.6）")

    g2 = p.add_argument_group("抽帧")
    g2.add_argument("--images-per-shot", type=int, choices=[1, 2, 3, 4], help="每镜抽几张（默认 2）")
    g2.add_argument("--frame-width", type=int, help="关键帧输出宽度像素（默认 720）")

    g3 = p.add_argument_group("流程控制")
    g3.add_argument("--skip-download", action="store_true", help="跳过下载，直接找已有的视频文件")
    g3.add_argument("--skip-subtitle", action="store_true", help="跳过台词提取")
    g3.add_argument("--skip-vision", action="store_true", help="跳过画面分析（只出切镜+台词）")
    g3.add_argument("--no-export", action="store_true", help="只跑到分析，不导出文件")
    g3.add_argument("--resume", action="store_true", help="断点续跑：复用已有产物")
    g3.add_argument("--force", action="store_true", help="清空已有产物重跑")
    return p


def apply_overrides(cfg: dict, args) -> dict:
    if args.threshold is not None:
        cfg["scene"]["threshold"] = args.threshold
    if args.detector:
        cfg["scene"]["detector"] = args.detector
    if args.min_shot is not None:
        cfg["scene"]["min_shot_sec"] = args.min_shot
    if args.images_per_shot is not None:
        cfg["keyframe"]["images_per_shot"] = args.images_per_shot
    if args.frame_width is not None:
        cfg["keyframe"]["width"] = args.frame_width
    if args.skip_subtitle:
        cfg["subtitle"]["enabled"] = False
    if args.out:
        cfg["paths"]["work_dir"] = args.out
    return cfg


def find_video_in(work_dir: Path) -> Path | None:
    for ext in (".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv", ".m4v"):
        hits = sorted(work_dir.glob(f"*{ext}"), key=lambda p: p.stat().st_size, reverse=True)
        if hits:
            return hits[0]
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = apply_overrides(load_config(args.config), args)

    src = args.source
    work_root = resolve_work_dir(cfg)
    work_root.mkdir(parents=True, exist_ok=True)

    # ---------- 判断是「新项目」还是「已有项目续跑」
    src_path = Path(src).expanduser()
    is_existing_project = src_path.is_dir() and (src_path / "scenes.json").exists()
    if is_existing_project:
        work_dir = src_path.resolve()
        meta = load_json(work_dir / "meta.json", {}) or {}
        video = Path(meta.get("video_path") or "") if meta.get("video_path") else None
        if not video or not video.exists():
            video = find_video_in(work_dir)
        if not video:
            die(f"在 {work_dir} 里找不到视频文件")
        log(f"续跑已有项目：{work_dir.name}", "step")
        args.resume = True
    else:
        log(f"新项目：{src}", "step")
        if args.force:
            pass  # 由 ensure 逻辑覆盖

    print()
    step("开始拉片")
    print()

    # ---------- S1 入库
    if is_existing_project:
        pass
    else:
        video, meta = s1_ingest.ingest(src, work_root, cfg) if not args.skip_download else (None, {})
        if args.skip_download:
            video = find_video_in(work_root)
            if not video:
                die(f"--skip-download 指定了，但 {work_root} 里没有视频文件")
            meta = {"source": str(video), "source_type": "local", "title": video.stem,
                    "video_path": str(video), "video_id": video.stem, "duration": 0}
        name = slugify(args.name or meta.get("title") or Path(src).stem)
        work_dir = work_root / name
        work_dir.mkdir(parents=True, exist_ok=True)
        # 视频若在 work_root 根下（下载的），移进项目目录
        if video.parent != work_dir and str(video).startswith(str(work_root)):
            target = work_dir / video.name
            if not target.exists():
                video.rename(target)
            meta["video_path"] = str(target)
            video = target
        elif not str(video).startswith(str(work_root)):
            meta["video_path"] = str(video)
        # 字幕 / info.json 一起同步进来（本地源只复制，绝不挪动用户原始文件）
        s1_ingest.move_sidecar_files(
            video, work_dir, copy_only=(meta.get("source_type") == "local")
        )
        save_json(meta, work_dir / "meta.json")

        if args.force:
            import shutil
            killed = []
            for f in ("scenes.json", "keyframes.json", "subtitles.json",
                      "vision_tasks.json", "vision_results.json", "vision.json",
                      "shots_final.json", "feishu_payload.json"):
                p = work_dir / f
                if p.exists():
                    p.unlink()
                    killed.append(f)
            fd = work_dir / "keyframes"
            if fd.exists():
                shutil.rmtree(fd)
                killed.append("keyframes/")
            ed = work_dir / "export"
            if ed.exists():
                shutil.rmtree(ed)
                killed.append("export/")
            if killed:
                log(f"--force 已清空中间产物：{', '.join(killed)}", "warn")

    print()
    meta = load_json(work_dir / "meta.json", {}) or meta
    video = Path(meta.get("video_path") or find_video_in(work_dir))

    # ---------- S2 切镜
    if args.resume and (work_dir / "scenes.json").exists():
        scenes = load_json(work_dir / "scenes.json")
        log(f"S2 复用已有切镜结果（{scenes['shot_count']} 镜）", "ok")
    else:
        print()
        scenes = s2_scene.detect(video, work_dir, cfg, float(meta.get("duration") or 0))

    # ---------- S3 抽帧
    if args.resume and (work_dir / "keyframes.json").exists():
        kf = load_json(work_dir / "keyframes.json")["shots"]
        log(f"S3 复用已有关键帧（{sum(m['frame_count'] for m in kf)} 张）", "ok")
    else:
        print()
        kf = s3_keyframes.extract(video, work_dir, cfg, scenes)

    # ---------- S4 字幕
    if args.resume and (work_dir / "subtitles.json").exists():
        sub_data = load_json(work_dir / "subtitles.json") or {}
        subs = sub_data.get("subtitles", [])
        log(f"S4 复用已有字幕（{len(subs)} 行）", "ok")
    else:
        print()
        subs = s4_subtitle.fetch(video, work_dir, cfg)

    # ---------- S5 多模态分析
    vision: dict = {}
    if not args.skip_vision:
        print()
        vision = s5_vision.run(work_dir, cfg, kf, scenes, subs, meta)
        if not vision:
            print()
            log("画面分析待回填 —— 先看任务单，逐帧分析后写入 vision_results.json", "warn")
            log(f"任务单：{work_dir / 'vision_任务单.md'}", "info")
            log(f"关键帧目录：{work_dir / 'keyframes'}", "info")
            log(f"回填后重跑：python3 pipeline.py {work_dir} --resume", "info")
    vision = s5_vision.merge_results(work_dir) or vision
    if args.skip_vision:
        for k in [m["shot_id"] for m in kf]:
            vision.setdefault(str(k), {})

    # ---------- S6 合并
    print()
    data = s6_assemble.build(work_dir, scenes, kf, subs, vision, meta)

    # ---------- S7 导出
    made: dict = {}
    if not args.no_export:
        print()
        made = s7_export.run(work_dir, cfg, data, meta)

    # ---------- 收尾
    print()
    step("完成")
    print()
    log(f"项目目录：{work_dir}", "ok")
    st = data["stats"]
    log(f"{st['shot_count']} 镜 · 总时长 {st['total_duration']:.1f}s · 平均 {st['avg_shot_duration']}s／镜", "info")
    if st["analyzed"] < st["shot_count"]:
        log(f"画面分析完成度 {st['analyzed']}/{st['shot_count']}，其余留待回填", "warn")
    for k, v in made.items():
        if k != "feishu_payload":
            log(f"{k}: {v}", "info")
    log("「人工分析」列已留空，等你在表里补", "info")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        die("已中断")
