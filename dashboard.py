"""管理监控看板（只读）：业务 / 任务 / 成本 / 系统 四层指标。

独立端口 7861，Basic Auth 保护（ADMIN_PASSWORD），与 app 同进程运行。
数据来源：PostgreSQL 现有表 + psutil 系统指标 + metrics 进程内计数器。
"""
import os
import time
from datetime import datetime, timedelta, timezone

import gradio as gr
import psutil
from sqlalchemy import func

import db
import metrics

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
DATA_DIR = os.getenv("DATA_DIR", "/data")
BJT = timezone(timedelta(hours=8))

FAIL_LABELS = {
    "resolve": "解析失败",
    "download": "下载失败",
    "transcribe": "转写失败",
    "summarize": "摘要失败",
    "merge": "汇总失败",
    "unknown": "其他",
}
STATUS_LABELS = {"running": "转换中", "completed": "已完成", "failed": "失败", "cancelled": "已停止"}


def _today_start():
    now = datetime.now(tz=BJT)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _sum(s, col, *filters):
    q = s.query(func.coalesce(func.sum(col), 0.0))
    for f in filters:
        q = q.filter(f)
    return float(q.scalar() or 0.0)


def _count(s, model, *filters):
    q = s.query(model)
    for f in filters:
        q = q.filter(f)
    return q.count()


def _fmt_ratio(a, b):
    return f"{a / b * 100:.1f}%" if b else "—"


def _business(s, today):
    users_total = _count(s, db.User)
    users_today = _count(s, db.User, db.User.created_at >= today)
    # billing: consume 为负数（扣费），recharge 为正数
    revenue_total = -_sum(s, db.Billing.amount, db.Billing.type == "consume")
    revenue_today = -_sum(s, db.Billing.amount, db.Billing.type == "consume", db.Billing.created_at >= today)
    recharge_total = _sum(s, db.Billing.amount, db.Billing.type == "recharge")
    recharge_today = _sum(s, db.Billing.amount, db.Billing.type == "recharge", db.Billing.created_at >= today)
    cost_total = _sum(s, db.Task.cost)
    cost_today = _sum(s, db.Task.cost, db.Task.created_at >= today)
    balance_total = _sum(s, db.User.balance)
    return {
        "users_total": users_total, "users_today": users_today,
        "revenue_total": revenue_total, "revenue_today": revenue_today,
        "recharge_total": recharge_total, "recharge_today": recharge_today,
        "cost_total": cost_total, "cost_today": cost_today,
        "balance_total": balance_total,
        "margin_total": revenue_total - cost_total,
    }


def _orders(s, today):
    total = _count(s, db.Order)
    paid = _count(s, db.Order, db.Order.status == "paid")
    pending = _count(s, db.Order, db.Order.status == "pending")
    today_cnt = _count(s, db.Order, db.Order.created_at >= today)
    return {"total": total, "paid": paid, "pending": pending, "today": today_cnt}


def _tasks(s, today):
    total = _count(s, db.Task)
    today_cnt = _count(s, db.Task, db.Task.created_at >= today)
    status = {}
    for k in STATUS_LABELS:
        status[k] = _count(s, db.Task, db.Task.status == k)
    completed = status.get("completed", 0)
    failed = status.get("failed", 0)
    cancelled = status.get("cancelled", 0)
    denom = completed + failed + cancelled
    stale = time.time() - 600
    stuck = _count(s, db.Task, db.Task.status == "running", db.Task.updated_at < stale)
    fail_rows = (s.query(db.Task.fail_reason, func.count())
                 .filter(db.Task.status == "failed")
                 .group_by(db.Task.fail_reason).all())
    fail_dist = {FAIL_LABELS.get(r[0] or "unknown", "其他"): r[1] for r in fail_rows}
    return {
        "total": total, "today": today_cnt, "status": status,
        "success_rate": _fmt_ratio(completed, denom),
        "stuck": stuck, "fail_dist": fail_dist,
    }


def _cost(s):
    input_tokens = _sum(s, db.Task.input_tokens)
    output_tokens = _sum(s, db.Task.output_tokens)
    asr_seconds = _sum(s, db.Task.asr_seconds)
    completed = _count(s, db.Task, db.Task.status == "completed")
    cost_total = _sum(s, db.Task.cost, db.Task.status == "completed")
    subtitle = _count(s, db.Task, db.Task.status == "completed", db.Task.asr_seconds == 0)
    transcribe = _count(s, db.Task, db.Task.status == "completed", db.Task.asr_seconds > 0)
    return {
        "input_tokens": int(input_tokens), "output_tokens": int(output_tokens),
        "asr_seconds": asr_seconds, "completed": completed,
        "avg_cost": cost_total / completed if completed else 0.0,
        "subtitle": subtitle, "transcribe": transcribe,
    }


