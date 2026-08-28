"""业务逻辑层：与 UI 解耦，供 FastAPI 调用。

从 app.py（Gradio 版）抽离，去除所有 Gradio 依赖（gr.Error / gr.update），
抛 ServiceError 异常、返回纯数据结构。核心转换逻辑仍复用 video_to_note.py。
"""
import hashlib
import math
import os
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone

import db
import payment
import sms
import video_to_note as vn

OUTDIR = vn.DATA_DIR
MAX_CONCURRENT = 4
PRICE_PER_UNIT = 0.6  # 元
UNIT_SECONDS = 900  # 15 分钟
SESSION_TTL_SECONDS = 7 * 24 * 3600  # 会话 7 天
INITIAL_BALANCE = 1.8  # 新用户赠送初始余额（测试期）
STALE_SECONDS = 600  # running 超时判定为 interrupted

BJT = timezone(timedelta(hours=8))


class ServiceError(Exception):
    """业务异常，main.py 里转成 HTTPException。"""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


# ---------- 停止事件 ----------
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


# ---------- 缓存去重 ----------
def _prompt_hash(merge_prompt):
    return hashlib.md5((merge_prompt or vn.DEFAULT_MERGE_PROMPT).encode("utf-8")).hexdigest()


def _note_suffix(page_numbers, merge_prompt):
    pk = "_".join(str(x) for x in sorted(page_numbers))
    return f"__p{pk}__{_prompt_hash(merge_prompt)[:6]}"


def _uniquify_note_path(path, page_numbers, merge_prompt):
    base, ext = os.path.splitext(path)
    unique_path = f"{base}{_note_suffix(page_numbers, merge_prompt)}{ext}"
    os.rename(path, unique_path)
    return unique_path


# ---------- 任务持久化 ----------
def _create_task(user_id, title, message, bvid="", page_key="", prompt_hash=""):
    session = db.get_session()
    try:
        task = db.Task(user_id=user_id, title=title, status="running", message=message,
                       result_file="", cost=0.0, created_at=time.time(), updated_at=time.time(),
                       bvid=bvid, page_key=page_key, prompt_hash=prompt_hash)
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


def _count_running():
    session = db.get_session()
    try:
        return session.query(db.Task).filter(db.Task.status == "running").count()
    finally:
        session.close()


def _find_completed_note(user_id, bvid, page_key, prompt_hash):
    session = db.get_session()
    try:
        task = (session.query(db.Task)
                .filter(db.Task.user_id == user_id,
                        db.Task.bvid == bvid,
                        db.Task.page_key == page_key,
                        db.Task.prompt_hash == prompt_hash,
                        db.Task.status == "completed")
                .order_by(db.Task.id.desc())
                .first())
        if task and task.result_file and os.path.exists(task.result_file):
            return task.result_file
        return None
    finally:
        session.close()


def _has_running_same(user_id, bvid, page_key, prompt_hash):
    session = db.get_session()
    try:
        return session.query(db.Task).filter(
            db.Task.user_id == user_id,
            db.Task.bvid == bvid,
            db.Task.page_key == page_key,
            db.Task.prompt_hash == prompt_hash,
            db.Task.status == "running",
        ).count() > 0
    finally:
        session.close()


# ---------- 计费 ----------
def calc_amount(duration_seconds):
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


# ---------- 会话 ----------
def create_session(user_id):
    token = secrets.token_hex(32)
    now = time.time()
    session = db.get_session()
    try:
        session.add(db.Session(token=token, user_id=user_id,
                               created_at=now, expires_at=now + SESSION_TTL_SECONDS))
        session.commit()
        return token
    finally:
        session.close()


def get_user_id_by_token(token):
    if not token:
        return None
    session = db.get_session()
    try:
        s = session.query(db.Session).filter(db.Session.token == token).first()
        if not s or s.expires_at < time.time():
            return None
        return s.user_id
    finally:
        session.close()


def delete_session(token):
    if not token:
        return
    session = db.get_session()
    try:
        session.query(db.Session).filter(db.Session.token == token).delete()
        session.commit()
    finally:
        session.close()


def track_event(user_id, event_type):
    """记录用户行为事件（如充值点击），用于统计充值意愿。"""
    if not user_id or not event_type:
        return
    session = db.get_session()
    try:
        session.add(db.Event(user_id=user_id, type=event_type, created_at=time.time()))
        session.commit()
    finally:
        session.close()


