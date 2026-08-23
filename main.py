"""FastAPI 应用：REST API + 静态前端（路线 B）。

运行：uvicorn main:app --host 0.0.0.0 --port 7860
静态前端在 static/ 目录（阶段 2 接入）。
"""
import os
import time

from fastapi import Depends, FastAPI, Header
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import db
import service
from service import ServiceError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

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

# 管理看板（Gradio，独立 7861 端口，后台线程运行）
try:
    import threading
    import dashboard
    threading.Thread(target=dashboard.launch, daemon=True).start()
except Exception as e:
    print(f"[dashboard] 启动失败：{e}", flush=True)


# ---------- 异常处理 ----------
@app.exception_handler(ServiceError)
async def handle_service_error(request, exc: ServiceError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


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
    code: str


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


# ---------- 认证 ----------
@app.post("/api/auth/send-code")
def send_code(req: SendCodeReq):
    return {"message": service.send_code(req.phone)}


@app.post("/api/auth/login")
def login(req: LoginReq):
    return service.login(req.phone, req.code)


@app.post("/api/auth/logout")
def logout(authorization: str = Header(default="")):
    token = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    service.delete_session(token)
    return {"ok": True}


# ---------- 标签 ----------
@app.get("/api/presets")
def list_presets(user_id: int = Depends(get_current_user)):
    return service.list_presets()


@app.post("/api/presets/save")
def save_preset(req: SavePresetReq, user_id: int = Depends(get_current_user)):
    return service.save_preset(req.selected, req.name, req.prompt)


@app.post("/api/presets/delete")
def delete_preset(req: DeletePresetReq, user_id: int = Depends(get_current_user)):
    return service.delete_preset(req.name)


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


# ---------- 静态前端（阶段 2 接入）----------
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
