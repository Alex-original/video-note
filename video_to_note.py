"""B站视频 → 总结笔记。

核心库 + CLI，供 app.py（Gradio 界面）和命令行共同调用。

用法:
    python video_to_note.py <B站链接或BV号>   # CLI，打印进度

流程: 解析链接 -> 优先字幕 -> (无字幕则下载音频+Whisper转写) -> DeepSeek分块摘要 -> 输出 .md
"""
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import requests
from openai import OpenAI

import metrics


def _get_env(name: str, default: str = "") -> str:
    """读取环境变量，优先 os.environ，其次仓库根目录 .env。"""
    val = os.getenv(name, "")
    if val:
        return val
    repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    env_path = os.path.join(repo_root, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith(f"{name}="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    return val or default


# ---- DeepSeek 配置（与项目一致）----
API_KEY = _get_env("OPENAI_API_KEY")
BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-v4-flash"

# ---- 阿里云百炼 语音识别（云端转写，可选）----
DASHSCOPE_API_KEY = _get_env("DASHSCOPE_API_KEY")
if DASHSCOPE_API_KEY and not DASHSCOPE_API_KEY.startswith("sk-"):
    DASHSCOPE_API_KEY = ""  # 占位符视为未配置
DASHSCOPE_BASE_URL = _get_env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")

# ---- 部署相关配置 ----
DATA_DIR = _get_env("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))  # 数据目录：本地默认 data/，服务器挂载卷 /data
BILI_COOKIES_FILE = _get_env("BILI_COOKIES_FILE", "")  # 服务器场景的 B站 cookies 文件路径
ENABLE_LOCAL_WHISPER = _get_env("ENABLE_LOCAL_WHISPER", "1").lower() in ("1", "true", "yes")

WHISPER_MODEL = "medium"  # small / medium / large-v3
CHUNK_CHARS = 3500        # 每个摘要分块的最大字符数
PARTIAL_RATIO_THRESHOLD = 0.5  # 实际音频时长低于元数据该比例时，判定为付费/充电视频的试看片段

MODEL_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.txt"]
MODEL_SOURCE = "https://modelscope.cn"  # 国内可直连，比 huggingface 快

client = OpenAI(api_key=API_KEY, base_url=BASE_URL)

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://www.bilibili.com",
}


# ---------- 1. 链接解析 ----------
def resolve_bvid(url: str) -> str:
    url = (url or "").strip()
    # 1. 直接找 BV 号（兼容整段分享文本、链接里带 BV 号）
    if m := re.search(r"(BV[0-9A-Za-z]{10})", url):
        return m.group(1)
    # 2. 从粘贴的文本里提取 http(s) 链接（兼容「【标题】 https://b23.tv/xxx」格式）
    if m := re.search(r"https?://\S+", url):
        url = m.group(0).rstrip("，。、；：！？）】》」』\"'<>")
    # 3. 跟随短链重定向，取最终 URL 里的 BV 号
    r = requests.get(url, headers=HEADERS, allow_redirects=True, timeout=15)
    if m := re.search(r"(BV[0-9A-Za-z]{10})", r.url):
        return m.group(1)
    raise ValueError(f"无法从链接解析出 BV 号: {url}")


def get_video_info(bvid: str) -> dict:
    r = requests.get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bvid}, headers=HEADERS, timeout=15,
    )
    data = r.json()["data"]
    pages = [
        {"page": p["page"], "cid": p["cid"],
         "title": p.get("part") or data["title"], "duration": p["duration"]}
        for p in (data.get("pages") or [])
    ]
    if not pages:
        pages = [{"page": 1, "cid": data["cid"], "title": data["title"], "duration": data["duration"]}]
    return {
        "bvid": bvid,
        "title": data["title"],
        "owner": data["owner"]["name"],
        "pages": pages,
    }


# ---------- 2. 字幕（优先）----------
COOKIE_BROWSER = _get_env("COOKIE_BROWSER", "chrome")  # chrome / safari / edge / firefox / none


