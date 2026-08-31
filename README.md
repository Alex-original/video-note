# 视频转笔记（Video Note）

把 B 站视频一键转成结构化 Markdown 笔记。粘贴视频链接，自动下载字幕（或转写音频），再由大模型分块摘要、汇总成一篇可读的笔记。

## ✨ 功能特性

- **链接解析**：支持 B 站完整链接、短链（b23.tv）、纯 BV 号、以及「【标题】+ 链接」分享文本
- **多分 P 识别**：解析出视频标题、UP 主、时长、分 P 列表，可勾选要转换的分 P
- **字幕优先**：用 yt-dlp 下载 B 站 AI 中文字幕（`ai-zh`），命中则跳过转写，又快又省
- **无字幕回退**：下载音频 → 云端转写（阿里云百炼 Qwen-ASR）→ 本地 Whisper（faster-whisper）兜底
- **分块摘要（map-reduce）**：转录文本按 3500 字分块，逐块摘要，再汇总成一篇 Markdown 笔记
- **自定义输出标签**：内置多套模板（金融分析 / 学术速览 / 观点提取 / 故事梗概等），可自定义
- **缓存去重**：同一视频（相同分 P + 标签）重复转换直接复用，不重复调 API、不重复扣费
- **手机号 + 短信验证码登录**：阿里云短信，验证码 5 分钟有效、60 秒频控、防重放
- **计费系统**：0.6 元 / 15 分钟（向上取整），转换前余额预检，完成后扣费，失败/取消不扣费
- **支付宝支付**：手机网站 + 电脑网站双端支付，按设备自动切换，支持退款
- **付费视频检测**：识别充电/付费视频的「试看片段」，提示用户确认后再转换
- **监控看板**：数据看板（任务/消费/成本/系统指标）+ 管理工具（余额调整、白名单/黑名单、退款、Cookie 管理）

## 🏗 架构

```
浏览器 → 原生 HTML/JS 前端 → FastAPI (REST API) → 业务逻辑 → DB / 后台线程
```

任务执行采用「后台线程 + 任务表轮询」的解耦架构：前端提交转换后，后台线程独立执行，进度实时写入数据库，前端定时轮询展示。任务执行不依赖前端连接存活性——手机锁屏、刷新、断网都不会中断任务。

## 🛠 技术栈

| 组件 | 选型 |
|---|---|
| 后端框架 | FastAPI + uvicorn |
| 前端 | 原生 HTML/CSS/JS + marked.js + mermaid.js（零构建） |
| 数据库 | PostgreSQL + SQLAlchemy |
| 字幕/音频下载 | yt-dlp + ffmpeg |
| 云端转写 | 阿里云百炼 Qwen-ASR（qwen3-asr-flash） |
| 本地转写 | faster-whisper（medium，CPU int8） |
| 文本摘要 | DeepSeek（deepseek-v4-flash） |
| 支付 | 支付宝（手机网站 + 电脑网站） |
| 短信 | 阿里云短信 |
| 部署 | Docker + docker-compose |

## 📁 目录结构

```
video-note/
├── main.py            # FastAPI 应用：路由 + 鉴权 + 静态文件
├── service.py         # 业务逻辑层：任务/计费/登录/充值/退款
├── video_to_note.py   # 转换核心：字幕/转写/摘要/汇总
├── payment.py         # 支付宝支付 + 退款 + 验签
├── sms.py             # 短信验证码（发送 + 校验）
├── db.py              # 数据库模型（SQLAlchemy）
├── stats.py           # 监控看板数据聚合
├── metrics.py         # API 计数器
├── recharge.py        # 管理员充值脚本
├── static/            # 前端（主应用 + 管理看板）
├── docs/              # 文档（用户协议、隐私政策、接入流程等）
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example       # 环境变量模板
```

## 🚀 快速开始（Docker 部署）

### 1. 克隆仓库

```bash
git clone https://github.com/Alex-original/video-note.git
cd video-note
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入真实值。**最小配置只需要 `OPENAI_API_KEY`**（DeepSeek 摘要），其余可选——不填时转写回退本地 Whisper、支付走模拟兜底、验证码打印到服务端日志。

### 3. 启动

```bash
docker compose up -d --build
```

首次启动会自动构建镜像（约 1~2 分钟）并拉起 PostgreSQL + 应用两个容器。

### 4. 访问

- 主应用：http://localhost:7860
- 管理看板：http://localhost:7860/admin/

## 🔧 环境变量

| 变量 | 必填 | 说明 |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | DeepSeek 摘要模型 key（兼容 OpenAI 格式） |
| `DASHSCOPE_API_KEY` | 可选 | 阿里云百炼转写 key；不填则回退本地 Whisper |
| `DASHSCOPE_BASE_URL` | 可选 | 百炼工作空间端点 |
| `ALIYUN_ACCESS_KEY_ID` | 可选 | 阿里云短信 AccessKeyId；不填则验证码打日志 |
| `ALIYUN_ACCESS_KEY_SECRET` | 可选 | 阿里云短信 AccessKeySecret |
| `SMS_SIGN_NAME` | 可选 | 短信签名 |
| `SMS_TEMPLATE_CODE` | 可选 | 短信模板 Code |
| `ALIPAY_APP_ID` | 可选 | 支付宝 AppID；三项都填才走真实支付 |
| `ALIPAY_PRIVATE_KEY` | 可选 | 支付宝应用私钥（PKCS8） |
| `ALIPAY_PUBLIC_KEY` | 可选 | 支付宝公钥（用于回调验签） |
| `ALIPAY_NOTIFY_URL` | 可选 | 支付宝异步回调地址 |
| `ADMIN_PASSWORD` | 可选 | 管理看板密码（默认 `admin123`） |
| `POSTGRES_PASSWORD` | 可选 | 数据库密码（默认 `video_note`） |
| `RECHARGE_WHITELIST` | 可选 | 充值白名单（逗号分隔手机号；空 = 全员开放） |
| `BILI_COOKIES_FILE` | 可选 | B 站 cookies 文件路径（服务器场景，用于下载需登录的字幕/音频） |

## 📖 使用说明

1. 手机号 + 验证码登录（新用户赠送 1.8 元体验额度）
2. 粘贴 B 站视频链接 → 点「开始转换」
3. 弹窗确认分 P 和费用 → 确认后开始转换
4. 转换完成 → 预览 / 下载 Markdown 笔记

## ⚠️ 免责声明

- 本项目仅供学习和技术交流使用，笔记内容由 AI 自动生成，**不构成投资建议**。
- 使用本项目处理 B 站内容时，请遵守 B 站服务条款及相关版权规定，仅限个人学习用途。
- 请勿将本项目用于任何侵犯他人权益的用途。

## 📄 许可证

[MIT License](LICENSE)
