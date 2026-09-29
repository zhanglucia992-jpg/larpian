# -*- coding: utf-8 -*-
"""S1 视频入库：yt-dlp 下载 或 本地文件 -> 规范化 mp4（同时尽量抓字幕）。"""
from __future__ import annotations

import re
from pathlib import Path

from .utils import (
    die, ensure_dir, ffprobe_duration, ffprobe_video_info,
    find_bin, load_json, log, run_cmd, save_json, slugify, Timer,
)

URL_RE = re.compile(r"^https?://", re.I)


def is_url(src: str) -> bool:
    return bool(URL_RE.match(src or ""))


def _extract_video_id(url: str) -> str:
    """从 URL 里猜一个短标识，用于命名目录。"""
    patterns = [
        r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})",
        r"bilibili\.com/video/(BV[\w]+|av\d+)",
        r"douyin\.com/video/(\d+)",
        r"xiaohongshu\.com/\S*?/([0-9a-f]{16,})",
        r"/(?:video|reel|status|note)/([\w-]{6,})",
    ]
    for p in patterns:
        m = re.search(p, url, re.I)
        if m:
            return m.group(1)
    tail = url.rstrip("/").split("/")[-1].split("?")[0]
    return slugify(tail, "video")[:30]


def download(url: str, work_dir: Path, cfg: dict) -> tuple[Path, dict]:
    """下载视频（含平台字幕），返回 (视频路径, 元信息)。"""
    yt_dlp = find_bin(cfg["paths"].get("yt_dlp", "yt-dlp"))
    ensure_dir(work_dir)
    vid = _extract_video_id(url)
    out_tmpl = str(work_dir / f"{vid}.%(ext)s")

    cmd = [
        yt_dlp,
        "--no-playlist",
        "-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
        "--merge-output-format", "mp4",
        "--write-subs", "--write-auto-subs",
        "--sub-langs", "zh-Hans,zh-CN,zh,en",
        "--sub-format", "srt/vtt",
        "--convert-subs", "srt",
        "--write-info-json",
        "--no-warnings",
        "-o", out_tmpl,
        url,
    ]
    log("开始下载（含字幕与元信息）…")
    try:
        run_cmd(cmd, check=True)
    except RuntimeError:
        log("带字幕参数下载失败，退回到纯视频下载", "warn")
        run_cmd([
            yt_dlp, "--no-playlist",
            "-f", "bv*[height<=1080]+ba/b",
            "--merge-output-format", "mp4",
            "-o", out_tmpl, url,
        ])

    videos = sorted(
        [p for p in work_dir.glob(f"{vid}.*") if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov")],
        key=lambda p: p.stat().st_size, reverse=True,
    )
    if not videos:
        die(f"下载完成但没找到视频文件，请检查 {work_dir}")
    video = videos[0]

    info = load_json(work_dir / f"{vid}.info.json", {}) or {}
    meta = {
        "source": url,
        "source_type": "url",
        "platform": info.get("extractor_key") or info.get("extractor") or "",
        "title": info.get("title") or vid,
        "uploader": info.get("uploader") or info.get("channel") or "",
        "duration": info.get("duration") or 0,
        "video_id": info.get("id") or vid,
        "video_path": str(video),
    }
    log(f"下载完成：{video.name}（{video.stat().st_size / 1e6:.1f} MB）", "ok")
    return video, meta


def move_sidecar_files(video: Path, work_dir: Path, copy_only: bool = False) -> list[str]:
    """把视频旁边的字幕 / info.json 同步到项目目录，避免后续找不到。

    copy_only=True（本地源）时用复制 —— 绝不挪动用户自己的原始文件。
    copy_only=False（我们自己下载的）时用移动，保持下载目录整洁。
    """
    import shutil

    synced: list[str] = []
    if video.parent == work_dir or not video.parent.exists():
        return synced

    stem = video.stem
    for p in sorted(video.parent.glob(f"{stem}.*")):
        if p == video or p.suffix.lower() in (".mp4", ".mkv", ".webm", ".mov", ".avi"):
            continue
        target = work_dir / p.name
        if target.exists() and target.stat().st_size == p.stat().st_size:
            continue
        try:
            if copy_only:
                shutil.copy2(p, target)
            else:
                if target.exists():
                    target.unlink()
                p.rename(target)
            synced.append(p.name)
        except OSError:
            continue

    # 本地源：只要同名字幕，不搬 info.json 之类无关文件
    if synced:
        verb = "复制" if copy_only else "搬入"
        log(f"已{verb}附属文件：{', '.join(synced)}", "dim")
    return synced


def ingest_local(src: str, work_dir: Path, cfg: dict) -> tuple[Path, dict]:
    """本地文件：直接引用（不转码，避免损失画质/时间）。"""
    p = Path(src).expanduser().resolve()
    if not p.exists():
        die(f"本地文件不存在：{p}")
    ensure_dir(work_dir)
    log(f"使用本地文件：{p.name}", "ok")
    meta = {
        "source": str(p),
        "source_type": "local",
        "platform": "local",
        "title": p.stem,
        "uploader": "",
        "duration": 0,
        "video_id": slugify(p.stem, "video"),
        "video_path": str(p),
    }
    return p, meta


def ingest(src: str, work_dir: Path, cfg: dict) -> tuple[Path, dict]:
    """入口：自动判别 URL / 本地文件。"""
    with Timer("S1 视频入库"):
        if is_url(src):
            video, meta = download(src, work_dir, cfg)
        else:
            video, meta = ingest_local(src, work_dir, cfg)

        ffprobe = find_bin(cfg["paths"].get("ffprobe", "ffprobe"))
        info = ffprobe_video_info(video, ffprobe)
        dur = ffprobe_duration(video, ffprobe)
        meta["duration"] = round(dur or meta.get("duration") or 0, 3)
        meta["width"] = info.get("width")
        meta["height"] = info.get("height")
        meta["fps"] = info.get("r_frame_rate")
        meta["codec"] = info.get("codec_name")

        save_json(meta, work_dir / "meta.json")
        log(
            f"时长 {meta['duration']:.1f}s · "
            f"{meta.get('width')}x{meta.get('height')} · {meta.get('codec')}",
            "info",
        )
    return video, meta
