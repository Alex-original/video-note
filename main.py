"""FastAPI 应用：REST API + 静态前端（路线 B）。

运行：uvicorn main:app --host 0.0.0.0 --port 7860
静态前端在 static/ 目录（阶段 2 接入）。
"""
import os
import secrets
import time

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import payment
import service
import stats
from service import ServiceError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

app = FastAPI(title="视频转笔记")


# ---------- 启动：初始化数据库（带重试，等 db 就绪）----------
def _init_db_with_retry(retries=15, delay=2):
    for i in range(retries):
        try:
            db.init_db()
            return
        except Exception:
            if i == retries - 1:
                raise
            time.sleep(delay)


_init_db_with_retry()


# ---------- 异常处理 ----------
@app.exception_handler(ServiceError)
async def handle_service_error(request, exc: ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


# ---------- 静态资源缓存策略（避免改版后浏览器用旧 js/css）----------
@app.middleware("http")
async def add_cache_headers(request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path.endswith(".min.js"):
        # 第三方库基本不变，长缓存省流量
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        # 自研 html/js/css 频繁改版，强制每次拉最新，不依赖浏览器协商
        response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# ---------- 鉴权依赖 ----------
def get_current_user(authorization: str = Header(default="")):
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:].strip()
    user_id = service.get_user_id_by_token(token)
    if not user_id:
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user_id


# ---------- 请求模型 ----------
class SendCodeReq(BaseModel):
    phone: str


class LoginReq(BaseModel):
    phone: str


class ParseReq(BaseModel):
    url: str


class ConvertReq(BaseModel):
    url: str
    pages: list[int]
    preset: str = ""


class RechargeOrderReq(BaseModel):
    amount: float


class SimulatePayReq(BaseModel):
    order_id: int


class SavePresetReq(BaseModel):
    selected: str = ""
    name: str
    prompt: str


class DeletePresetReq(BaseModel):
    name: str


class EventReq(BaseModel):
    type: str


class FeedbackReq(BaseModel):
    content: str
    category: str = "问题反馈"


class AdminAdjustBalanceReq(BaseModel):
    phone: str
    delta: float


class AdminUpdateRowReq(BaseModel):
    updates: dict


class AdminBiliCookieReq(BaseModel):
    cookie: str


# ---------- 认证 ----------
@app.post("/api/auth/send-code")
def send_code(req: SendCodeReq):
    return {"message": service.send_code(req.phone)}


@app.post("/api/auth/login")
def login(req: LoginReq):
    return service.login(req.phone)


@app.post("/api/auth/logout")
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    service.delete_session(token)
    return {"ok": True}


# ---------- 标签 ----------
@app.get("/api/presets")
def list_presets(user_id: int = Depends(get_current_user)):
    return service.list_presets(user_id)


@app.post("/api/presets/save")
def save_preset(req: SavePresetReq, user_id: int = Depends(get_current_user)):
    return service.save_preset(user_id, req.selected, req.name, req.prompt)


@app.post("/api/presets/delete")
def delete_preset(req: DeletePresetReq, user_id: int = Depends(get_current_user)):
    return service.delete_preset(user_id, req.name)


# ---------- 转换 ----------
@app.post("/api/convert/parse")
def parse(req: ParseReq, user_id: int = Depends(get_current_user)):
    return service.parse_video(req.url)


@app.post("/api/convert/start")
def start(req: ConvertReq, user_id: int = Depends(get_current_user)):
    task_id, cached = service.start_conversion(req.url, req.pages, req.preset, user_id)
    return {"task_id": task_id, "cached": cached}


@app.post("/api/task/{task_id}/stop")
def stop(task_id: int, user_id: int = Depends(get_current_user)):
    return service.stop_task(task_id, user_id)


# ---------- 任务 / 余额 ----------
@app.get("/api/tasks")
def tasks(user_id: int = Depends(get_current_user)):
    return {"balance": service._get_balance(user_id), "tasks": service.list_tasks(user_id)}


@app.get("/api/task/{task_id}/preview")
def preview(task_id: int, user_id: int = Depends(get_current_user)):
    return {"content": service.task_preview(user_id, task_id)}


@app.get("/api/task/{task_id}/download")
def download(task_id: int, user_id: int = Depends(get_current_user)):
    path = service.task_download_path(user_id, task_id)
    return FileResponse(path, filename=os.path.basename(path))


# ---------- 充值 ----------
@app.post("/api/recharge/order")
def recharge_order(req: RechargeOrderReq, user_id: int = Depends(get_current_user)):
    return service.create_recharge_order(req.amount, user_id)


@app.post("/api/recharge/simulate")
def simulate_pay(req: SimulatePayReq, user_id: int = Depends(get_current_user)):
    return service.simulate_pay(req.order_id, user_id)


@app.get("/api/recharge/order/{order_id}")
def recharge_order_status(order_id: int, user_id: int = Depends(get_current_user)):
    return service.order_status(user_id, order_id)


@app.post("/api/recharge/callback")
async def recharge_callback(request: Request):
    """支付宝当面付异步回调（公开，无鉴权，靠验签保证安全）。"""
    form = await request.form()
    data = {k: v for k, v in form.items()}
    if not payment.verify_callback_signature("alipay", data):
        return JSONResponse(status_code=400, content={"detail": "验签失败"})
    trade_status = data.get("trade_status", "")
    out_trade_no = data.get("out_trade_no", "")
    trade_no = data.get("trade_no", "")
    if trade_status in ("TRADE_SUCCESS", "TRADE_FINISHED") and out_trade_no:
        payment.mark_paid(out_trade_no, trade_no, provider="alipay",
                          expected_amount=data.get("total_amount"))
    return PlainTextResponse("success")


@app.post("/api/event")
def track_event(req: EventReq, user_id: int = Depends(get_current_user)):
    service.track_event(user_id, req.type)
    return {"ok": True}


@app.post("/api/feedback")
def feedback(req: FeedbackReq, user_id: int = Depends(get_current_user)):
    return service.submit_feedback(user_id, req.content, req.category)


# ---------- 合规文档（公开，无需登录）----------
def _load_doc(name):
    p = os.path.join(BASE_DIR, "docs", f"{name}.md")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read()
    except Exception:
        return f"（{name} 文档缺失）"


@app.get("/api/docs/terms")
def docs_terms():
    return {"content": _load_doc("用户协议")}


@app.get("/api/docs/privacy")
def docs_privacy():
    return {"content": _load_doc("隐私政策")}


# ---------- 管理看板（密码鉴权）----------
def check_admin(x_admin_password: str = Header(default="")):
    if not secrets.compare_digest(x_admin_password, ADMIN_PASSWORD):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="管理员密码错误")
    return True


@app.get("/api/admin/stats")
def admin_stats(_: bool = Depends(check_admin)):
    return stats.get_admin_stats()


@app.get("/api/admin/table/{table}")
def admin_table(table: str, _: bool = Depends(check_admin)):
    from fastapi import HTTPException
    data = stats.get_table(table)
    if data is None:
        raise HTTPException(status_code=404, detail="表不存在")
    return data


@app.post("/api/admin/adjust-balance")
def admin_adjust_balance(req: AdminAdjustBalanceReq, _: bool = Depends(check_admin)):
    return service.admin_adjust_balance(req.phone, req.delta)


@app.post("/api/admin/table/{table}/{row_id}")
def admin_update_row(table: str, row_id: int, req: AdminUpdateRowReq, _: bool = Depends(check_admin)):
    return service.admin_update_row(table, row_id, req.updates)


@app.post("/api/admin/bili-cookie")
def admin_bili_cookie(req: AdminBiliCookieReq, _: bool = Depends(check_admin)):
    return service.update_bili_cookie(req.cookie)


# ---------- 静态前端（阶段 2 接入）----------
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