# 管理员可直接编辑的表和字段（白名单，防止误改 id/token/时间戳等）
EDITABLE_FIELDS = {
    "users": {"balance": float, "phone": str},
    "tasks": {"status": str, "title": str},
    "orders": {"status": str},
    "billing": {"amount": float, "type": str},
    "feedback": {"category": str, "content": str},
}

_MODELS = {
    "users": db.User, "tasks": db.Task, "orders": db.Order,
    "billing": db.Billing, "feedback": db.Feedback,
}


def admin_update_row(table, row_id, updates):
    """管理员直接修改某条记录的可编辑字段。updates: {column: value}。"""
    if table not in EDITABLE_FIELDS:
        raise ServiceError("该表不支持编辑")
    model = _MODELS[table]
    session = db.get_session()
    try:
        row = session.get(model, row_id)
        if not row:
            raise ServiceError("记录不存在", status_code=404)
        for column, value in (updates or {}).items():
            if column not in EDITABLE_FIELDS[table]:
                raise ServiceError(f"字段「{column}」不支持编辑")
            caster = EDITABLE_FIELDS[table][column]
            try:
                value = caster(value)
            except (TypeError, ValueError):
                raise ServiceError(f"字段「{column}」值格式不正确")
            setattr(row, column, value)
        session.commit()
        return {"ok": True}
    finally:
        session.close()


def admin_adjust_balance(phone, delta):
    """管理员手动调整余额（正=加，负=减），写 billing 流水。"""
    phone = (phone or "").strip()
    if not phone:
        raise ServiceError("请输入手机号")
    try:
        delta = float(delta)
    except (TypeError, ValueError):
        raise ServiceError("调整金额格式不正确")
    if delta == 0:
        raise ServiceError("调整金额不能为 0")
    session = db.get_session()
    try:
        user = session.query(db.User).filter(db.User.phone == phone).first()
        if not user:
            raise ServiceError("用户不存在", status_code=404)
        user.balance = round(user.balance + delta, 2)
        btype = "recharge" if delta > 0 else "consume"
        session.add(db.Billing(user_id=user.id, amount=delta, type=btype, created_at=time.time()))
        session.commit()
        return {"phone": phone, "balance": user.balance}
    finally:
        session.close()


def submit_feedback(user_id, content, category="问题反馈"):
    """用户意见反馈。"""
    content = (content or "").strip()
    if not content:
        raise ServiceError("反馈内容不能为空")
    if len(content) > 2000:
        raise ServiceError("反馈内容过长（最多 2000 字）")
    session = db.get_session()
    try:
        session.add(db.Feedback(user_id=user_id, category=category,
                                content=content, created_at=time.time()))
        session.commit()
    finally:
        session.close()
    return {"ok": True}


# ---------- 登录 ----------
def send_code(phone):
    ok, msg = sms.send_code(phone)
    if not ok:
        raise ServiceError(msg)
    return msg


def login(phone):
    phone = (phone or "").strip()
    if not phone:
        raise ServiceError("请输入手机号")
    if not (len(phone) == 11 and phone.isdigit()):
        raise ServiceError("手机号格式不正确")

    session = db.get_session()
    try:
        user = session.query(db.User).filter(db.User.phone == phone).first()
        if not user:
            user = db.User(phone=phone, balance=INITIAL_BALANCE, created_at=time.time())
            session.add(user)
            session.commit()
        user_id = user.id
    finally:
        session.close()

    token = create_session(user_id)
    return {"token": token, "phone": phone}


# ---------- 转换 ----------
def parse_video(url):
    url = (url or "").strip()
    if not url:
        raise ServiceError("请先粘贴视频链接")
    try:
        bvid = vn.resolve_bvid(url)
        info = vn.get_video_info(bvid)
    except Exception as e:
        raise ServiceError(f"解析视频失败：{e}")
    return {
        "bvid": bvid,
        "title": info["title"],
        "owner": info["owner"],
        "pages": [{"page": p["page"], "title": p["title"], "duration": p["duration"]}
                  for p in info["pages"]],
    }


def build_estimate(pages, selected_pages, user_id):
    sel = {int(x) for x in selected_pages} if selected_pages else set()
    total_dur = sum(p["duration"] for p in pages if p["page"] in sel)
    return {
        "duration_seconds": total_dur,
        "amount": calc_amount(total_dur),
        "balance": _get_balance(user_id),
    }


