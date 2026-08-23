FROM python:3.13-slim

# 用阿里云 Debian 源加速 apt（国内直连 deb.debian.org 超慢）
RUN sed -i 's/deb.debian.org/mirrors.aliyun.com/g' /etc/apt/sources.list.d/*.sources /etc/apt/sources.list 2>/dev/null; \
    apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 缓存层）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制代码
COPY video_to_note.py db.py recharge.py payment.py sms.py dashboard.py metrics.py service.py main.py ./
COPY docs/ ./docs/
COPY static/ ./static/

# 数据目录（docker-compose 挂载卷）
ENV DATA_DIR=/data

EXPOSE 7860 7861

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
