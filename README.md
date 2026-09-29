# 拉片工具（larpian）

把一个视频自动拆成逐镜拉片表：**切镜头 → 抽关键帧 → 多模态逐镜分析 → 台词对齐 → CSV/Markdown/飞书多维表格**。

```
yt-dlp / 本地文件
   ↓
PySceneDetect 切镜头边界（本地，秒级，免费）
   ↓
ffmpeg 每镜抽 1-2 张关键帧
   ↓
多模态模型逐镜分析 → 景别/运镜/画面内容/情绪/表达核心/用户体验路径/视听分工
   ↓
对齐 srt 时间轴补台词
   ↓
落成拉片表 → CSV / Markdown / 飞书多维表格
人工分析列永远留空，留给你写
```

## 安装

```bash
git clone https://github.com/zhanglucia992-jpg/larpian.git
cd larpian
pip install "scenedetect[opencv]"    # PySceneDetect + opencv
pip install openpyxl                  # 可选，xlsx 导出
pip install openai-whisper            # 可选，无字幕时的语音转写兜底

cp config.example.yaml config.yaml    # 按需修改
./samples/demo.mp4                    # 自带 demo，可直接试跑
```

依赖外部命令：`ffmpeg` / `ffprobe`（brew install ffmpeg）、`yt-dlp`（pip install yt-dlp）、`lark-cli`（可选，推飞书）。

## 快速开始

```bash
# 一条命令跑全流程（URL 或本地视频都行）
python3 pipeline.py <视频URL或路径>
```

跑完会停在「画面分析待回填」，此时：

1. 打开 `output/<项目>/vision_任务单.md`，里面每镜有关键帧路径 + 提示词
2. 让我（或其他多模态模型）逐镜读图分析，结果写进 `output/<项目>/vision_results.json`
3. 重跑合并：

```bash
python3 pipeline.py output/<项目> --resume
```

产出在 `output/<项目>/export/`：CSV（Excel/飞书可导入）、拉片表.md、简洁版.md。

## 常用命令

```bash
# 本地视频，每镜抽 1 张帧，更省 token
python3 pipeline.py ./video.mp4 --images-per-shot 1

# B站/YouTube 链接（自动下载视频+字幕）
python3 pipeline.py https://www.bilibili.com/video/BVxxxx

# 调切镜灵敏度（越小切得越碎）
python3 pipeline.py ./video.mp4 --threshold 22

# 只要切镜+抽帧+台词，不做画面分析
python3 pipeline.py ./video.mp4 --skip-vision

# 清空重跑
python3 pipeline.py ./video.mp4 --force

# 推送到飞书多维表格（自动建表/建字段）
python3 scripts/push_feishu.py output/<项目>

# 推到指定表格
python3 scripts/push_feishu.py output/<项目> --base <app_token> --table <table_id>
```

## 字段说明（拉片表列）

| 列 | 来源 | 说明 |
|---|---|---|
| 镜号/起始/结束/时长 | PySceneDetect | 自动 |
| 景别 | 多模态分析 | 大远景~大特写/空镜 |
| 运镜 | 多模态分析 | 固定/推拉摇移跟/手持/组合运镜等 |
| 画面内容 | 多模态分析 | 客观描述，不评价 |
| 台词 | srt/内嵌字幕/whisper | 按时间轴对齐到镜头 |
| 情绪 | 多模态分析 | 2-4 个词 |
| 表达核心 | 多模态分析 | 镜头的功能定位 |
| 用户体验路径 | 多模态分析 | 看到X→感到Y→想知道Z |
| 视听分工 | 多模态分析 | 视觉/听觉各承担什么、什么关系 |
| **人工分析** | **留空** | **你的判断：为什么这样剪、可复用的手法** |

## 配置（config.yaml）

- `scene.threshold`：切镜灵敏度，默认 27，22~32 常用。访谈类调大、动作片调小
- `keyframe.images_per_shot`：每镜抽帧数，2 能看出镜头内运动
- `subtitle.prefer`：字幕来源优先级，默认本地 > 内嵌 > 平台 > whisper
- `vision.provider`：`manual`（Agent 读图分析，推荐）或 `api`（OpenAI 兼容接口全自动，key 放环境变量 `VISION_API_KEY`）

## 目录结构

```
output/<项目名>/
├── video / meta.json        # 视频与元信息
├── scenes.json              # 镜头边界
├── keyframes/               # 关键帧 jpg
├── subtitles.json           # 台词
├── vision_tasks.json        # 逐镜分析任务单
├── vision_results.json      # 分析回填（manual 通道）
├── vision.json              # 规整后的分析结果
├── shots_final.json         # 合并后的完整拉片数据
├── feishu_payload.json      # 飞书推送载荷
└── export/                  # CSV + Markdown 拉片表
```

## 依赖

- ffmpeg / ffprobe / yt-dlp
- Python 3.10+，包：`scenedetect[opencv]`（必需）、`openpyxl`（xlsx 导出，可选）
- whisper CLI（可选，兜底转写台词）
- lark-cli（可选，推飞书多维表格）
