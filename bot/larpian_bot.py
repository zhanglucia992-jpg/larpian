#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拉片机器人：飞书私聊发视频 → 全自动拉片 → 飞书多维表格 → 回表格链接。

私聊机器人发视频文件（mp4/mov/mkv/webm…）或视频链接即可。

可选配置 bot/.env：
  VISION_API_KEY=...      多模态 key（配了才做画面分析）
  VISION_BASE_URL=...     OpenAI 兼容地址（可选）
  VISION_MODEL=...        模型名（可选）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = "/Users/zhangyingying10/.workbuddy/binaries/python/envs/default/bin/python"
PIPELINE = ROOT / "pipeline.py"
PUSH = ROOT / "scripts" / "push_feishu.py"
ENV_FILE = ROOT / "bot" / ".env"
STATE_FILE = ROOT / "bot" / "state.json"
LOG_FILE = ROOT / "bot" / "bot.log"
INBOX = ROOT / "bot" / "inbox"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".flv", ".ts", ".wmv", ".3gp"}
URL_RE = re.compile(r"https?://[^\s<>\"'）)】]+")

_lock = threading.Lock()


def log(msg: str) -> None:
    line = f"[{time.strftime('%m-%d %H:%M:%S')}] {msg}"
    with _lock:
        print(line, flush=True)
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass


def lark(args: list[str], timeout: int = 300) -> dict:
    """跑一条 lark-cli 命令，返回 JSON dict。"""
    try:
        p = subprocess.run(["lark-cli", *args, "--as", "bot", "--json"],
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"message": f"lark-cli 超时: {' '.join(args[:3])}"}}
    try:
        return json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return {"ok": False, "error": {"message": (p.stderr or p.stdout or "无输出")[:300]}}


