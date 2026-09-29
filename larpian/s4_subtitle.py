# -*- coding: utf-8 -*-
"""S4 字幕获取：本地 srt / 视频内嵌字幕 / 平台下载的字幕 / whisper 兜底。

输出统一的 subtitles.json：
    [{"index":1, "start":0.0, "end":2.4, "start_tc":"...", "end_tc":"...", "text":"..."}]
"""
from __future__ import annotations

import re
from pathlib import Path

from .utils import (
    find_bin, load_json, log, run_cmd, save_json, sec_to_tc, Timer,
)

# 一些纯语气词/无意义行，用于过滤平台自动字幕的噪音
_NOISE = re.compile(
    r"^(字幕[由志][\w\W]{0,12}|感谢观看|请点赞|关注我|订阅|"
    r"\[.*?\]|\(.*?\)|♪.*♪)$"
)


# ---------------------------------------------------------------- 解析

def parse_srt(text: str) -> list[dict]:
    """解析 SRT 文本。"""
    subs: list[dict] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip())
    for b in blocks:
        lines = [ln for ln in b.split("\n") if ln.strip()]
        if not lines:
            continue
        ti = 0
        if lines[0].strip().isdigit():
            ti = 1
        if len(lines) <= ti:
            continue
        m = re.search(
            r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})"
            r"\s*-->\s*"
            r"(\d{1,2}:\d{2}:\d{2}[,.]\d{1,3}|\d{1,2}:\d{2}[,.]\d{1,3})",
            lines[ti],
        )
        if not m:
            continue
        start = _srt_time(m.group(1))
        end = _srt_time(m.group(2))
        content = " ".join(ln.strip() for ln in lines[ti + 1:]).strip()
        content = re.sub(r"<[^>]+>", "", content).strip()
        if not content:
            continue
        subs.append({"start": start, "end": end, "text": content})

    subs.sort(key=lambda x: x["start"])
    for i, s in enumerate(subs, start=1):
        s["index"] = i
        s["start_tc"] = sec_to_tc(s["start"])
        s["end_tc"] = sec_to_tc(s["end"])
        s["start_short"] = f"{int(s['start'] // 60):02d}:{s['start'] % 60:04.1f}"
    return subs


def _srt_time(t: str) -> float:
    t = t.replace(",", ".").strip()
    parts = t.split(":")
    try:
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return int(parts[0]) * 60 + float(parts[1])
        return float(t)
    except ValueError:
        return 0.0


def _dedupe(subs: list[dict]) -> list[dict]:
    """去掉完全重复的相邻行 + 明显噪音行（平台自动字幕常见）。"""
    out: list[dict] = []
    for s in subs:
        text = s["text"].strip()
        if not text or _NOISE.match(text):
            continue
        if out and text == out[-1]["text"] and (s["start"] - out[-1]["end"]) < 0.5:
            out[-1]["end"] = s["end"]
            out[-1]["end_tc"] = s["end_tc"]
            continue
        out.append(s)
    for i, s in enumerate(out, start=1):
        s["index"] = i
    return out


# ---------------------------------------------------------------- 各来源

def _from_local(video: Path, work_dir: Path) -> list[dict]:
    """项目目录 / 视频同目录 / 同名文件 里的 .srt / .vtt。

    优先级：与视频同名的字幕 > 项目目录里的其他字幕。
    这样用户可以直接把现成的 srt 丢在视频旁边就能用。
    """
    dirs: list[Path] = []
    for d in (work_dir, video.parent):
        if d not in dirs and d.exists():
            dirs.append(d)

    # 1) 与视频同名的字幕（最高优先）
    stem = video.stem
    for d in dirs:
        for ext in ("srt", "vtt"):
            p = d / f"{stem}.{ext}"
            if p.exists() and p.stat().st_size >= 20:
                subs = _read_sub_file(p)
                if subs:
                    log(f"使用同名字幕：{p.name}（{len(subs)} 行）", "ok")
                    return subs

    # 2) 目录里任意字幕文件
    for d in dirs:
        for ext in ("srt", "vtt"):
            for p in sorted(d.glob(f"*.{ext}")):
                if p.stat().st_size < 20:
                    continue
                subs = _read_sub_file(p)
                if subs:
                    log(f"使用字幕文件：{p.name}（{len(subs)} 行）", "ok")
                    return subs
    return []


