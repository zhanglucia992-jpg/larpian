# -*- coding: utf-8 -*-
"""配置加载：config.yaml + 环境变量覆盖。"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIG = {
    "paths": {
        "ffmpeg": "ffmpeg",
        "ffprobe": "ffprobe",
        "yt_dlp": "yt-dlp",
        "work_dir": "output",
    },
    "scene": {
        # content 检测器阈值：越小越敏感（切得越碎）
        "detector": "content",          # content | adaptive | hash
        "threshold": 27.0,
        "adaptive_threshold": 3.0,
        "min_shot_sec": 0.6,            # 短于此的镜头并入前一个
        "merge_gap_sec": 0.15,
    },
    "keyframe": {
        "images_per_shot": 2,           # 每镜抽几张（1 或 2）
        "positions": [0.12, 0.62],      # 抽帧相对位置（避开转场与末帧）
        "width": 720,                   # 输出宽度，控制 token 消耗
        "quality": 3,                   # ffmpeg -q:v，2 最高 31 最低
    },
    "subtitle": {
        "enabled": True,
        "prefer": ["local", "embedded", "platform", "whisper"],
        "auto_translate": False,
        "whisper_model": "small",
        "whisper_lang": "zh",
    },
    "vision": {
        # manual = 生成任务单由 Agent 逐帧读图分析（推荐，质量最高）
        # api    = 调用 OpenAI 兼容的多模态接口，全自动
        "provider": "manual",
        "api": {
            "base_url": "https://api.openai.com/v1",
            "api_key_env": "VISION_API_KEY",
            "model": "gpt-4o",
            "max_tokens": 1200,
            "temperature": 0.2,
            "concurrency": 4,
            "timeout": 120,
        },
    },
    "feishu": {
        "enabled": False,
        "app_token": "",                # 多维表格的 app_token（base URL 里的那串）
        "table_id": "",                 # 数据表 id
        "link_field": "",               # 存关键帧链接的字段（可选）
    },
    "export": {
        "csv": True,
        "markdown": True,
        "xlsx": False,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _strip_comment(val: str) -> str:
    """去掉行尾注释（' # ...'）。引号包裹的值原样保留。"""
    if "#" not in val:
        return val.rstrip()
    v = val.strip()
    if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
        return v
    for marker in (" #", "\t#"):
        idx = val.find(marker)
        if idx != -1:
            val = val[:idx]
    return val.rstrip()


def _parse_simple_yaml(text: str) -> dict:
    """极简 YAML 解析（只支持本项目用到的两层结构 + 列表 + 标量）。

    避免强制依赖 PyYAML。如果环境里装了 PyYAML，优先用它。
    """
    try:
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    except ImportError:
        pass

    root: dict = {}
    stack = [(-1, root)]
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        line = _strip_comment(raw.strip())
        if not line:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if line.startswith("- "):
            item = _scalar(line[2:].strip())
            if isinstance(parent, list):
                parent.append(item)
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        # 行内列表 [a, b]
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1].strip()
            parent[key] = [_scalar(x.strip()) for x in inner.split(",")] if inner else []
        elif val == "":
            # 可能是 dict，也可能是 list —— 先建 dict，遇到 "- " 再换成 list
            parent[key] = {}
            stack.append((indent, parent[key]))
        else:
            parent[key] = _scalar(val)
    return root


def _scalar(v: str):
    v = v.strip().strip('"').strip("'")
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    if v.lower() in ("null", "none", "~"):
        return None
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def load_config(path: str | Path | None = None) -> dict:
    """加载配置：默认值 <- config.yaml <- 环境变量。"""
    cfg = _deep_merge(DEFAULT_CONFIG, {})

    candidates = []
    if path:
        candidates.append(Path(path))
    else:
        here = Path(__file__).resolve().parent.parent
        candidates += [here / "config.yaml", Path.cwd() / "config.yaml"]

    for c in candidates:
        if c.exists():
            try:
                data = _parse_simple_yaml(c.read_text(encoding="utf-8"))
                cfg = _deep_merge(cfg, data or {})
                break
            except Exception as exc:  # noqa: BLE001
                print(f"! 读取配置失败（忽略，用默认值）：{c} -> {exc}")

    # 环境变量覆盖（方便临时切换视觉通道 / API key）
    if os.environ.get("LARPIAN_VISION_PROVIDER"):
        cfg["vision"]["provider"] = os.environ["LARPIAN_VISION_PROVIDER"]
    for key in ("VISION_BASE_URL", "VISION_MODEL"):
        if os.environ.get(key):
            cfg["vision"]["api"][key.replace("VISION_", "").lower()] = os.environ[key]

    return cfg


def resolve_work_dir(cfg: dict, project: str | None = None) -> Path:
    base = Path(cfg["paths"].get("work_dir") or "output")
    if not base.is_absolute():
        base = Path(__file__).resolve().parent.parent / base
    return base / project if project else base
