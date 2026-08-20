"""Gradio 界面：登录页 → 转换任务页。

多用户版：手机号 + 邀请码登录，任务/文件按用户隔离。
登录前后是两个独立视图（gr.render 按登录态切换渲染）。
"""
import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone

import gradio as gr

import db
import video_to_note as vn

OUTDIR = vn.DATA_DIR
INVITE_CODE = os.getenv("INVITE_CODE", "")

MAX_CONCURRENT = 4

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


# ---- 任务持久化（数据库）----
def _create_task(user_id, title, message):
    session = db.get_session()
    try:
        task = db.Task(user_id=user_id, title=title, status="running", message=message,
                       result_file="", cost=0.0, created_at=time.time(), updated_at=time.time())
        session.add(task)
        session.commit()
        return task.id
    finally:
        session.close()


def _update_task(task_id, **kwargs):
    session = db.get_session()
    try:
        task = session.get(db.Task, task_id)
        if task:
            for k, v in kwargs.items():
                setattr(task, k, v)
            task.updated_at = time.time()
            session.commit()
    finally:
        session.close()


def _load_tasks(user_id):
    if not user_id:
        return []
    session = db.get_session()
    try:
        tasks = (session.query(db.Task)
                 .filter(db.Task.user_id == user_id)
                 .order_by(db.Task.id.desc())
                 .limit(50).all())
        return [
            {"id": t.id, "title": t.title, "status": t.status, "message": t.message,
             "result_file": t.result_file, "cost": t.cost,
             "created_at": t.created_at, "updated_at": t.updated_at}
            for t in tasks
        ]
    finally:
        session.close()


def _count_running():
    session = db.get_session()
    try:
        return session.query(db.Task).filter(db.Task.status == "running").count()
    finally:
        session.close()


def _get_phone(user_id):
    if not user_id:
        return ""
    session = db.get_session()
    try:
        user = session.get(db.User, user_id)
        return user.phone if user else ""
    finally:
        session.close()


# ---- 计费 ----
PRICE_PER_UNIT = 0.8  # 元
UNIT_SECONDS = 900  # 15 分钟


def calc_amount(duration_seconds):
    """0.8元/15分钟，不足15分钟按15分钟算（向上取整）。"""
    if duration_seconds <= 0:
        return PRICE_PER_UNIT
    units = math.ceil(duration_seconds / UNIT_SECONDS)
    return round(units * PRICE_PER_UNIT, 2)


def _get_balance(user_id):
    if not user_id:
        return 0.0
    session = db.get_session()
    try:
        user = session.get(db.User, user_id)
        return user.balance if user else 0.0
    finally:
        session.close()


def _charge(user_id, task_id, amount):
    """转换完成扣费，写 billing 流水。"""
    session = db.get_session()
    try:
        user = session.get(db.User, user_id)
        if user:
            user.balance = round(user.balance - amount, 2)
            session.add(db.Billing(user_id=user_id, amount=-amount, type="consume",
                                   task_id=task_id, created_at=time.time()))
            session.commit()
    finally:
        session.close()


def recharge(amount, user_id):
    """测试用模拟充值：直接加余额，不接真实支付。"""
    if not user_id:
        raise gr.Error("请先登录")
    try:
        amount = round(float(amount), 2)
    except (TypeError, ValueError):
        raise gr.Error("金额格式不正确")
    if amount <= 0:
        raise gr.Error("金额必须大于 0")
    if amount > 1000:
        raise gr.Error("单次充值不能超过 1000 元")

    session = db.get_session()
    try:
        user = session.get(db.User, user_id)
        if not user:
            raise gr.Error("用户不存在")
        user.balance = round(user.balance + amount, 2)
        session.add(db.Billing(user_id=user_id, amount=amount, type="recharge",
                               created_at=time.time()))
        session.commit()
        new_balance = user.balance
    finally:
        session.close()
    return f"✅ 充值成功 ¥{amount:.2f}，当前余额 ¥{new_balance:.2f}"


# ---- 登录 ----
def login(phone, invite_code):
    phone = (phone or "").strip()
    invite_code = (invite_code or "").strip()
    if not phone:
        raise gr.Error("请输入手机号")
    if not (len(phone) == 11 and phone.isdigit()):
        raise gr.Error("手机号格式不正确")
    if not INVITE_CODE:
        raise gr.Error("服务端未配置邀请码，请联系管理员")
    if invite_code != INVITE_CODE:
        raise gr.Error("邀请码错误")

    session = db.get_session()
    try:
        user = session.query(db.User).filter(db.User.phone == phone).first()
        if not user:
            user = db.User(phone=phone, balance=0.0, created_at=time.time())
            session.add(user)
            session.commit()
        return user.id
    finally:
        session.close()


def logout():
    return None


_initial_presets = vn.load_presets()
_preset_names = [p["name"] for p in _initial_presets]


def _lookup_prompt(name):
    if not name:
        return None
    for p in vn.load_presets():
        if p["name"] == name:
            return p["prompt"]
    return None