def _read_sub_file(p: Path) -> list[dict]:
    """读 srt/vtt 文件。自动嗅探编码（平台字幕常是 GBK）。"""
    text = ""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            text = p.read_text(encoding=enc)
            break
        except (UnicodeDecodeError, UnicodeError):
            continue
    if not text:
        return []
    if p.suffix.lower() == ".vtt" or text.lstrip().startswith("WEBVTT"):
        text = text.replace("WEBVTT", "", 1)
    return _dedupe(parse_srt(text))


def _has_subtitle_stream(video: Path, cfg: dict) -> bool:
    """先探测视频里到底有没有内嵌字幕轨，避免 ffmpeg 刷一堆报错。"""
    ffprobe = find_bin(cfg["paths"].get("ffprobe", "ffprobe"))
    import json as _json
    try:
        proc = run_cmd([
            ffprobe, "-v", "error",
            "-select_streams", "s",
            "-show_entries", "stream=index,codec_name",
            "-of", "json", str(video),
        ])
        streams = _json.loads(proc.stdout or "{}").get("streams") or []
        return len(streams) > 0
    except Exception:  # noqa: BLE001
        return False


def _from_embedded(video: Path, work_dir: Path, cfg: dict) -> list[dict]:
    """抽取视频内嵌字幕轨（先探测，没有就安静跳过）。"""
    if not _has_subtitle_stream(video, cfg):
        return []
    ffmpeg = find_bin(cfg["paths"].get("ffmpeg", "ffmpeg"))
    out = work_dir / "subtitle_stream.srt"
    if out.exists():
        out.unlink()
    try:
        run_cmd([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(video), "-map", "0:s:0", "-y", str(out),
        ])
    except RuntimeError:
        return []
    if not out.exists() or out.stat().st_size < 20:
        return []
    subs = parse_srt(out.read_text(encoding="utf-8", errors="ignore"))
    if subs:
        log(f"使用视频内嵌字幕轨（{len(subs)} 行）", "ok")
    return _dedupe(subs)


def _from_whisper(video: Path, work_dir: Path, cfg: dict) -> list[dict]:
    """whisper 兜底转写。只在本机装有 whisper 时启用。

    输出写到独立子目录，绝不覆盖已有的 .srt 文件。
    """
    import shutil
    whisper = None
    for cand in ("whisper", "whisper-cli", "faster-whisper"):
        w = shutil.which(cand)
        if w:
            whisper = w
            break
    if not whisper:
        log("未检测到 whisper，跳过语音转写（台词列留空，可手工补或放一份 srt 到视频旁）", "warn")
        return []

    sc = cfg["subtitle"]
    model = sc.get("whisper_model", "small")
    lang = sc.get("whisper_lang", "zh")
    out_dir = work_dir / "_whisper"
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"调用 whisper 转写（模型 {model}，语言 {lang}），长视频耗时较长…")
    try:
        run_cmd([
            whisper, str(video),
            "--model", model, "--language", lang,
            "--output_format", "srt",
            "--output_dir", str(out_dir),
            "--fp16", "False",
        ], check=True)
    except RuntimeError:
        log("whisper 转写失败", "warn")
        return []

    for p in sorted(out_dir.glob(f"{video.stem}*.srt")):
        subs = parse_srt(p.read_text(encoding="utf-8", errors="ignore"))
        if subs:
            log(f"whisper 转写完成（{len(subs)} 行）", "ok")
            return _dedupe(subs)
    return []


# ---------------------------------------------------------------- 入口

def fetch(video: Path, work_dir: Path, cfg: dict) -> list[dict]:
    """按配置顺序依次尝试各字幕来源，返回统一结构。"""
    if not cfg["subtitle"].get("enabled", True):
        log("字幕功能已关闭", "warn")
        return []

    with Timer("S4 获取台词字幕"):
        order = cfg["subtitle"].get("prefer") or ["local", "embedded", "platform", "whisper"]
        handlers = {
            "local": lambda: _from_local(video, work_dir),
            "embedded": lambda: _from_embedded(video, work_dir, cfg),
            "platform": lambda: _from_local(video, work_dir),   # 平台字幕已由 yt-dlp 落盘
            "whisper": lambda: _from_whisper(video, work_dir, cfg),
        }
        subs: list[dict] = []
        used = "none"
        for key in order:
            fn = handlers.get(key)
            if not fn:
                continue
            subs = fn()
            if subs:
                used = key
                break

        if not subs:
            log("未获取到任何字幕，台词列留空（可在拉片表里手工补）", "warn")

        save_json({"source": used, "count": len(subs), "subtitles": subs},
                  work_dir / "subtitles.json")
        log(f"字幕来源：{used} · 共 {len(subs)} 行", "ok" if subs else "warn")
    return subs
