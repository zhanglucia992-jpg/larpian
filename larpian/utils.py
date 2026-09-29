# -*- coding: utf-8 -*-
"""通用工具：日志 / 时间码 / 命令行封装 / 路径处理。"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------- 日志

_COLOR = {
    "info": "\033[36m",
    "ok": "\033[32m",
    "warn": "\033[33m",
    "err": "\033[31m",
    "step": "\033[35m",
    "dim": "\033[90m",
}
_RESET = "\033[0m"


def _color_enabled() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def log(msg: str, level: str = "info") -> None:
    """带颜色和级别前缀的打印。"""
    prefix = {
        "info": "·",
        "ok": "✓",
        "warn": "!",
        "err": "✗",
        "step": "▶",
        "dim": " ",
    }.get(level, "·")
    if _color_enabled():
        c = _COLOR.get(level, "")
        print(f"{c}{prefix} {msg}{_RESET}", flush=True)
    else:
        print(f"{prefix} {msg}", flush=True)


def step(title: str) -> None:
    log(f"{'─' * 4} {title} {'─' * 4}", "step")


def die(msg: str, code: int = 1):
    log(msg, "err")
    sys.exit(code)


# ---------------------------------------------------------------- 时间码

def sec_to_tc(seconds: float) -> str:
    """123.456 -> '00:02:03.456'"""
    seconds = max(0.0, float(seconds))
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def sec_to_short(seconds: float) -> str:
    """123.456 -> '02:03.5'（给人看的简写）"""
    seconds = max(0.0, float(seconds))
    m = int(seconds // 60)
    s = seconds % 60
    return f"{m:02d}:{s:04.1f}"


def tc_to_sec(tc: str) -> float:
    """支持 '00:02:03.456' / '02:03.456' / '02:03:12' 等。"""
    tc = tc.strip().replace(",", ".")
    parts = tc.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        m = re.search(r"(\d+):(\d+):(\d+)", tc)
        return 0.0 if not m else int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] if nums else 0.0


# ---------------------------------------------------------------- 命令行

def find_bin(name: str, extra_paths: list[str] | None = None) -> str:
    """在 PATH 和常见位置里找一个可执行文件。"""
    hit = shutil.which(name)
    if hit:
        return hit
    candidates = list(extra_paths or [])
    candidates += [
        str(Path.home() / ".local" / "bin" / name),
        f"/opt/homebrew/bin/{name}",
        f"/usr/local/bin/{name}",
        f"/usr/bin/{name}",
    ]
    for c in candidates:
        if Path(c).exists() and os.access(c, os.X_OK):
            return c
    die(f"找不到可执行文件：{name}，请先安装或写入 config.yaml")


def run_cmd(cmd: list[str], desc: str = "", quiet: bool = True, check: bool = True):
    """跑一条外部命令，返回 CompletedProcess。"""
    if desc:
        log(desc, "dim")
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if quiet else None,
        stderr=subprocess.PIPE if quiet else None,
        text=True,
    )
    if check and proc.returncode != 0:
        err = (proc.stderr or "")[-1500:]
        log(f"命令失败（exit {proc.returncode}）：{' '.join(cmd[:6])}...", "err")
        if err:
            print(err, file=sys.stderr)
        raise RuntimeError(f"命令执行失败：{' '.join(cmd[:3])}")
    return proc


def ffprobe_duration(path: str | Path, ffprobe: str = "ffprobe") -> float:
    """读取视频时长（秒）。"""
    proc = run_cmd(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        quiet=True,
    )
    try:
        return float((proc.stdout or "0").strip())
    except ValueError:
        return 0.0


def ffprobe_video_info(path: str | Path, ffprobe: str = "ffprobe") -> dict:
    """读取分辨率/帧率/编码等基本信息。"""
    proc = run_cmd(
        [
            ffprobe, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate,codec_name,nb_frames",
            "-of", "json",
            str(path),
        ],
        quiet=True,
    )
    try:
        info = json.loads(proc.stdout or "{}")
        return (info.get("streams") or [{}])[0]
    except json.JSONDecodeError:
        return {}


# ---------------------------------------------------------------- 文件

def ensure_dir(p: str | Path) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_json(data, path: str | Path, indent: int = 2) -> Path:
    path = Path(path)
    ensure_dir(path.parent)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )
    return path


def load_json(path: str | Path, default=None):
    path = Path(path)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return default


def slugify(text: str, fallback: str = "project") -> str:
    """把标题变成安全的目录名。"""
    text = re.sub(r"[\\/:*?\"<>|\s]+", "_", (text or "").strip())
    text = re.sub(r"_+", "_", text).strip("_")
    text = text[:60]
    return text or fallback


class Timer:
    def __init__(self, label: str):
        self.label = label

    def __enter__(self):
        self.t0 = time.time()
        return self

    def __exit__(self, *exc):
        log(f"{self.label} 用时 {time.time() - self.t0:.1f}s", "dim")
        return False