def _system():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(DATA_DIR)
    try:
        load = os.getloadavg()
    except Exception:
        load = (0.0, 0.0, 0.0)
    counters = metrics.snapshot()
    return {
        "cpu": cpu, "mem_percent": mem.percent,
        "mem_used_gb": mem.used / 1024 ** 3, "mem_total_gb": mem.total / 1024 ** 3,
        "disk_percent": disk.percent, "disk_used_gb": disk.used / 1024 ** 3, "disk_total_gb": disk.total / 1024 ** 3,
        "load": load, "counters": counters,
    }


def _build_summary():
    s = db.get_session()
    try:
        today = _today_start()
        b = _business(s, today)
        o = _orders(s, today)
        t = _tasks(s, today)
        c = _cost(s)
    finally:
        s.close()
    sys_ = _system()

    fail_lines = "、".join(f"{k} {v}" for k, v in sorted(t["fail_dist"].items(), key=lambda x: -x[1])) or "无"

    return f"""## 💰 业务核心

- 总用户 **{b['users_total']}**（今日 +{b['users_today']}）
- 累计消费（收入）**¥{b['revenue_total']:.2f}** ｜ 今日 ¥{b['revenue_today']:.2f}
- 累计充值 **¥{b['recharge_total']:.2f}** ｜ 今日 ¥{b['recharge_today']:.2f}
- 累计成本 **¥{b['cost_total']:.4f}** ｜ 今日 ¥{b['cost_today']:.4f}
- **毛利 ¥{b['margin_total']:.4f}**（收入 − 成本）
- 用户余额合计 **¥{b['balance_total']:.2f}**

## 🧾 充值订单

- 总 {o['total']} ｜ 已支付 {o['paid']} ｜ 待支付 {o['pending']} ｜ 今日 {o['today']}
- 支付成功率 **{_fmt_ratio(o['paid'], o['total'])}**

## ⚙️ 转换任务

- 总 **{t['total']}** ｜ 今日 {t['today']}
- 转换中 {t['status'].get('running', 0)} ｜ 已完成 {t['status'].get('completed', 0)} ｜ 失败 {t['status'].get('failed', 0)} ｜ 已停止 {t['status'].get('cancelled', 0)}
- 成功率 **{t['success_rate']}** ｜ 卡住任务（超时 running）**{t['stuck']}**
- 失败原因：{fail_lines}

## 💸 成本明细

- 累计 token：输入 **{c['input_tokens']:,}** ｜ 输出 **{c['output_tokens']:,}**
- 累计转写时长 **{c['asr_seconds']:.0f} 秒**
- 平均单任务成本 **¥{c['avg_cost']:.4f}**
- 字幕命中 **{c['subtitle']}** 个 ｜ 走转写 **{c['transcribe']}** 个

## 🖥️ 系统

- CPU **{sys_['cpu']:.0f}%** ｜ 内存 **{sys_['mem_percent']:.0f}%**（{sys_['mem_used_gb']:.1f}/{sys_['mem_total_gb']:.1f} GB）
- 磁盘 **{sys_['disk_percent']:.0f}%**（{sys_['disk_used_gb']:.1f}/{sys_['disk_total_gb']:.1f} GB）
- 负载 1/5/15min：{sys_['load'][0]:.2f} / {sys_['load'][1]:.2f} / {sys_['load'][2]:.2f}
- API 计数：LLM 限流 **{sys_['counters'].get('llm_rate_limits', 0)}** ｜ LLM 错误 **{sys_['counters'].get('llm_errors', 0)}** ｜ ASR 错误 **{sys_['counters'].get('asr_errors', 0)}**
"""


def launch():
    with gr.Blocks(title="视频转笔记 · 监控看板") as d:
        gr.Markdown("# 📊 视频转笔记 · 监控看板")
        summary = gr.Markdown()
        timer = gr.Timer(15)
        timer.tick(fn=_build_summary, outputs=[summary])
        d.load(fn=_build_summary, outputs=[summary])
    d.launch(
        server_name="0.0.0.0",
        server_port=int(os.getenv("DASHBOARD_PORT", "7861")),
        inbrowser=False,
        show_error=True,
        auth=[("admin", ADMIN_PASSWORD)],
    )
