"""Gradio 界面：粘贴 B 站链接 → 选择分P → 选输出标签 → 实时进度 → 生成总结笔记。

双击「启动工具.command」或运行 `python app.py` 启动，自动打开浏览器。
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import gradio as gr

import video_to_note as vn

OUTDIR = vn.DATA_DIR

_stop_events = {}
_stop_events_lock = threading.Lock()


def _register_stop_event(task_id):
    ev = threading.Event()
    with _stop_events_lock:
        _stop_events[task_id] = ev
    return ev


def _set_stop_event(task_id):
    with _stop_events_lock:
        ev = _stop_events.get(task_id)
    if ev:
        ev.set()


TASKS_FILE = os.path.join(OUTDIR, "tasks.json")
_tasks_lock = threading.Lock()


def _read_tasks_unlocked():
    try:
        with open(TASKS_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def _write_tasks_unlocked(tasks):
    os.makedirs(os.path.dirname(TASKS_FILE), exist_ok=True)
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, ensure_ascii=False, indent=2)


def _load_tasks():
    with _tasks_lock:
        return _read_tasks_unlocked()


def _add_task(task_id, title, message):
    with _tasks_lock:
        tasks = _read_tasks_unlocked()
        tasks.insert(0, {
            "id": task_id,
            "title": title,
            "status": "running",
            "message": message,
            "result_file": "",
            "created_at": time.time(),
            "updated_at": time.time(),
        })
        _write_tasks_unlocked(tasks[:50])


def _update_task(task_id, **kwargs):
    with _tasks_lock:
        tasks = _read_tasks_unlocked()
        for t in tasks:
            if t.get("id") == task_id:
                t.update(kwargs)
                t["updated_at"] = time.time()
                break
        _write_tasks_unlocked(tasks)

STAGE_ICONS = {
    "resolve": "🔍", "info": "📹", "subtitle": "💬", "download": "⬇️",
    "transcribe": "🎙️", "summarize": "🧠", "merge": "📝", "done": "✅",
}

_initial_presets = vn.load_presets()
_preset_names = [p["name"] for p in _initial_presets]


def parse_video(url):
    """解析视频，返回分P选择项 + 提示信息。"""
    url = (url or "").strip()
    if not url:
        return gr.update(choices=[], value=[], visible=False), "请先在上方粘贴链接"
    try:
        bvid = vn.resolve_bvid(url)
        info = vn.get_video_info(bvid)
        pages = info["pages"]
        choices = [
            (f"P{p['page']}｜{p['title']}（{p['duration'] // 60} 分钟）", p["page"])
            for p in pages
        ]
        default = [p["page"] for p in pages]
        tip = f"《{info['title'][:40]}》共 {len(pages)} 个分P，勾选要转换的（默认全选）："
        return gr.update(choices=choices, value=default, visible=True), tip
    except Exception as e:
        return gr.update(choices=[], value=[], visible=False), f"❌ 解析失败：{e}"


def _lookup_prompt(name):
    if not name:
        return None
    for p in vn.load_presets():
        if p["name"] == name:
            return p["prompt"]
    return None


MAX_CONCURRENT = 4


def _count_running():
    return sum(1 for t in _load_tasks() if _effective_status(t) == "running")


def convert(url, selected_parts, preset_name):
    """启动一个后台转换任务并立即返回；进度由任务表轮询展示，不依赖前端连接。"""
    url = (url or "").strip()
    if not url:
        raise gr.Error("请先粘贴视频链接")
    if _count_running() >= MAX_CONCURRENT:
        raise gr.Error(f"当前已有 {MAX_CONCURRENT} 个任务在转换，请稍后再提交")

    page_numbers = list(selected_parts) if selected_parts else None
    merge_prompt = _lookup_prompt(preset_name)
    task_id = str(uuid.uuid4())
    stop_event = _register_stop_event(task_id)
    _add_task(task_id, "转换中...", "任务已提交")

    threading.Thread(
        target=_run_task,
        args=(task_id, url, stop_event, page_numbers, merge_prompt),
        daemon=True,
    ).start()
    raise gr.Info("✅ 已提交转换任务，进度见下方「转换任务进度表」")


def _run_task(task_id, url, stop_event, page_numbers, merge_prompt):
    """后台执行 vn.run，实时更新 tasks.json。"""
    try:
        for ev in vn.run(url, OUTDIR, should_stop=stop_event.is_set,
                         page_numbers=page_numbers, merge_prompt=merge_prompt):
            if ev.get("title"):
                _update_task(task_id, title=ev["title"])
            if ev.get("done"):
                _update_task(task_id, status="completed", message=ev["message"],
                             result_file=ev["path"])
                return
            _update_task(task_id, message=ev["message"])
    except vn.CancelledError:
        _update_task(task_id, status="cancelled", message="已停止转换")
    except Exception as e:
        _update_task(task_id, status="failed", message=f"出错：{e}")


# ---- 标签管理 ----
def load_preset_for_edit(name):
    for p in vn.load_presets():
        if p["name"] == name:
            return p["name"], p["prompt"]
    return "", ""


def save_preset(selected, name, prompt):
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name or not prompt:
        raise gr.Error("标签名称和输出格式要求都不能为空")
    presets = vn.load_presets()
    if selected:
        for p in presets:
            if p["name"] == selected:
                p["name"] = name
                p["prompt"] = prompt
                break
        else:
            presets.append({"name": name, "prompt": prompt})
    else:
        if any(p["name"] == name for p in presets):
            raise gr.Error(f"标签「{name}」已存在")
        presets.append({"name": name, "prompt": prompt})
    vn.save_presets(presets)
    choices = [p["name"] for p in presets]
    return gr.update(choices=choices, value=name)


def new_preset():
    return "", "", gr.update(value=None)


def delete_preset(selected):
    if not selected:
        raise gr.Error("请先选择要删除的标签")
    presets = vn.load_presets()
    presets = [p for p in presets if p["name"] != selected]
    if not presets:
        raise gr.Error("至少保留一个标签")
    vn.save_presets(presets)
    choices = [p["name"] for p in presets]
    return gr.update(choices=choices, value=presets[0]["name"])


# ---- 历史转换任务 ----
STALE_SECONDS = 600  # running 状态超过 10 分钟未更新，视为已中断

STATUS_BADGES = {
    "running": ("🟡 转换中", "#d97706"),
    "completed": ("✅ 已完成", "#16a34a"),
    "failed": ("🔴 失败", "#dc2626"),
    "cancelled": ("⏹️ 已停止", "#6b7280"),
    "interrupted": ("⏸️ 已中断", "#9ca3af"),
}


def _effective_status(t):
    if t.get("status") == "running" and (time.time() - t.get("updated_at", 0)) > STALE_SECONDS:
        return "interrupted"
    return t.get("status")


BJT = timezone(timedelta(hours=8))  # 北京时间（UTC+8）


def _fmt_time(ts):
    return datetime.fromtimestamp(ts, tz=BJT).strftime("%m-%d %H:%M")


def _read_note_preview(path, max_chars=3000):
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n……（内容较长，已截断，下载查看全文）"
        return content
    except Exception:
        return ""


def stop_task(task_id):
    _set_stop_event(task_id)


with gr.Blocks(title="视频转笔记") as demo:
    gr.Markdown("# 🎬 视频 → 总结笔记\n粘贴 B 站视频链接，自动生成结构化 Markdown 笔记。")

    with gr.Row():
        url_input = gr.Textbox(
            label="视频链接",
            placeholder="https://b23.tv/xxxxx  （支持短链 / 完整链接 / BV 号）",
            scale=4,
        )
        parse_btn = gr.Button("① 解析分P", scale=1)
        run_btn = gr.Button("② 开始转换", variant="primary", scale=1)

    parse_info = gr.Markdown()
    part_selector = gr.CheckboxGroup(label="选择要转换的分P", choices=[], visible=False)

    with gr.Row():
        preset_selector = gr.Dropdown(
            label="输出标签 / 格式",
            choices=_preset_names,
            value=_preset_names[0] if _preset_names else None,
            scale=3,
        )

    with gr.Accordion("🏷️ 管理输出标签", open=False):
        preset_name = gr.Textbox(
            label="标签名称",
            value=_initial_presets[0]["name"] if _initial_presets else "",
            placeholder="例如：学术分析文档",
        )
        preset_prompt = gr.Textbox(
            label="输出格式要求（提示词，告诉 AI 怎么排版）",
            value=_initial_presets[0]["prompt"] if _initial_presets else "",
            lines=8,
        )
        with gr.Row():
            save_preset_btn = gr.Button("💾 保存", variant="primary")
            new_preset_btn = gr.Button("➕ 新建")
            delete_preset_btn = gr.Button("🗑️ 删除", variant="stop")

    gr.Markdown("### 📊 转换任务进度表")

    parse_btn.click(fn=parse_video, inputs=[url_input], outputs=[part_selector, parse_info])
    run_btn.click(
        fn=convert,
        inputs=[url_input, part_selector, preset_selector],
    )

    preset_selector.change(
        fn=load_preset_for_edit, inputs=[preset_selector], outputs=[preset_name, preset_prompt],
    )
    save_preset_btn.click(
        fn=save_preset, inputs=[preset_selector, preset_name, preset_prompt], outputs=[preset_selector],
    )
    new_preset_btn.click(fn=new_preset, outputs=[preset_name, preset_prompt, preset_selector])
    delete_preset_btn.click(fn=delete_preset, inputs=[preset_selector], outputs=[preset_selector])

    timer = gr.Timer(5)

    @gr.render(triggers=[timer.tick])
    def render_task_table():
        tasks = _load_tasks()
        if not tasks:
            gr.Markdown("暂无转换任务")
            return
        for t in tasks:
            status = _effective_status(t)
            badge, _ = STATUS_BADGES.get(status, ("❓", "#888"))
            title = t.get("title") or "（未知标题）"
            msg = t.get("message") or ""
            ts = _fmt_time(t.get("created_at") or time.time())
            result = t.get("result_file") or ""
            with gr.Group():
                gr.Markdown(f"**{badge} {title}**  `{ts}`  \n{msg}")
                with gr.Row():
                    if status == "running":
                        gr.Button("⏹️ 停止", variant="stop").click(
                            fn=lambda tid=t["id"]: stop_task(tid)
                        )
                    if status == "completed" and result and os.path.exists(result):
                        gr.DownloadButton("⬇️ 下载", value=result)
                if status == "completed" and result and os.path.exists(result):
                    with gr.Accordion("📄 展开预览", open=False):
                        gr.Markdown(_read_note_preview(result))


def _get_auth():
    user = os.getenv("GRADIO_USERNAME", "")
    password = os.getenv("GRADIO_PASSWORD", "")
    if user and password:
        return (user, password)
    return None


if __name__ == "__main__":
    demo.queue(default_concurrency_limit=8).launch(
        server_name="0.0.0.0",
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
        inbrowser=False,
        auth=_get_auth(),
        allowed_paths=[OUTDIR],
        show_error=True,
        theme=gr.themes.Soft(),
    )