def _cookie_args() -> list:
    """按优先级返回 yt-dlp 的 cookie 参数：cookies 文件 > 本地浏览器 > 无。"""
    if BILI_COOKIES_FILE and os.path.exists(BILI_COOKIES_FILE):
        return ["--cookies", BILI_COOKIES_FILE]
    if COOKIE_BROWSER and COOKIE_BROWSER != "none":
        return ["--cookies-from-browser", COOKIE_BROWSER]
    return []


def _parse_srt(path: str):
    """解析 SRT/VTT 字幕文件为 [{start,end,text},...]。"""
    with open(path, encoding="utf-8") as f:
        content = f.read()

    def _to_sec(h, mi, s, ms):
        return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms) / (10 ** len(ms))

    segs = []
    for block in content.strip().split("\n\n"):
        lines = [ln.strip() for ln in block.strip().split("\n") if ln.strip()]
        if not lines:
            continue
        time_line = next((ln for ln in lines if "-->" in ln), None)
        if not time_line:
            continue
        m = re.search(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", time_line)
        if not m:
            continue
        start = _to_sec(m.group(1), m.group(2), m.group(3), m.group(4))
        end = _to_sec(m.group(5), m.group(6), m.group(7), m.group(8))
        idx = lines.index(time_line)
        text = " ".join(lines[idx + 1:]).strip()
        if text:
            segs.append({"start": round(start, 1), "end": round(end, 1), "text": text})
    return segs


def get_subtitle(bvid: str, page: int = 1):
    """用 yt-dlp 下载指定分P的 B站 AI 字幕并解析；无字幕/失败返回 None（回退到转写）。"""
    tmp = tempfile.mkdtemp(prefix="bili_sub_")
    out_template = os.path.join(tmp, "sub")
    try:
        cmd = [sys.executable, "-m", "yt_dlp", *_cookie_args(),
               "--skip-download", "--write-auto-subs", "--write-subs",
               "--sub-langs", "ai-zh", "-o", out_template,
               f"https://www.bilibili.com/video/{bvid}?p={page}"]
        subprocess.run(cmd, capture_output=True, timeout=90)
        files = [f for f in os.listdir(tmp) if f.startswith("sub.") and "danmaku" not in f]
        files.sort(key=lambda f: 0 if ".ai-zh." in f else 1)
        for f in files:
            if f.endswith((".srt", ".vtt")):
                segs = _parse_srt(os.path.join(tmp, f))
                if segs:
                    return segs
        return None
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------- 3. 音频下载 ----------
def download_audio(bvid: str, outdir: str, page: int = 1) -> str:
    url = f"https://www.bilibili.com/video/{bvid}?p={page}"
    base = os.path.join(outdir, f"audio_{bvid}_p{page}")
    subprocess.run(
        [sys.executable, "-m", "yt_dlp", *_cookie_args(),
         "-f", "bestaudio/best",
         "-o", f"{base}.%(ext)s", "--no-playlist", url],
        check=True,
    )
    matches = [f for f in os.listdir(outdir) if f.startswith(f"audio_{bvid}_p{page}.")]
    if not matches:
        raise FileNotFoundError("音频下载失败")
    return os.path.join(outdir, matches[0])


# ---------- 4. 转写 ----------
def ensure_model(model_size: str = WHISPER_MODEL) -> str:
    """确保本地有 whisper 模型；缺失时从 ModelScope 直链下载。"""
    d = os.path.expanduser(f"~/.cache/whisper-models/{model_size}")
    os.makedirs(d, exist_ok=True)
    missing = [f for f in MODEL_FILES if not os.path.exists(os.path.join(d, f))]
    if not missing:
        return d
    repo = f"Systran/faster-whisper-{model_size}"
    print(f"[模型] 下载 {model_size} 模型 ({len(missing)} 个文件)...")
    for f in missing:
        url = f"{MODEL_SOURCE}/models/{repo}/resolve/master/{f}"
        subprocess.run(
            ["curl", "-L", "--retry", "3", "--retry-delay", "3", "-C", "-",
             "-o", os.path.join(d, f), url],
            check=True,
        )
    return d


def transcribe(audio_path: str, outdir: str, cache_key: str):
    """生成器：逐个 yield 片段 dict；结束后写 transcript_{cache_key}.json。"""
    from faster_whisper import WhisperModel
    model = WhisperModel(ensure_model(WHISPER_MODEL), device="cpu", compute_type="int8")
    results = []
    segments, _ = model.transcribe(audio_path, language="zh", vad_filter=True, beam_size=5)
    for seg in segments:
        text = seg.text.strip()
        if text:
            d = {"start": round(seg.start, 1), "end": round(seg.end, 1), "text": text}
            results.append(d)
            yield d
    with open(os.path.join(outdir, f"transcript_{cache_key}.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


# ---------- 4.5 云端转写（阿里云百炼 Qwen-ASR）----------
ASR_MODEL = "qwen3-asr-flash"
ASR_CHUNK_SEC = 200  # 每段时长；同步接口上限 5 分钟，且 base64 需 < 10MB


def _audio_duration(audio_path: str) -> float:
    """用 ffprobe 获取音频时长（秒）；失败返回 0。"""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True, timeout=30,
        )
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def _fmt_dur(seconds: float) -> str:
    """把秒数格式化成可读时长，如「5.6 分钟」「38 分钟」。"""
    minutes = seconds / 60
    if minutes >= 10:
        return f"{minutes:.0f} 分钟"
    return f"{minutes:.1f} 分钟"


def _split_audio(audio_path: str, chunk_sec: int = ASR_CHUNK_SEC):
    """把音频切/转成 16kHz 单声道 wav 段，返回 (chunks, tmpdir)，chunks=[(wav路径, 偏移秒)]。"""
    tmpdir = tempfile.mkdtemp(prefix="asr_chunk_")
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
        capture_output=True, text=True,
    )
    try:
        duration = float(r.stdout.strip())
    except ValueError:
        duration = 0.0
    if duration <= chunk_sec:
        out = os.path.join(tmpdir, "chunk_0.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000", out],
            capture_output=True,
        )
        return [(out, 0)], tmpdir

    chunks = []
    # 尾部不足 1 秒的残余不再单独切块（近空音频会导致 ASR 报 "audio is empty"）
    n = int(duration // chunk_sec)
    if duration % chunk_sec > 1.0:
        n += 1
    n = max(n, 1)
    for i in range(n):
        offset = i * chunk_sec
        out = os.path.join(tmpdir, f"chunk_{i}.wav")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(offset), "-t", str(chunk_sec),
             "-i", audio_path, "-vn", "-ac", "1", "-ar", "16000", out],
            capture_output=True,
        )
        if os.path.exists(out):
            chunks.append((out, offset))
    return chunks, tmpdir


