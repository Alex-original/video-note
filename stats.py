"""管理看板统计（只读），供 /api/admin/stats 调用。"""
import time
from datetime import datetime, timedelta, timezone

import psutil
from sqlalchemy import func

import db
import metrics

BJT = timezone(timedelta(hours=8))

FAIL_LABELS = {
    "resolve": "解析失败",
    "download": "下载失败",
    "transcribe": "转写失败",
    "summarize": "摘要失败",
    "merge": "汇总失败",
    "unknown": "其他",
}


def _today_start():
    now = datetime.now(tz=BJT)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _sum(session, col, *filters):
    q = session.query(func.coalesce(func.sum(col), 0.0))
    for f in filters:
        q = q.filter(f)
    return float(q.scalar() or 0.0)


def _count(session, model, *filters):
    q = session.query(model)
    for f in filters:
        q = q.filter(f)
    return q.count()


def _day_key(ts):
    return datetime.fromtimestamp(ts, tz=BJT).strftime("%m-%d")


def _fmt_ts(ts):
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=BJT).strftime("%m-%d %H:%M")


def _trend_7d(session):
    """近 7 天：每日任务数、消费、成本、新增用户。"""
    now = time.time()
    start = now - 7 * 24 * 3600
    days = [(datetime.now(tz=BJT) - timedelta(days=6 - i)).strftime("%m-%d") for i in range(7)]
    task_cnt = {d: 0 for d in days}
    cost_sum = {d: 0.0 for d in days}
    revenue_sum = {d: 0.0 for d in days}
    user_cnt = {d: 0 for d in days}

    for t in session.query(db.Task).filter(db.Task.created_at >= start).all():
        k = _day_key(t.created_at)
        if k in task_cnt:
            task_cnt[k] += 1
            cost_sum[k] += t.cost
    for b in session.query(db.Billing).filter(db.Billing.created_at >= start, db.Billing.type == "consume").all():
        k = _day_key(b.created_at)
        if k in revenue_sum:
            revenue_sum[k] += -b.amount  # consume 为负数
    for u in session.query(db.User).filter(db.User.created_at >= start).all():
        k = _day_key(u.created_at)
        if k in user_cnt:
            user_cnt[k] += 1

    return {
        "labels": days,
        "tasks": [task_cnt[d] for d in days],
        "cost": [round(cost_sum[d], 4) for d in days],
        "revenue": [round(revenue_sum[d], 2) for d in days],
        "new_users": [user_cnt[d] for d in days],
    }


def _system():
    import os
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/data")
    try:
        load = os.getloadavg()
    except Exception:
        load = [0, 0, 0]
    c = metrics.snapshot()
    return {
        "cpu": round(cpu, 1),
        "mem_percent": mem.percent,
        "mem_used_gb": round(mem.used / 1024 ** 3, 1),
        "mem_total_gb": round(mem.total / 1024 ** 3, 1),
        "disk_percent": disk.percent,
        "disk_used_gb": round(disk.used / 1024 ** 3, 1),
        "disk_total_gb": round(disk.total / 1024 ** 3, 1),
        "load": [round(x, 2) for x in load],
        "llm_rate_limits": c.get("llm_rate_limits", 0),
        "llm_errors": c.get("llm_errors", 0),
        "asr_errors": c.get("asr_errors", 0),
    }


