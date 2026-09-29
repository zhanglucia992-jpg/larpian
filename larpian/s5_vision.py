# -*- coding: utf-8 -*-
"""S5 多模态逐镜分析。

两条通道：

  manual（默认，推荐）
      pipeline 跑完生成 vision_tasks.json，由 Agent 逐个读关键帧图片完成分析，
      回填 vision_results.json 后再次运行 pipeline 合并。
      质量最高、零 API 成本，但要 Agent 参与。

  api
      配置 OpenAI 兼容的多模态接口，脚本自动批量请求，全自动无人值守。

统一输出 vision.json：
    {"1": {"shot_size": "...", "camera_move": "...", ...}, ...}
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .utils import load_json, log, save_json, Timer

# 分析字段（英文键 -> 中文表头），导出时按此顺序成表
ANALYSIS_FIELDS: list[tuple[str, str]] = [
    ("shot_size", "景别"),
    ("camera_move", "运镜"),
    ("visual_content", "画面内容"),
    ("emotion", "情绪"),
    ("core_message", "表达核心"),
    ("ux_path", "用户体验路径"),
    ("av_division", "视听分工"),
]

SHOT_SIZES = [
    "大远景", "远景", "全景", "中景", "中近景", "近景", "特写", "大特写", "空镜",
]

CAMERA_MOVES = [
    "固定", "推", "拉", "摇", "移", "跟", "升降", "环绕", "手持晃动",
    "变焦", "甩镜", "组合运镜",
]

SYSTEM_PROMPT = """你是一位资深的影视/短视频拉片分析师，擅长把画面拆解成可复用的创作方法论。
你只输出 JSON，不要输出任何解释文字、不要用 markdown 代码块包裹。"""

USER_PROMPT_TMPL = """这是视频第 {shot_id} 个镜头（时间 {start_short} - {end_short}，时长 {duration}s）的 {n} 张代表帧（按时间先后排列）。

请逐帧观察后，输出严格合法的 JSON（只输出 JSON，字段名与取值必须完全按下述规定）：

{{
  "shot_size": "从这些里选一个：{sizes}",
  "camera_move": "从这些里选一个：{moves}",
  "visual_content": "客观描述画面：谁/什么、在什么环境、做什么动作、构图与光线特点。60-120字。只描述看到的，不评价。",
  "emotion": "这个镜头传递的情绪基调，可用 2-4 个词，如：紧张压迫 / 温暖治愈 / 好奇期待 / 荒诞搞笑 / 平静克制",
  "core_message": "导演用这个镜头想让观众接收到什么信息或感受？这是镜头的功能定位，40-80字。",
  "ux_path": "观众在这一镜里的注意力走向和心理动作。用 '看到X → 感到Y → 想知道Z' 这样的链条表达，30-70字。",
  "av_division": "视听分工：视觉承担了什么、听觉（台词/音乐/音效）承担了什么、两者是什么关系（互补/对位/反差/听觉主导）。40-80字。"
}}