def transcribe_cloud(audio_path: str, usage: dict = None):
    """用阿里云百炼 Qwen-ASR 云端转写；返回 [{start,end,text},...]，失败返回 None。"""
    tmpdir = None
    try:
        duration = _audio_duration(audio_path)
        if usage is not None and duration > 0:
            usage["asr_seconds"] += duration
            usage["cost"] += duration * ASR_PRICE_PER_SEC
        client = OpenAI(api_key=DASHSCOPE_API_KEY, base_url=DASHSCOPE_BASE_URL)
        chunks, tmpdir = _split_audio(audio_path)
        segs = []
        for chunk_path, offset in chunks:
            try:
                with open(chunk_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode()
                resp = client.chat.completions.create(
                    model=ASR_MODEL,
                    messages=[{"role": "user", "content": [
                        {"type": "input_audio",
                         "input_audio": {"data": f"data:audio/wav;base64,{b64}", "format": "wav"}}
                    ]}],
                )
                text = (resp.choices[0].message.content or "").strip()
                if text:
                    segs.append({"start": round(offset, 1),
                                 "end": round(offset + ASR_CHUNK_SEC, 1), "text": text})
            except Exception as e:
                metrics.inc("asr_errors")
                print(f"[转写] 分块 {offset}s 转写失败（跳过）: {type(e).__name__}: {e}", flush=True)
        return segs or None
    except Exception as e:
        metrics.inc("asr_errors")
        print(f"[转写] 云端转写异常: {type(e).__name__}: {e}", flush=True)
        return None
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ---------- 5. 成本统计 ----------
# DeepSeek-V4-Flash 峰谷分时计价（元/百万 tokens），2026-08-17 起生效
DEEPSEEK_PRICE = {
    "input_cache_hit": {"peak": 0.10, "offpeak": 0.05},
    "input_cache_miss": {"peak": 3.0, "offpeak": 1.5},
    "output": {"peak": 9.0, "offpeak": 4.5},
}
ASR_PRICE_PER_SEC = 0.00022  # qwen3-asr-flash：约 0.0132 元/分钟


def _is_peak_time() -> bool:
    """北京时间高峰时段：9:00-12:00、14:00-18:00，其余为空闲。"""
    bj_hour = (time.time() // 3600 + 8) % 24
    return 9 <= bj_hour < 12 or 14 <= bj_hour < 18


def _deepseek_cost(prompt_tokens, completion_tokens, cache_hit=0, cache_miss=0) -> float:
    """计算一次 DeepSeek 调用成本（元）。"""
    tier = "peak" if _is_peak_time() else "offpeak"
    if cache_hit + cache_miss <= 0:
        cache_miss = prompt_tokens  # 无缓存信息时全部按未命中计
    input_cost = (cache_hit * DEEPSEEK_PRICE["input_cache_hit"][tier]
                  + cache_miss * DEEPSEEK_PRICE["input_cache_miss"][tier]) / 1e6
    output_cost = completion_tokens * DEEPSEEK_PRICE["output"][tier] / 1e6
    return input_cost + output_cost


# ---------- 6. 摘要 ----------
def _call_llm(messages: list, max_tokens: int = 8000, timeout: float = 300, usage: dict = None) -> str:
    last_err = None
    for attempt in range(2):
        try:
            resp = client.chat.completions.create(
                model=MODEL, messages=messages, max_tokens=max_tokens, temperature=0.3,
                timeout=timeout,
            )
            content = (resp.choices[0].message.content or "").strip()
            if content:
                if usage is not None:
                    u = resp.usage
                    usage["input_tokens"] += u.prompt_tokens
                    usage["output_tokens"] += u.completion_tokens
                    usage["cost"] += _deepseek_cost(
                        u.prompt_tokens, u.completion_tokens,
                        getattr(u, "prompt_cache_hit_tokens", 0) or 0,
                        getattr(u, "prompt_cache_miss_tokens", 0) or 0,
                    )
                return content
            last_err = RuntimeError(f"模型返回空内容 (finish_reason={resp.choices[0].finish_reason})")
        except Exception as e:
            last_err = e
            if getattr(e, "status_code", None) == 429:
                metrics.inc("llm_rate_limits")
            else:
                metrics.inc("llm_errors")
        if attempt == 0:
            time.sleep(3)
    raise RuntimeError(f"LLM 调用失败: {last_err}")


def chunk_segments(segments: list) -> list:
    chunks, buf, buf_start, buf_end, n = [], [], None, None, 0
    for s in segments:
        if buf_start is None:
            buf_start = s["start"]
        buf.append(s["text"])
        buf_end = s["end"]
        n += len(s["text"])
        if n >= CHUNK_CHARS:
            chunks.append({"start": buf_start, "end": buf_end, "text": "".join(buf)})
            buf, buf_start, buf_end, n = [], None, None, 0
    if buf:
        chunks.append({"start": buf_start, "end": buf_end, "text": "".join(buf)})
    return chunks


def _ts(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def summarize_chunk(chunk: dict, idx: int, total: int, usage: dict = None) -> str:
    sys_prompt = (
        "你是专业的财经视频内容整理助手。请把下面这段视频转录文字，"
        "提炼成结构化的要点，保留所有具体数据、点位、时间判断和结论。"
        "用中文，简洁但不要丢失关键信息。"
    )
    user = f"[视频片段 {idx}/{total}，时间 {_ts(chunk['start'])}-{_ts(chunk['end'])}]\n\n{chunk['text']}"
    return _call_llm([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": user},
    ], usage=usage)


# ---------- 5.1 输出标签（预设模板）----------
PRESETS_FILE = os.path.join(DATA_DIR, "presets.json")

DEFAULT_MERGE_PROMPT = (
    "你是专业的财经笔记整理助手。根据下面所有片段的要点，"
    "生成一篇结构清晰的中文总结笔记 Markdown，包含：\n"
    "1. `# 标题`\n"
    "2. 一句话核心结论\n"
    "3. 分主题要点（用二级标题 + 列表，逻辑归类）\n"
    "4. 关键数据/点位/时间节点（列表）\n"
    "5. 尾部加一段『⚠️ 本文为 AI 自动整理，仅供学习参考，不构成投资建议』\n"
    "6. 文档末尾用一个 mermaid 代码块画一张「逻辑关系图」，体现视频的核心论证逻辑（如：核心信号 → 利多/利空因素 → 判断 → 结论/建议），要有分支和对立关系，不要简单把要点排成一条线。节点文字用中文、不超过 8 个。\n\n"
    "mermaid 代码块格式严格参照下面的示例：\n"
    "```mermaid\n"
    "flowchart TD\n"
    "    A[核心信号] --> B[利多因素]\n"
    "    A --> C[利空因素]\n"
    "    B --> D[判断/结论]\n"
    "    C --> D\n"
    "    D --> E[操作建议/展望]\n"
    "```\n\n"
    "直接输出 Markdown 正文（含 mermaid 代码块），不要输出多余解释。"
)

DEFAULT_PRESETS = [
    {"name": "金融分析", "prompt": DEFAULT_MERGE_PROMPT},
    {"name": "学术速览", "prompt": (
        "你是严谨的学术内容整理助手。根据下面所有片段的要点，以论文摘要结构生成 Markdown 笔记，包含：\n"
        "1. `# 标题`\n2. 研究问题\n3. 方法与论证\n4. 主要结论\n5. 局限与延伸\n"
        "直接输出 Markdown 正文，不要输出多余解释。"
    )},
    {"name": "观点提取", "prompt": (
        "你是高效的信息整理助手。根据下面所有片段的要点，提取视频中的核心观点、论据与反驳，生成 Markdown 笔记，包含：\n"
        "1. `# 标题`\n2. 核心观点（列表）\n3. 正方论据\n4. 反方 / 质疑\n5. 结论\n"
        "直接输出 Markdown 正文，不要输出多余解释。"
    )},
    {"name": "故事梗概", "prompt": (
        "你是内容梳理助手。根据下面所有片段的要点，按时间线梳理剧情，生成 Markdown 笔记，包含：\n"
        "1. `# 标题`\n2. 一句话梗概\n3. 时间线剧情（分节，二级标题）\n4. 人物关系\n5. 关键转折与结局\n"
        "直接输出 Markdown 正文，不要输出多余解释。"
    )},
    {"name": "学习笔记", "prompt": (
        "你是专业的课程学习整理助手。根据下面所有片段的要点，生成一份「极简复习指南」Markdown，包含：\n"
        "1. `# 标题`\n"
        "2. 一句话核心主旨（这个视频解决什么问题、传授什么核心技能）\n"
        "3. 关键概念卡片（3~5 个核心干货，每个一行：💡 概念名——一句话解释；🛠️ 实操——一个关键动作）\n"
        "4. 末尾用一个 mermaid 代码块画一张脑图（mindmap），展示「基础概念 → 进阶方法 → 实际应用」的知识层级。\n\n"
        "mermaid 代码块格式严格参照下面的示例：\n"
        "```mermaid\n"
        "mindmap\n"
        "  root((视频主题))\n"
        "    基础概念\n"
        "      概念A\n"
        "      概念B\n"
        "    进阶方法\n"
        "      方法A\n"
        "    实际应用\n"
        "      应用A\n"
        "```\n\n"
        "直接输出 Markdown 正文（含 mermaid 代码块），不要输出多余解释。"
    )},
]


def load_presets() -> list:
    if os.path.exists(PRESETS_FILE):
        try:
            with open(PRESETS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list) and data:
                return data
        except Exception:
            pass
    return list(DEFAULT_PRESETS)


def save_presets(presets: list) -> None:
    os.makedirs(os.path.dirname(PRESETS_FILE), exist_ok=True)
    with open(PRESETS_FILE, "w", encoding="utf-8") as f:
        json.dump(presets, f, ensure_ascii=False, indent=2)


def merge_note(title: str, owner: str, duration: int, summaries: list, system_prompt: str = None, usage: dict = None) -> str:
    joined = "\n\n".join(f"## 片段 {i + 1}\n{s}" for i, s in enumerate(summaries))
    if system_prompt is None:
        system_prompt = DEFAULT_MERGE_PROMPT
    user = (
        f"视频标题: {title}\nUP主: {owner}\n时长: {duration // 60} 分钟\n\n"
        f"各片段要点如下:\n\n{joined}"
    )
    return _call_llm([
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ], max_tokens=8000, usage=usage)


# ---------- 6. 主流程（生成器：逐个 yield 进度事件）----------
class CancelledError(Exception):
    """用户主动停止转换。"""


def _safe_remove(path):
    """删除文件，失败静默（音频清理用）。"""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def run(url: str, outdir: str = ".", should_stop=None, page_numbers=None, merge_prompt=None):
    def _check():
        if should_stop is not None and should_stop():
            raise CancelledError()

    usage = {"input_tokens": 0, "output_tokens": 0, "asr_seconds": 0, "cost": 0.0}
    partial_warnings = []  # 付费/充电视频的试看片段提示（按分P收集）
    os.makedirs(outdir, exist_ok=True)

    yield {"stage": "resolve", "message": "解析链接...", "pct": 0.03}
    bvid = resolve_bvid(url)
    info = get_video_info(bvid)
    pages = info["pages"]
    if page_numbers:
        page_numbers = {int(x) for x in page_numbers}
        pages = [p for p in pages if p["page"] in page_numbers]
    if not pages:
        raise ValueError("未选择任何分P")
    multi = len(pages) > 1
    total_dur = sum(p["duration"] for p in pages)
    part_info = f"，共 {len(pages)} 个分P" if multi else ""
    yield {"stage": "info",
           "message": f"视频：{info['title']}（约 {total_dur // 60} 分钟{part_info}，UP主 {info['owner']}）",
           "title": info["title"],
           "pct": 0.06}

    # 逐分P处理，各分P处理占进度区间 [0.08, 0.56]
    all_segments = []
    for i, page in enumerate(pages, 1):
        _check()
        p_label = f"P{page['page']}"
        base_pct = 0.08 + 0.48 * (i - 1) / len(pages)
        end_pct = 0.08 + 0.48 * i / len(pages)
        p_dur = max(page["duration"], 1)

        if multi:
            yield {"stage": "subtitle", "message": f"—— {p_label}：{page['title'][:24]} ——", "pct": base_pct}

        segments = get_subtitle(bvid, page=page["page"])
        if segments:
            msg = (f"✅ {p_label} 找到字幕 {len(segments)} 条" if multi
                   else f"✅ 找到字幕 {len(segments)} 条，跳过音频转写")
            yield {"stage": "subtitle", "message": msg, "pct": end_pct}
        else:
            yield {"stage": "subtitle",
                   "message": (f"{p_label} 无字幕，改用音频转写" if multi else "未找到字幕，改用音频转写"),
                   "pct": base_pct + 0.04}
            transcript_path = os.path.join(outdir, f"transcript_{bvid}_p{page['page']}.json")
            if os.path.exists(transcript_path):
                with open(transcript_path, encoding="utf-8") as f:
                    segments = json.load(f)
                yield {"stage": "transcribe",
                       "message": f"✅ 已有转写缓存（{len(segments)} 段），跳过转写",
                       "pct": end_pct}
            else:
                audio_path = os.path.join(outdir, f"audio_{bvid}_p{page['page']}.mp4")
                if not os.path.exists(audio_path):
                    yield {"stage": "download", "message": "下载音频中...", "pct": base_pct + 0.04}
                    audio_path = download_audio(bvid, outdir, page=page["page"])

                # 付费/充电视频检测：实际音频时长远小于元数据时长 → 只能拿到试看片段
                actual_dur = _audio_duration(audio_path)
                meta_dur = page.get("duration", 0) or p_dur
                if meta_dur > 0 and 0 < actual_dur < meta_dur * PARTIAL_RATIO_THRESHOLD:
                    label = p_label if multi else "该视频"
                    warn = (f"{label}为充电/付费内容，未付费账号仅能获取前{_fmt_dur(actual_dur)}的试看片段"
                            f"（完整{_fmt_dur(meta_dur)}），笔记内容不完整")
                    partial_warnings.append(warn)
                    yield {"message": f"⚠️ {warn}"}

                segments = None
                # 云端转写优先（配置了 DashScope key 时）
                if DASHSCOPE_API_KEY:
                    yield {"stage": "transcribe",
                           "message": "☁️ 云端转写中（DashScope Paraformer，可能需要几分钟）...",
                           "pct": base_pct + 0.10}
                    cloud_segs = transcribe_cloud(audio_path, usage=usage)
                    _check()
                    if cloud_segs:
                        segments = cloud_segs
                        with open(transcript_path, "w", encoding="utf-8") as f:
                            json.dump(segments, f, ensure_ascii=False, indent=2)
                        yield {"stage": "transcribe",
                               "message": f"✅ 云端转写完成（{len(segments)} 段）",
                               "pct": end_pct}
                    else:
                        yield {"stage": "transcribe",
                               "message": "云端转写失败，回退本地 Whisper...",
                               "pct": base_pct + 0.10}

                # 本地 Whisper 兜底
                if segments is None:
                    if not ENABLE_LOCAL_WHISPER:
                        raise RuntimeError("未找到字幕，且云端转写不可用，本地转写已禁用")
                    segments = []
                    cache_key = f"{bvid}_p{page['page']}"
                    for seg in transcribe(audio_path, outdir, cache_key):
                        segments.append(seg)
                        frac = min(seg["end"] / p_dur, 1.0)
                        yield {"stage": "transcribe",
                               "message": f"转写中 {_ts(seg['end'])} / {_ts(p_dur)}（{frac * 100:.0f}%）",
                               "pct": base_pct + 0.12 + 0.30 * frac}
                        _check()

                # 转写完成，删除音频文件（转写结果已缓存，音频不再需要）
                _safe_remove(audio_path)

        if multi:
            all_segments.append({"start": 0, "end": 0, "text": f"\n\n【{p_label}｜{page['title']}】\n\n"})
        all_segments.extend(segments)

    # 摘要（合并所有分P）
    chunks = chunk_segments(all_segments)
    summaries = []
    for i, c in enumerate(chunks, 1):
        _check()
        pct = 0.60 + 0.25 * (i - 1) / max(len(chunks), 1)
        yield {"stage": "summarize", "message": f"摘要分析：分块 {i}/{len(chunks)}", "pct": pct}
        summaries.append(summarize_chunk(c, i, len(chunks), usage=usage))

    _check()
    yield {"stage": "merge", "message": "汇总生成笔记...", "pct": 0.92}
    note = merge_note(info["title"], info["owner"], total_dur, summaries, system_prompt=merge_prompt, usage=usage)

    safe_title = re.sub(r'[\\/:*?"<>|]', "_", info["title"])
    out_path = os.path.join(outdir, f"{safe_title}.md")
    if partial_warnings:
        warn_block = ("> ⚠️ **内容不完整提示**\n>\n"
                      + "\n".join(f"> - {w}" for w in partial_warnings)
                      + "\n\n---\n\n")
        note = warn_block + note
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(note)
    done_msg = "⚠️ 完成（内容不完整：付费/充电视频仅获取试看片段）" if partial_warnings else "✅ 完成，笔记已生成"
    yield {"stage": "done", "done": True, "message": done_msg,
           "pct": 1.0, "path": out_path,
           "cost": round(usage["cost"], 4), "usage": usage}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("用法: python video_to_note.py <B站链接或BV号>")
        sys.exit(1)
    for ev in run(sys.argv[1]):
        print(f"[{ev['pct'] * 100:3.0f}%] {ev['message']}")