def load_env() -> dict:
    env = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Bot:
    def __init__(self) -> None:
        self.env = load_env()
        # Kimi/飞书都是国内直连，绕开本机代理，避免 urllib 走代理失败
        self.env["NO_PROXY"] = "*"
        self.env["no_proxy"] = "*"
        self.state = self._load_state()
        self.pool = ThreadPoolExecutor(max_workers=1)  # 串行处理，避免并发 ffmpeg 抢资源
        INBOX.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------ 状态

    def _load_state(self) -> dict:
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {"processed": {}}

    def _save_state(self) -> None:
        items = sorted(self.state["processed"].items(), key=lambda kv: kv[1])[-500:]
        self.state["processed"] = dict(items)
        try:
            STATE_FILE.write_text(json.dumps(self.state, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        except OSError:
            pass

    # ------------------------------------------------------------ 回复

    def reply(self, chat_id: str, text: str) -> None:
        d = lark(["im", "+messages-send", "--chat-id", chat_id, "--text", text])
        if not d.get("ok"):
            log(f"回复失败 -> {chat_id}: {(d.get('error') or {}).get('message', '')[:200]}")

    # ------------------------------------------------------------ 事件循环

    def start(self) -> None:
        log("机器人启动，监听私聊消息…")
        proc = subprocess.Popen(
            ["lark-cli", "event", "consume", "im.message.receive_v1", "--as", "bot"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            stdin=subprocess.PIPE, text=True, bufsize=1,
        )
        threading.Thread(target=self._pump_err, args=(proc,), daemon=True).start()
        try:
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                threading.Thread(target=self._handle_safe, args=(ev,), daemon=True).start()
        except KeyboardInterrupt:
            log("收到中断，退出")
        finally:
            proc.terminate()
            log("监听退出")

    def _pump_err(self, proc) -> None:
        for line in proc.stderr:
            t = line.strip()
            if t:
                log(f"[bus] {t[:160]}")

    def _handle_safe(self, ev: dict) -> None:
        try:
            self._handle(ev)
        except Exception as exc:  # noqa: BLE001
            log(f"处理事件异常: {exc!r}")

    def _handle(self, ev: dict) -> None:
        # 只处理私聊
        chat_type = str(ev.get("chat_type") or "")
        if "p2p" not in chat_type:
            log(f"忽略非私聊消息 ({chat_type})")
            return

        mid = ev.get("message_id") or ev.get("id") or ""
        if not mid or mid in self.state["processed"]:
            return
        self.state["processed"][mid] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save_state()

        chat_id = ev.get("chat_id") or ""
        mtype = str(ev.get("message_type") or "")
        content = ev.get("content") or ""
        log(f"私聊消息 {mid} type={mtype}")

        if mtype in ("file", "media"):
            self.pool.submit(self._process_file_msg, chat_id, mid, mtype, content)
        elif mtype == "text":
            m = URL_RE.search(content)
            if m:
                self.pool.submit(self._process_url, chat_id, m.group(0))
            else:
                self.reply(chat_id, "发我一个视频文件，或视频链接（B站/YouTube等）就行，我来拉片 🎬")
        elif mtype == "post":
            m = URL_RE.search(content)
            if m:
                self.pool.submit(self._process_url, chat_id, m.group(0))
        else:
            log(f"忽略消息类型 {mtype}")

    # ------------------------------------------------------------ 视频消息

    def _get_file_info(self, mid: str, content: str) -> tuple[str, str]:
        """从消息提取 (file_key, file_name)。先试 content JSON，兜底用 mget。"""
        try:
            c = json.loads(content)
            if isinstance(c, dict) and c.get("file_key"):
                return c["file_key"], c.get("file_name") or c.get("name") or "video.mp4"
        except (json.JSONDecodeError, TypeError):
            pass
        d = lark(["im", "+messages-mget", "--message-id", mid])
        try:
            items = (d.get("data") or {}).get("messages") or (d.get("data") or {}).get("items") or []
            for it in items:
                try:
                    c = json.loads(it.get("content") or "{}")
                    if c.get("file_key"):
                        return c["file_key"], c.get("file_name") or c.get("name") or "video.mp4"
                except json.JSONDecodeError:
                    continue
        except Exception:  # noqa: BLE001
            pass
        return "", ""

    def _process_file_msg(self, chat_id: str, mid: str, mtype: str, content: str) -> None:
        file_key, file_name = self._get_file_info(mid, content)
        if not file_key:
            self.reply(chat_id, "没解析到视频文件，重新发一次试试？（直接拖入视频文件发送）")
            return

        name = file_name or "video.mp4"
        ext = Path(name).suffix.lower()
        if ext not in VIDEO_EXTS:
            self.reply(chat_id, f"这个文件（{name}）不是视频，我处理 mp4/mov/mkv/webm 这些格式哦")
            return

        work = INBOX / mid
        work.mkdir(parents=True, exist_ok=True)
        dest = work / f"video{ext}"
        self.reply(chat_id, f"收到视频《{Path(name).stem}》，开始拉片：切镜 → 抽帧 → 台词 → 逐镜分析，预计几分钟…")

        d = lark(["im", "+messages-resources-download",
                  "--message-id", mid, "--file-key", file_key,
                  "--type", "file", "--output", str(dest)], timeout=900)
        if not d.get("ok") or not dest.exists() or dest.stat().st_size < 10000:
            err = (d.get("error") or {}).get("message", "未知错误")
            self.reply(chat_id, f"视频下载失败：{err[:150]}\n试试压缩到 500MB 以内再发，或者直接发视频链接")
            return
        log(f"视频已下载: {dest} ({dest.stat().st_size / 1e6:.1f} MB)")
        self._run_pipeline(chat_id, dest, Path(name).stem)

    def _process_url(self, chat_id: str, url: str) -> None:
        self.reply(chat_id, "收到链接，开始下载并拉片，预计几分钟…")
        self._run_pipeline(chat_id, url, None)

    # ------------------------------------------------------------ 跑流水线

    def _run_pipeline(self, chat_id: str, source: Path | str, name_hint: str | None) -> None:
        t0 = time.time()
        try:
            cmd = [PY, str(PIPELINE), str(source)]
            p = subprocess.run(cmd, capture_output=True, text=True,
                               cwd=str(ROOT), env=self.env, timeout=3600)
            tail = (p.stdout or "")[-600:]
            if p.returncode != 0:
                self.reply(chat_id, f"拉片出错了：\n{(p.stderr or tail)[-400:]}")
                log(f"pipeline 失败: {source}\n{p.stderr[-500:]}")
                return
        except subprocess.TimeoutExpired:
            self.reply(chat_id, "拉片超时（视频太长？），先用短一点的试试")
            return
        except Exception as exc:  # noqa: BLE001
            self.reply(chat_id, f"拉片异常：{exc!r}")
            return

        # 找本项目目录（output/ 下最新且含 shots_final.json 的目录）
        out_root = ROOT / "output"
        proj_dirs = [d for d in out_root.iterdir() if d.is_dir() and (d / "shots_final.json").exists()]
        if not proj_dirs:
            self.reply(chat_id, f"流水线跑完但没找到产物：\n{tail[-300:]}")
            return
        proj = max(proj_dirs, key=lambda d: d.stat().st_mtime)

        stats = {}
        try:
            data = json.loads((proj / "shots_final.json").read_text(encoding="utf-8"))
            stats = data.get("stats") or {}
        except Exception:  # noqa: BLE001
            pass

        # 推飞书多维表格（每个视频新建一张表）
        pf = subprocess.run([sys.executable, str(PUSH), str(proj), "--new-table"],
                            capture_output=True, text=True, cwd=str(ROOT),
                            env=self.env, timeout=900)
        m = re.search(r"(https?://\S*feishu\.cn/base/\S+)", pf.stdout or "")
        table_url = m.group(1).strip() if m else ""
        if not table_url:
            log(f"推飞书输出: {(pf.stdout or '')[-400:]} / err: {(pf.stderr or '')[-300:]}")

        dur = time.time() - t0
        n = stats.get("shot_count", "?")
        total = stats.get("total_duration", 0)
        analyzed = stats.get("analyzed", "?")
        lines = [
            "✅ 拉片完成",
            f"🎬 {n} 镜 · 总时长 {total:.1f}s · 画面分析 {analyzed}/{n} 镜 · 用时 {dur:.0f}s",
        ]
        if table_url:
            lines.append(f"📊 飞书多维表格（「人工分析」列留给你）：{table_url}")
        else:
            lines.append(f"⚠️ 飞书表格推送失败，本地文件在：{proj / 'export'}")
        self.reply(chat_id, "\n".join(lines))
        log(f"任务完成 {source} -> {table_url or '(推表失败)'}")


if __name__ == "__main__":
    Bot().start()