def _build_estimate(parsed, selected_pages):
    pages = parsed["pages"]
    sel = {int(x) for x in selected_pages} if selected_pages else set()
    total_dur = sum(d for p, d in pages.items() if p in sel)
    amount = calc_amount(total_dur)
    balance = _get_balance(parsed["user_id"])
    minutes, seconds = divmod(total_dur, 60)
    return (
        f"**时长**：{minutes} 分 {seconds} 秒（已选 {len(sel)} 个分P）\n\n"
        f"**本次费用**：¥{amount:.2f}\n\n"
        f"**当前余额**：¥{balance:.2f}"
    )


def start_convert(url, preset_name, user_id):
    """点「开始转换」：解析视频，弹窗内选分P + 显示预估费用。"""
    if not user_id:
        raise gr.Error("请先登录")
    url = (url or "").strip()
    if not url:
        raise gr.Error("请先粘贴视频链接")
    if _count_running() >= MAX_CONCURRENT:
        raise gr.Error(f"当前已有 {MAX_CONCURRENT} 个任务在转换，请稍后再提交")

    try:
        bvid = vn.resolve_bvid(url)
        info = vn.get_video_info(bvid)
        pages = info["pages"]
    except Exception as e:
        raise gr.Error(f"解析视频失败：{e}")

    choices = [
        (f"P{p['page']}｜{p['title']}（{p['duration'] // 60} 分钟）", p["page"])
        for p in pages
    ]
    default = [p["page"] for p in pages]

    parsed = {
        "url": url,
        "user_id": user_id,
        "merge_prompt": _lookup_prompt(preset_name),
        "title": info["title"],
        "pages": {p["page"]: p["duration"] for p in pages},
    }

    estimate = _build_estimate(parsed, default)
    title_md = f"**视频**：{info['title'][:60]}"
    return (
        title_md,  # confirm_title
        gr.update(choices=choices, value=default),  # part_selector
        estimate,  # estimate_md
        parsed,  # parsed_state
        gr.update(visible=True),  # confirm_modal
    )


def update_estimate(selected_pages, parsed):
    if not parsed:
        return ""
    return _build_estimate(parsed, selected_pages)


def do_convert(selected_pages, parsed):
    """确认转换：用当前勾选的分P执行。"""
    if not parsed:
        raise gr.Error("没有待确认的转换")
    sel = {int(x) for x in selected_pages} if selected_pages else set()
    if not sel:
        raise gr.Error("请至少选择一个分P")

    total_dur = sum(d for p, d in parsed["pages"].items() if p in sel)
    amount = calc_amount(total_dur)
    balance = _get_balance(parsed["user_id"])
    if balance < amount:
        raise gr.Error(f"余额不足：本次需 ¥{amount:.2f}，当前余额 ¥{balance:.2f}")

    page_numbers = sorted(sel)
    task_id = _create_task(parsed["user_id"], parsed["title"], "任务已提交")
    stop_event = _register_stop_event(task_id)
    threading.Thread(
        target=_run_task,
        args=(task_id, parsed["user_id"], parsed["url"], stop_event,
              page_numbers, parsed["merge_prompt"], amount),
        daemon=True,
    ).start()
    return gr.update(visible=False), "", None


def cancel_confirm():
    return gr.update(visible=False), "", None


def _run_task(task_id, user_id, url, stop_event, page_numbers, merge_prompt, amount):
    """后台执行 vn.run，实时更新数据库任务；完成后扣费。"""
    user_dir = os.path.join(OUTDIR, str(user_id))
    try:
        for ev in vn.run(url, user_dir, should_stop=stop_event.is_set,
                         page_numbers=page_numbers, merge_prompt=merge_prompt):
            if ev.get("title"):
                _update_task(task_id, title=ev["title"])
            if ev.get("done"):
                _update_task(task_id, status="completed", message=ev["message"],
                             result_file=ev["path"], cost=ev.get("cost", 0.0))
                _charge(user_id, task_id, amount)
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


# ---- 任务表 ----
STALE_SECONDS = 600

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


BJT = timezone(timedelta(hours=8))


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


def _init_db_with_retry(retries=15, delay=2):
    for i in range(retries):
        try:
            db.init_db()
            return
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(delay)


CSS = """
.modal-box {
    position: fixed !important;
    top: 50% !important;
    left: 50% !important;
    transform: translate(-50%, -50%) !important;
    z-index: 9999 !important;
    background: white !important;
    border: 1px solid #e5e7eb !important;
    border-radius: 14px !important;
    padding: 24px 28px !important;
    box-shadow: 0 16px 60px rgba(0, 0, 0, 0.35) !important;
    width: 720px !important;
    max-width: 94vw !important;
    max-height: 85vh !important;
    overflow-y: auto !important;
}
"""

