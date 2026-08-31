# Video Note

Turn Bilibili videos into structured Markdown notes in one click. Paste a video link, and the tool downloads subtitles (or transcribes the audio), then uses an LLM to summarize in chunks and produce a readable note.

## ✨ Features

- **Link parsing**: supports full Bilibili links, short links (b23.tv), bare BV IDs, and share text in the format `【Title】+ link`
- **Multi-part recognition**: extracts title, uploader, duration, and the list of parts; select which parts to convert
- **Subtitle-first**: downloads Bilibili's AI Chinese subtitles (`ai-zh`) via yt-dlp, skipping transcription when available (faster and cheaper)
- **Transcription fallback**: downloads audio → cloud ASR (Alibaba Cloud Qwen-ASR) → local Whisper (faster-whisper) as last resort
- **Chunked summarization (map-reduce)**: splits the transcript into 3500-character chunks, summarizes each, then merges into one Markdown note
- **Custom output presets**: built-in templates (financial analysis / academic overview / key points / story summary, etc.) plus custom presets
- **Caching & deduplication**: re-converting the same video (same parts + preset) reuses the existing note without re-calling APIs or re-charging
- **Phone + SMS verification login**: Alibaba Cloud SMS, 5-minute expiry, 60-second rate limit, replay-proof
- **Billing**: ¥0.6 / 15 minutes (rounded up); balance checked before conversion, charged on completion; no charge on failure/cancel
- **Alipay payments**: mobile web + desktop web dual-channel, auto-selected by device, with refund support
- **Paid-video detection**: detects "preview-only" clips of paid/membership videos and prompts the user to confirm before converting
- **Admin dashboard**: metrics (tasks/revenue/cost/system) + management tools (balance adjustment, allowlist/blocklist, refunds, cookie management)

## 🏗 Architecture

```
Browser → vanilla HTML/JS frontend → FastAPI (REST API) → business logic → DB / background threads
```

Tasks run in background threads decoupled from the frontend: the frontend submits a task, a background thread executes it independently, writes progress to the database, and the frontend polls periodically. Task execution does not depend on the client connection — locking the screen, refreshing, or losing the network does not interrupt the task.

## 🛠 Tech Stack

| Component | Choice |
|---|---|
| Backend | FastAPI + uvicorn |
| Frontend | Vanilla HTML/CSS/JS + marked.js + mermaid.js (zero build) |
| Database | PostgreSQL + SQLAlchemy |
| Subtitle/audio download | yt-dlp + ffmpeg |
| Cloud ASR | Alibaba Cloud Qwen-ASR (qwen3-asr-flash) |
| Local ASR | faster-whisper (medium, CPU int8) |
| Summarization | DeepSeek (deepseek-v4-flash) |
| Payments | Alipay (mobile web + desktop web) |
| SMS | Alibaba Cloud SMS |
| Deployment | Docker + docker-compose |

## 📁 Directory Structure

```
video-note/
├── main.py            # FastAPI app: routes + auth + static files
├── service.py         # Business logic: tasks/billing/login/recharge/refund
├── video_to_note.py   # Conversion core: subtitle/transcription/summarization
├── payment.py         # Alipay payment + refund + signature verification
├── sms.py             # SMS verification code (send + verify)
├── db.py              # Database models (SQLAlchemy)
├── stats.py           # Admin dashboard data aggregation
├── metrics.py         # API counter
├── recharge.py        # Admin recharge script
├── static/            # Frontend (main app + admin dashboard)
├── docs/              # Legal docs (terms of service, privacy policy)
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
└── .env.example       # Environment variable template
```

## 🚀 Quick Start (Docker)

### 1. Clone the repo

```bash
git clone https://github.com/Alex-original/video-note.git
cd video-note
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` and fill in real values. **The minimal config only needs `OPENAI_API_KEY`** (DeepSeek summarization). Everything else is optional — without them, transcription falls back to local Whisper, payments use a mock fallback, and verification codes are printed to server logs.

### 3. Start

```bash
docker compose up -d --build
```

The first start builds the image (~1–2 min) and brings up two containers: PostgreSQL and the app.

### 4. Access

- Main app: http://localhost:7860
- Admin dashboard: http://localhost:7860/admin/

## 🔧 Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OPENAI_API_KEY` | ✅ | DeepSeek summarization key (OpenAI-compatible format) |
| `DASHSCOPE_API_KEY` | Optional | Alibaba Cloud ASR key; falls back to local Whisper if unset |
| `DASHSCOPE_BASE_URL` | Optional | DashScope workspace endpoint |
| `ALIYUN_ACCESS_KEY_ID` | Optional | Alibaba Cloud SMS AccessKeyId; codes logged if unset |
| `ALIYUN_ACCESS_KEY_SECRET` | Optional | Alibaba Cloud SMS AccessKeySecret |
| `SMS_SIGN_NAME` | Optional | SMS signature |
| `SMS_TEMPLATE_CODE` | Optional | SMS template code |
| `ALIPAY_APP_ID` | Optional | Alipay AppID; real payments only when all three are set |
| `ALIPAY_PRIVATE_KEY` | Optional | Alipay app private key (PKCS8) |
| `ALIPAY_PUBLIC_KEY` | Optional | Alipay public key (for callback verification) |
| `ALIPAY_NOTIFY_URL` | Optional | Alipay async callback URL |
| `ADMIN_PASSWORD` | Optional | Admin dashboard password (default `admin123`) |
| `POSTGRES_PASSWORD` | Optional | Database password (default `video_note`) |
| `RECHARGE_WHITELIST` | Optional | Recharge allowlist (comma-separated phones; empty = open to all) |
| `BILI_COOKIES_FILE` | Optional | Bilibili cookies file path (server-side, for subtitles/audio requiring login) |

## 📖 Usage

1. Log in with phone number + verification code (new users get ¥1.8 trial credit)
2. Paste a Bilibili video link → click "Start conversion"
3. Confirm parts and cost in the dialog → conversion starts
4. When done → preview / download the Markdown note

## ⚠️ Disclaimer

- This project is for learning and technical exchange only. Notes are AI-generated and do **not** constitute investment advice.
- When processing Bilibili content, comply with Bilibili's terms of service and relevant copyright rules, for personal learning use only.
- Do not use this project for any purpose that infringes upon the rights of others.

## 📄 License

[MIT License](LICENSE)

---

*[中文文档](README.md)*