def start_conversion(url, page_numbers, preset_name, user_id):
    """确认转换：去重 + 预检 + 建任务 + 后台执行。返回 (task_id, cached)。"""
    if not user_id:
        raise ServiceError("请先登录")
    if _count_running() >= MAX_CONCURRENT:
        raise ServiceError(f"当前已有 {MAX_CONCURRENT} 个任务在转换，请稍后再提交", status_code=429)

    info = parse_video(url)
    bvid = info["bvid"]
    pages = info["pages"]
    sel = {int(x) for x in page_numbers} if page_numbers else set()
    if not sel:
        raise ServiceError("请至少选择一个分P")
    sel = {p for p in sel if any(pg["page"] == p for pg in pages)}
    if not sel:
        raise ServiceError("所选分P无效")

    merge_prompt = _lookup_prompt(user_id, preset_name)
    page_key = ",".join(str(x) for x in sorted(sel))
    prompt_hash = _prompt_hash(merge_prompt)

    # 去重 1：进行中
    if _has_running_same(user_id, bvid, page_key, prompt_hash):
        raise ServiceError("该视频（相同分P与标签）正在转换中，请勿重复提交")

    # 去重 2：已完成 → 复用（不新建记录）
    cached = _find_completed_note(user_id, bvid, page_key, prompt_hash)
    if cached:
        return None, True

    total_dur = sum(pg["duration"] for pg in pages if pg["page"] in sel)
    amount = calc_amount(total_dur)
    balance = _get_balance(user_id)
    if balance < amount:
        raise ServiceError(f"余额不足：本次需 ¥{amount:.2f}，当前余额 ¥{balance:.2f}")

    page_numbers = sorted(sel)
    task_id = _create_task(user_id, info["title"], "任务已提交",
                           bvid=bvid, page_key=page_key, prompt_hash=prompt_hash)
    stop_event = _register_stop_event(task_id)
    threading.Thread(
        target=_run_task,
        args=(task_id, user_id, url, stop_event, page_numbers, merge_prompt, amount),
        daemon=True,
    ).start()
    return task_id, False


def _run_task(task_id, user_id, url, stop_event, page_numbers, merge_prompt, amount):
    user_dir = os.path.join(OUTDIR, str(user_id))
    stage = "unknown"
    try:
        for ev in vn.run(url, user_dir, should_stop=stop_event.is_set,
                         page_numbers=page_numbers, merge_prompt=merge_prompt):
            stage = ev.get("stage", stage)
            if ev.get("title"):
                _update_task(task_id, title=ev["title"])
            if ev.get("done"):
                path = ev["path"]
                unique_path = _uniquify_note_path(path, page_numbers, merge_prompt)
                usage = ev.get("usage", {})
                _update_task(task_id, status="completed", message=ev["message"],
                             result_file=unique_path, cost=ev.get("cost", 0.0),
                             input_tokens=usage.get("input_tokens", 0),
                             output_tokens=usage.get("output_tokens", 0),
                             asr_seconds=usage.get("asr_seconds", 0.0))
                _charge(user_id, task_id, amount)
                return
            _update_task(task_id, message=ev["message"])
    except vn.CancelledError:
        _update_task(task_id, status="cancelled", message="已停止转换")
    except Exception as e:
        _update_task(task_id, status="failed", message=f"出错：{e}", fail_reason=stage)


def stop_task(task_id, user_id):
    task = _get_task_for_user(user_id, task_id)
    if not task:
        raise ServiceError("任务不存在", status_code=404)
    _set_stop_event(task_id)
    return {"ok": True}


# ---------- 任务查询 ----------
def _effective_status(task):
    if task["status"] == "running" and (time.time() - task["updated_at"]) > STALE_SECONDS:
        return "interrupted"
    return task["status"]


def list_tasks(user_id):
    session = db.get_session()
    try:
        tasks = (session.query(db.Task)
                 .filter(db.Task.user_id == user_id)
                 .order_by(db.Task.id.desc())
                 .limit(50).all())
        result = []
        for t in tasks:
            d = {"id": t.id, "title": t.title, "status": t.status, "message": t.message,
                 "cost": t.cost, "created_at": t.created_at, "updated_at": t.updated_at}
            d["status"] = _effective_status(d)
            d["has_file"] = bool(t.result_file and os.path.exists(t.result_file))
            result.append(d)
        return result
    finally:
        session.close()


