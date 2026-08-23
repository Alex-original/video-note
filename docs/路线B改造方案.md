# 路线 B 改造方案：Gradio → FastAPI + 原生前端

> 制定日期：2026-08-23
> 状态：待执行（按本文件计划持续进行）
> 背景：Gradio 无法还原 UI 稿的视觉标准（自定义 SVG 图标、精细组件、双栏布局），决定重写前端，复用后端业务逻辑。

## 一、目标与原则

1. **复用核心逻辑**：转换、数据、短信、支付、计费全部不动，只重写 UI 层。
2. **前端即 HTML 稿**：用户已审核过的迭代版 HTML 稿（`~/Downloads/video-to-notes 2/index.html`）直接变成真实前端，还原度 95%+。
3. **可回退**：改造期间保留 Gradio 版本，切换失败能回滚。

## 二、架构对比

```
现在：   浏览器 → Gradio(UI + 业务逻辑混合) → DB / 后台线程
改造后： 浏览器 → 原生 HTML/JS 前端 → FastAPI(REST API) → 业务逻辑 → DB / 后台线程
```

## 三、后端改造

### 3.1 完全复用（不改代码）

| 文件 | 内容 |
|---|---|
| `video_to_note.py` | 转换核心（字幕/转写/摘要/汇总）|
| `db.py` | 数据模型（User/Task/Billing/Order/SmsCode）|
| `sms.py` | 短信验证码 |
| `payment.py` | 订单 + 幂等充值 |
| `metrics.py` | API 计数器 |
| `recharge.py` | 管理员充值脚本 |

### 3.2 从 app.py 提取业务逻辑 → 新增 `service.py`

现在 app.py 里「业务逻辑」和「UI」混在一起。把下面这些抽到 `service.py`：

- 任务增删查（`_create_task`/`_update_task`/`_load_tasks`/`_count_running`）
- 计费（`calc_amount`/`_get_balance`/`_charge`）
- 转换编排（`_run_task` 后台线程 + 停止事件）
- 缓存去重（`_find_completed_note`/`_has_running_same`/`_prompt_hash`/`_note_suffix`/`_uniquify_note_path`）
- 登录/充值（`login`/`send_sms_code`/`create_recharge_order`/`simulate_pay`）
- 标签管理（`save_preset`/`delete_preset`/`_lookup_prompt`）

### 3.3 新增

- `main.py`：FastAPI 应用 + 路由 + 静态文件服务
- `db.py` 加一张 `sessions` 表（登录 token）
- 依赖：`fastapi`、`uvicorn[standard]`

### 3.4 API 清单

**认证**

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/send-code` | `{phone}` 发验证码 |
| POST | `/api/auth/login` | `{phone, code}` → `{token, phone}` |
| POST | `/api/auth/logout` | 注销 |

**转换**

| POST | `/api/convert/parse` | `{url}` → 视频信息 + 分P列表（弹窗用）|
| POST | `/api/convert/start` | `{url, pages, preset}` → `{task_id}`（含缓存去重）|
| POST | `/api/task/{id}/stop` | 停止任务 |

**任务 / 余额**

| GET | `/api/tasks` | → `{balance, tasks:[...]}`（前端每 5 秒轮询）|
| GET | `/api/task/{id}/preview` | 笔记预览 |
| GET | `/api/task/{id}/download` | 下载笔记文件 |

**充值**

| POST | `/api/recharge/order` | `{amount}` → `{order_id}` |
| POST | `/api/recharge/simulate` | `{order_id}` 模拟支付到账 |
| POST | `/api/recharge/callback` | 真实支付回调（等商户号）|

**标签**

| GET | `/api/presets` | 标签列表 |
| POST | `/api/presets/save` | 保存/新建/删除 |

**会话**：登录返回随机 token（存 `sessions` 表），前端放 localStorage，请求带 `Authorization: Bearer <token>`。

## 四、前端改造

### 4.1 直接复用 HTML 稿

稿子的 10 个「屏」映射成真实界面：

| 稿子屏幕 | 真实用途 |
|---|---|
| 移动·登录 / 桌面·登录 | **登录页**（响应式）|
| 移动·主页 / 桌面·主页 | **主页面**（topbar + hero + 标签 + 余额 + 任务表）|
| 二次确认弹窗 | 转换确认**弹窗** |
| 充值弹窗 | 充值**弹窗** |
| 空状态 / 余额不足 | 页面的**状态**（非独立页）|
| 标签管理 | 标签管理弹窗/视图 |

### 4.2 加 JS 接线（原生 JS，无构建）

- 登录：发码 → 登录 → 存 token
- 主页面：填链接 → parse → 弹确认 → start → 每 5 秒轮询 `/api/tasks`
- 充值：下单 → 模拟支付 → 刷新余额
- 下载/预览：调下载/预览接口

技术选型：**原生 HTML/CSS/JS**（稿子已是原生，零构建，最省事）；如后续状态复杂再升级 Vue（CDN 引入，不用构建）。

## 五、部署改造

- `Dockerfile`：`CMD` 从 `python app.py` 改为 `uvicorn main:app --host 0.0.0.0 --port 7860`
- `requirements.txt`：加 `fastapi`、`uvicorn[standard]`
- 静态文件（前端 HTML/CSS/JS）放 `static/` 目录，FastAPI 直接服务
- `dashboard.py`（管理看板）**暂保留 Gradio**，跑在 7861，不影响主应用

## 六、分阶段实施

1. **阶段 1**：新建 `service.py`（抽业务逻辑）+ `main.py`（FastAPI 全部接口）+ `sessions` 表。先让 API 全部跑通，用 curl/脚本自测。
2. **阶段 2**：前端接线（HTML 稿 + JS 调 API），联调登录→转换→下载全流程。
3. **阶段 3**：部署切换（Dockerfile 改 uvicorn），保留旧 Gradio 容器做回退。
4. **阶段 4**：清理（删 app.py 的 Gradio 部分，或归档）。

## 七、风险与注意

1. **安全**：标题/消息要 HTML 转义（防 XSS）；下载接口要校验 `user_id` 归属（防越权）；支付回调要验签。
2. **会话**：token 设过期时间，logout 删除。
3. **并发**：沿用现有 `threading.Thread` + `_stop_events`，模型不变。
4. **工作量**：阶段 1（后端）约 1 天，阶段 2（前端接线）约 1-2 天，主要是体力活，无技术风险。

## 八、当前进度

- [x] 阶段 1：FastAPI 后端 + service.py（已部署验证：登录/任务/标签/解析/充值接口均通过）
- [x] 阶段 2：前端接线（static/ 三件套 style.css/index.html/app.js，已联调登录→转换弹窗→费用预估通过）
- [x] 阶段 3：部署切换（Dockerfile CMD 改 uvicorn，7860 已上线前端，7861 看板保留，端到端验证通过）
- [x] 阶段 4：清理（删除 app.py Gradio 版，dashboard.py 保留跑 7861）