def get_admin_stats():
    session = db.get_session()
    try:
        today = _today_start()

        # 概览
        users_total = _count(session, db.User)
        users_today = _count(session, db.User, db.User.created_at >= today)
        revenue_total = -_sum(session, db.Billing.amount, db.Billing.type == "consume")
        revenue_today = -_sum(session, db.Billing.amount, db.Billing.type == "consume", db.Billing.created_at >= today)
        recharge_total = _sum(session, db.Billing.amount, db.Billing.type == "recharge")
        cost_total = _sum(session, db.Task.cost)
        cost_today = _sum(session, db.Task.cost, db.Task.created_at >= today)
        balance_total = _sum(session, db.User.balance)

        # 任务状态
        status = {k: _count(session, db.Task, db.Task.status == k)
                  for k in ("running", "completed", "failed", "cancelled")}
        completed = status["completed"]
        failed = status["failed"]
        cancelled = status["cancelled"]
        denom = completed + failed + cancelled
        stale = time.time() - 600
        stuck = _count(session, db.Task, db.Task.status == "running", db.Task.updated_at < stale)

        # 失败原因
        fail_rows = (session.query(db.Task.fail_reason, func.count())
                     .filter(db.Task.status == "failed")
                     .group_by(db.Task.fail_reason).all())
        fail_reasons = {FAIL_LABELS.get(r[0] or "unknown", "其他"): r[1] for r in fail_rows}

        # 成本明细
        cost_detail = {
            "input_tokens": int(_sum(session, db.Task.input_tokens)),
            "output_tokens": int(_sum(session, db.Task.output_tokens)),
            "asr_seconds": round(_sum(session, db.Task.asr_seconds), 0),
            "avg_cost": round(cost_total / completed, 4) if completed else 0.0,
            "subtitle": _count(session, db.Task, db.Task.status == "completed", db.Task.asr_seconds == 0),
            "transcribe": _count(session, db.Task, db.Task.status == "completed", db.Task.asr_seconds > 0),
        }

        trend = _trend_7d(session)

        # 充值意向（点击充值按钮次数）
        recharge_clicks_total = _count(session, db.Event, db.Event.type == "recharge_click")
        recharge_clicks_today = _count(session, db.Event, db.Event.type == "recharge_click", db.Event.created_at >= today)
        recharge_clicks_users = session.query(func.count(func.distinct(db.Event.user_id))).filter(db.Event.type == "recharge_click").scalar() or 0

        return {
            "overview": {
                "users_total": users_total, "users_today": users_today,
                "revenue_total": round(revenue_total, 2), "revenue_today": round(revenue_today, 2),
                "recharge_total": round(recharge_total, 2),
                "cost_total": round(cost_total, 4), "cost_today": round(cost_today, 4),
                "margin_total": round(revenue_total - cost_total, 4),
                "balance_total": round(balance_total, 2),
            },
            "tasks": {
                "total": sum(status.values()), "today": _count(session, db.Task, db.Task.created_at >= today),
                "status": status,
                "success_rate": round(completed / denom * 100, 1) if denom else 0.0,
                "stuck": stuck,
            },
            "recharge_intent": {
                "clicks_total": recharge_clicks_total,
                "clicks_today": recharge_clicks_today,
                "users": recharge_clicks_users,
            },
            "fail_reasons": fail_reasons,
            "cost_detail": cost_detail,
            "trend_7d": trend,
            "system": _system(),
        }
    finally:
        session.close()


def get_table(name):
    """数据库表查看器：返回指定表的最近 50 行（list[dict]）。"""
    session = db.get_session()
    try:
        if name == "users":
            rows = session.query(db.User).order_by(db.User.id.desc()).limit(50).all()
            return [{"id": u.id, "phone": u.phone, "balance": u.balance, "created_at": _fmt_ts(u.created_at)} for u in rows]

        if name == "tasks":
            rows = (session.query(db.Task, db.User.phone)
                    .join(db.User, db.Task.user_id == db.User.id)
                    .order_by(db.Task.id.desc()).limit(50).all())
            return [{"id": t.id, "phone": phone, "title": t.title, "status": t.status,
                     "cost": round(t.cost, 4), "bvid": t.bvid or "",
                     "url": f"https://www.bilibili.com/video/{t.bvid}" if t.bvid else "",
                     "created_at": _fmt_ts(t.created_at)} for t, phone in rows]

        if name == "billing":
            rows = session.query(db.Billing).order_by(db.Billing.id.desc()).limit(50).all()
            return [{"id": b.id, "user_id": b.user_id, "amount": b.amount, "type": b.type,
                     "task_id": b.task_id, "created_at": _fmt_ts(b.created_at)} for b in rows]

        if name == "orders":
            rows = session.query(db.Order).order_by(db.Order.id.desc()).limit(50).all()
            return [{"id": o.id, "user_id": o.user_id, "amount": o.amount, "status": o.status,
                     "provider": o.provider, "out_trade_no": o.out_trade_no,
                     "created_at": _fmt_ts(o.created_at),
                     "paid_at": _fmt_ts(o.paid_at) if o.paid_at else ""} for o in rows]

        if name == "events":
            rows = (session.query(db.Event, db.User.phone)
                    .join(db.User, db.Event.user_id == db.User.id)
                    .order_by(db.Event.id.desc()).limit(50).all())
            return [{"id": e.id, "phone": phone, "type": e.type, "created_at": _fmt_ts(e.created_at)} for e, phone in rows]

        if name == "sms_codes":
            rows = session.query(db.SmsCode).order_by(db.SmsCode.id.desc()).limit(50).all()
            return [{"id": s.id, "phone": s.phone, "code": s.code, "used": s.used,
                     "expires_at": _fmt_ts(s.expires_at)} for s in rows]

        if name == "feedback":
            rows = (session.query(db.Feedback, db.User.phone)
                    .join(db.User, db.Feedback.user_id == db.User.id)
                    .order_by(db.Feedback.id.desc()).limit(50).all())
            return [{"id": f.id, "phone": phone, "category": f.category,
                     "content": f.content, "created_at": _fmt_ts(f.created_at)}
                    for f, phone in rows]

        return None
    finally:
        session.close()