def _get_task_for_user(user_id, task_id):
    session = db.get_session()
    try:
        task = (session.query(db.Task)
                .filter(db.Task.id == task_id, db.Task.user_id == user_id).first())
        return task
    finally:
        session.close()


def task_preview(user_id, task_id):
    task = _get_task_for_user(user_id, task_id)
    if not task:
        raise ServiceError("任务不存在", status_code=404)
    if not task.result_file:
        raise ServiceError("笔记尚未生成", status_code=404)
    try:
        with open(task.result_file, encoding="utf-8") as f:
            return f.read()
    except Exception:
        raise ServiceError("笔记文件读取失败", status_code=500)


def task_download_path(user_id, task_id):
    task = _get_task_for_user(user_id, task_id)
    if not task or not task.result_file or not os.path.exists(task.result_file):
        raise ServiceError("笔记文件不存在", status_code=404)
    return task.result_file


# ---------- 充值 ----------
def create_recharge_order(amount, user_id):
    if not user_id:
        raise ServiceError("请先登录")
    try:
        order_id, out_trade_no, qr_code = payment.create_order(user_id, amount)
    except (ValueError, RuntimeError) as e:
        raise ServiceError(str(e))
    return {"order_id": order_id, "out_trade_no": out_trade_no,
            "amount": round(float(amount), 2),
            "qr_code": qr_code, "qr_data_url": payment.qr_to_data_url(qr_code)}


def order_status(user_id, order_id):
    session = db.get_session()
    try:
        order = session.get(db.Order, order_id)
        if not order or order.user_id != user_id:
            raise ServiceError("订单不存在", status_code=404)
        return {"status": order.status, "balance": _get_balance(user_id)}
    finally:
        session.close()


def simulate_pay(order_id, user_id):
    if not user_id:
        raise ServiceError("请先登录")
    session = db.get_session()
    try:
        order = session.get(db.Order, order_id)
        if not order or order.user_id != user_id:
            raise ServiceError("订单不存在", status_code=404)
        out_trade_no = order.out_trade_no
    finally:
        session.close()
    payment.mark_paid(out_trade_no, f"SIMULATED-{order_id}", provider="manual")
    return {"balance": _get_balance(user_id)}


# ---------- 标签（per-user，存 DB）----------
def _user_presets(user_id):
    """该用户自己的标签列表；无记录返回 None（表示用默认种子）。"""
    session = db.get_session()
    try:
        rows = (session.query(db.Preset)
                .filter(db.Preset.user_id == user_id)
                .order_by(db.Preset.id.asc()).all())
        if not rows:
            return None
        return [{"name": r.name, "prompt": r.prompt} for r in rows]
    finally:
        session.close()


def _write_user_presets(user_id, presets):
    """把完整标签列表写入该用户（先删后插）。"""
    session = db.get_session()
    try:
        session.query(db.Preset).filter(db.Preset.user_id == user_id).delete()
        for p in presets:
            session.add(db.Preset(user_id=user_id, name=p["name"], prompt=p["prompt"], created_at=time.time()))
        session.commit()
    finally:
        session.close()


def list_presets(user_id):
    presets = _user_presets(user_id)
    return presets if presets is not None else list(vn.DEFAULT_PRESETS)


def _lookup_prompt(user_id, name):
    if not name:
        return None
    for p in list_presets(user_id):
        if p["name"] == name:
            return p["prompt"]
    return None


def save_preset(user_id, selected, name, prompt):
    name = (name or "").strip()
    prompt = (prompt or "").strip()
    if not name or not prompt:
        raise ServiceError("标签名称和输出格式要求都不能为空")
    presets = list(list_presets(user_id))
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
            raise ServiceError(f"标签「{name}」已存在")
        presets.append({"name": name, "prompt": prompt})
    _write_user_presets(user_id, presets)
    return presets


def delete_preset(user_id, name):
    if not name:
        raise ServiceError("请先选择要删除的标签")
    presets = list(list_presets(user_id))
    presets = [p for p in presets if p["name"] != name]
    if not presets:
        raise ServiceError("至少保留一个标签")
    _write_user_presets(user_id, presets)
    return presets