with gr.Blocks(title="视频转笔记") as demo:
    gr.Markdown("# 🎬 视频 → 总结笔记\n粘贴 B 站视频链接，自动生成结构化 Markdown 笔记。")

    login_state = gr.State(None)
    parsed_state = gr.State(None)
    timer = gr.Timer(5)

    # ---- 登录页（未登录时渲染）----
    @gr.render(inputs=[login_state])
    def render_login(user_id):
        if user_id is not None:
            return
        gr.Markdown("### 🔐 登录")
        with gr.Group():
            phone_input = gr.Textbox(label="手机号", placeholder="11 位手机号")
            code_input = gr.Textbox(label="邀请码", placeholder="内测邀请码")
            login_btn = gr.Button("登录", variant="primary")
        login_btn.click(fn=login, inputs=[phone_input, code_input], outputs=[login_state])

    # ---- 转换页（已登录时渲染）----
    @gr.render(inputs=[login_state])
    def render_main(user_id):
        if user_id is None:
            return
        phone = _get_phone(user_id)
        with gr.Row():
            gr.Markdown(f"### 👤 已登录：{phone}")
            recharge_btn = gr.Button("💰 充值", scale=0)
            logout_btn = gr.Button("退出登录", scale=0)
        logout_btn.click(fn=logout, outputs=[login_state])

        # 充值弹窗（悬浮遮罩）
        with gr.Group(elem_classes=["modal-box"], visible=False) as recharge_modal:
            gr.Markdown("### 💰 充值")
            gr.Markdown(
                "**计费规则**：0.8 元 / 15 分钟，不足 15 分钟按 15 分钟计；有字幕 / 无字幕统一价。\n\n"
                "**费用示例**：5分钟 ¥0.8 ｜ 16分钟 ¥1.6 ｜ 30分钟 ¥1.6 ｜ 60分钟 ¥3.2 ｜ 83分钟 ¥4.8"
            )
            recharge_amount = gr.Number(label="充值金额（元）", value=10, precision=2)
            with gr.Row():
                confirm_recharge_btn = gr.Button("✅ 确认充值", variant="primary")
                cancel_recharge_btn = gr.Button("取消")
            recharge_info = gr.Markdown()

        recharge_btn.click(fn=lambda: gr.update(visible=True), outputs=[recharge_modal])
        confirm_recharge_btn.click(fn=recharge, inputs=[recharge_amount, login_state], outputs=[recharge_info])
        cancel_recharge_btn.click(fn=lambda: gr.update(visible=False), outputs=[recharge_modal])

        with gr.Row():
            url_input = gr.Textbox(
                label="视频链接",
                placeholder="https://b23.tv/xxxxx  （支持短链 / 完整链接 / BV 号）",
                scale=4,
            )
            run_btn = gr.Button("开始转换", variant="primary", scale=1)

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

        # 二次确认弹窗（悬浮遮罩）
        with gr.Group(elem_classes=["modal-box"], visible=False) as confirm_modal:
            gr.Markdown("### ⚠️ 确认转换")
            confirm_title = gr.Markdown()
            part_selector = gr.CheckboxGroup(label="选择要转换的分P", choices=[])
            estimate_md = gr.Markdown()
            with gr.Row():
                confirm_btn = gr.Button("✅ 确认转换", variant="primary")
                cancel_btn = gr.Button("取消", variant="stop")

        run_btn.click(
            fn=start_convert,
            inputs=[url_input, preset_selector, login_state],
            outputs=[confirm_title, part_selector, estimate_md, parsed_state, confirm_modal],
        )
        part_selector.change(
            fn=update_estimate,
            inputs=[part_selector, parsed_state],
            outputs=[estimate_md],
        )
        confirm_btn.click(
            fn=do_convert,
            inputs=[part_selector, parsed_state],
            outputs=[confirm_modal, estimate_md, parsed_state],
        )
        cancel_btn.click(
            fn=cancel_confirm,
            outputs=[confirm_modal, estimate_md, parsed_state],
        )
        preset_selector.change(
            fn=load_preset_for_edit, inputs=[preset_selector], outputs=[preset_name, preset_prompt],
        )
        save_preset_btn.click(
            fn=save_preset, inputs=[preset_selector, preset_name, preset_prompt], outputs=[preset_selector],
        )
        new_preset_btn.click(fn=new_preset, outputs=[preset_name, preset_prompt, preset_selector])
        delete_preset_btn.click(fn=delete_preset, inputs=[preset_selector], outputs=[preset_selector])

    # ---- 任务表（已登录时渲染，timer 周期刷新）----
    @gr.render(inputs=[login_state], triggers=[timer.tick, login_state.change])
    def render_task_table(user_id):
        if user_id is None:
            return
        balance = _get_balance(user_id)
        gr.Markdown(f"### 💰 余额：¥{balance:.2f}")
        gr.Markdown("### 📊 转换任务进度表")
        tasks = _load_tasks(user_id)
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
            cost = t.get("cost") or 0.0
            cost_txt = f"｜💰 ¥{cost:.4f}" if cost else ""
            with gr.Group():
                gr.Markdown(f"**{badge} {title}**  `{ts}`{cost_txt}  \n{msg}")
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


if __name__ == "__main__":
    _init_db_with_retry()
    demo.queue(default_concurrency_limit=8).launch(
        server_name="0.0.0.0",
        server_port=int(os.getenv("GRADIO_SERVER_PORT", "7860")),
        inbrowser=False,
        allowed_paths=[OUTDIR],
        show_error=True,
        theme=gr.themes.Soft(),
        css=CSS,
    )