参考语境：本镜头台词为「{dialogue}」（若为「（无）」表示该镜头无台词）。
{extra}
注意：如果 {n} 张帧之间画面差异明显（说明镜头内有运动或变化），请在 visual_content 中说明运动方向与变化过程。"""


# ---------------------------------------------------------------- 任务单

def _dialogue_map(subs: list[dict], scenes: dict) -> dict[int, list[str]]:
    """把字幕按时间轴分配到镜头。"""
    out: dict[int, list[str]] = {s["shot_id"]: [] for s in scenes["shots"]}
    for sub in subs or []:
        mid = (sub["start"] + sub["end"]) / 2
        for shot in scenes["shots"]:
            if shot["start"] <= mid < shot["end"] or (
                sub["start"] >= shot["start"] and sub["start"] < shot["end"]
            ):
                out[shot["shot_id"]].append(sub["text"])
                break
    return out


def build_tasks(work_dir: Path, cfg: dict, kf: list[dict], scenes: dict,
                subs: list[dict]) -> list[dict]:
    """生成逐镜分析任务单（含帧绝对路径）。"""
    dmap = _dialogue_map(subs, scenes)
    tasks: list[dict] = []
    for shot, m in zip(scenes["shots"], kf):
        frames = [f["path"] for f in m["frames"]]
        dialogue = " ".join(dmap.get(shot["shot_id"], [])) or "（无）"
        tasks.append({
            "shot_id": shot["shot_id"],
            "start": shot["start"],
            "end": shot["end"],
            "duration": shot["duration"],
            "start_short": shot["start_short"],
            "end_short": shot["end_short"],
            "frames": frames,
            "dialogue": dialogue,
            "prompt": USER_PROMPT_TMPL.format(
                shot_id=shot["shot_id"],
                start_short=shot["start_short"],
                end_short=shot["end_short"],
                duration=shot["duration"],
                n=len(frames) or 1,
                sizes=" / ".join(SHOT_SIZES),
                moves=" / ".join(CAMERA_MOVES),
                dialogue=dialogue,
                extra="",
            ),
        })
    return tasks


# ---------------------------------------------------------------- API 通道

def _b64(path: str) -> tuple[str, str]:
    mime = mimetypes.guess_type(path)[0] or "image/jpeg"
    with open(path, "rb") as f:
        return mime, base64.b64encode(f.read()).decode("ascii")


def _call_api(task: dict, cfg: dict) -> dict:
    import urllib.error
    import urllib.request

    api = cfg["vision"]["api"]
    key = os.environ.get(api.get("api_key_env", "VISION_API_KEY")) or api.get("api_key")
    if not key:
        raise RuntimeError(
            f"未设置 API Key。请 export {api.get('api_key_env', 'VISION_API_KEY')}=xxx 后重试"
        )

    content: list[dict] = [{"type": "text", "text": task["prompt"]}]
    for p in task["frames"]:
        if not Path(p).exists():
            continue
        mime, data = _b64(p)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{data}", "detail": "low"},
        })

    body = {
        "model": api.get("model", "gpt-4o"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        "max_tokens": int(api.get("max_tokens", 1200)),
        "temperature": float(api.get("temperature", 0.2)),
    }
    req = urllib.request.Request(
        api["base_url"].rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(api.get("timeout", 120))) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc

    text = (payload.get("choices") or [{}])[0].get("message", {}).get("content", "")
    return _parse_json_block(text)


def _parse_json_block(text: str) -> dict:
    """从模型输出里抠出 JSON。"""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    return {}


def analyze_via_api(tasks: list[dict], work_dir: Path, cfg: dict) -> dict:
    api = cfg["vision"]["api"]
    workers = max(1, int(api.get("concurrency", 4)))
    results: dict[str, dict] = {}
    done = 0
    log(f"调用多模态接口：模型 {api.get('model')} · 并发 {workers} · 共 {len(tasks)} 镜")

    with Timer("S5 多模态分析（API）"):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_call_api, t, cfg): t for t in tasks}
            for fut in as_completed(futs):
                t = futs[fut]
                done += 1
                try:
                    results[str(t["shot_id"])] = fut.result()
                except Exception as exc:  # noqa: BLE001
                    log(f"  镜 {t['shot_id']} 分析失败：{exc}", "warn")
                    results[str(t["shot_id"])] = {"error": str(exc)}
                if done % 10 == 0:
                    log(f"  分析进度 {done}/{len(tasks)}", "dim")

    save_json(results, work_dir / "vision.json")
    ok = sum(1 for v in results.values() if v and "error" not in v)
    log(f"分析完成 {ok}/{len(tasks)} 镜", "ok" if ok else "warn")
    return results


# ---------------------------------------------------------------- 任务单落盘

def write_task_files(tasks: list[dict], work_dir: Path, meta: dict) -> Path:
    """写 vision_tasks.json + 人可读的 vision_任务单.md。"""
    save_json(tasks, work_dir / "vision_tasks.json")

    lines = [
        f"# 逐镜多模态分析任务单",
        "",
        f"- 视频：{meta.get('title', '')}",
        f"- 镜头数：{len(tasks)}",
        f"- 字段：{' / '.join(cn for _, cn in ANALYSIS_FIELDS)}",
        "",
        "> 用法：交给具备视觉能力的模型，逐镜读图 + 按下方提示词分析，",
        "> 结果写回 `vision_results.json`，键为镜号、值为字段对象，然后重跑 pipeline。",
        "",
    ]
    for t in tasks:
        lines += [
            f"## 镜 {t['shot_id']:04d} · {t['start_short']}–{t['end_short']}（{t['duration']}s）",
            "",
            f"**关键帧**（{len(t['frames'])} 张）",
            "",
        ]
        lines += [f"- `{p}`" for p in t["frames"]]
        lines += ["", f"**台词**：{t['dialogue']}", "", "**提示词**：", "", "```", t["prompt"], "```", ""]

    p = work_dir / "vision_任务单.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


# ---------------------------------------------------------------- 入口

def run(work_dir: Path, cfg: dict, kf: list[dict], scenes: dict,
        subs: list[dict], meta: dict) -> dict:
    """执行 S5：按 provider 分流。"""
    tasks = build_tasks(work_dir, cfg, kf, scenes, subs)
    provider = str(cfg["vision"].get("provider", "manual")).lower()

    # 已有分析结果（Agent 回填 或 上次 API 结果）→ 直接复用
    existing = load_json(work_dir / "vision_results.json") or load_json(work_dir / "vision.json")
    if existing and len(existing) >= len(tasks):
        save_json(existing, work_dir / "vision.json")
        log(f"检测到已存在的分析结果（{len(existing)} 镜），直接复用", "ok")
        return existing

    if provider == "api":
        return analyze_via_api(tasks, work_dir, cfg)

    # manual：产出任务单，等 Agent 回填
    md = write_task_files(tasks, work_dir, meta)
    log(f"已生成逐镜分析任务单：{md.name}（{len(tasks)} 镜待分析）", "ok")
    log("下一步：由多模态模型逐帧读图分析 → 写入 vision_results.json → 重跑 pipeline 合并", "info")
    return {}


def merge_results(work_dir: Path) -> dict:
    """把 vision_results.json 规整为 vision.json。"""
    data = load_json(work_dir / "vision_results.json") or {}
    if not data:
        return {}
    clean: dict[str, dict] = {}
    for k, v in data.items():
        if not isinstance(v, dict):
            continue
        row = {}
        for key, _ in ANALYSIS_FIELDS:
            val = v.get(key)
            if isinstance(val, list):
                val = " / ".join(str(x) for x in val)
            row[key] = ("" if val is None else str(val)).strip()
        clean[str(int(k)) if str(k).isdigit() else str(k)] = row
    save_json(clean, work_dir / "vision.json")
    return clean
