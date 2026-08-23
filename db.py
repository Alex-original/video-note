"""数据库模型与会话管理（PostgreSQL + SQLAlchemy）。

时间字段统一存 Unix 时间戳（float），与 app.py 的 time.time() 保持一致。
"""
import os

from sqlalchemy import Boolean, Column, Float, ForeignKey, Integer, String, Text, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://video_note:video_note@db:5432/video_note",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20), unique=True, nullable=False, index=True)
    balance = Column(Float, nullable=False, default=0.0)  # 余额（元），预留计费
    created_at = Column(Float, nullable=False)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    title = Column(String(500), nullable=False, default="")
    status = Column(String(20), nullable=False, default="running")
    message = Column(Text, nullable=False, default="")
    result_file = Column(String(1000), nullable=False, default="")
    cost = Column(Float, nullable=False, default=0.0)
    created_at = Column(Float, nullable=False)
    updated_at = Column(Float, nullable=False)
    # 缓存去重键：同一 (bvid + 分P + 标签) 的已完成任务即缓存，可复用不重复计费
    bvid = Column(String(20), nullable=True, default="")
    page_key = Column(String(200), nullable=True, default="")
    prompt_hash = Column(String(64), nullable=True, default="")
    # 成本明细 + 失败归类（监控看板用）
    input_tokens = Column(Integer, nullable=True, default=0)
    output_tokens = Column(Integer, nullable=True, default=0)
    asr_seconds = Column(Float, nullable=True, default=0.0)
    fail_reason = Column(String(50), nullable=True, default="")


class Billing(Base):
    __tablename__ = "billing"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False)  # 正=充值，负=消费
    type = Column(String(20), nullable=False)  # recharge / consume
    task_id = Column(Integer, nullable=True)
    created_at = Column(Float, nullable=False)


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False)  # 充值金额（元）
    status = Column(String(20), nullable=False, default="pending")  # pending / paid / expired / failed
    provider = Column(String(20), nullable=False, default="")  # alipay / wechat / manual
    out_trade_no = Column(String(64), nullable=False, unique=True, index=True)  # 商户订单号
    transaction_id = Column(String(64), nullable=False, default="")  # 第三方流水号
    pay_url = Column(String(2000), nullable=False, default="")  # 收银台/二维码链接
    created_at = Column(Float, nullable=False)
    paid_at = Column(Float, nullable=True)
    expire_at = Column(Float, nullable=True)


class SmsCode(Base):
    __tablename__ = "sms_codes"

    id = Column(Integer, primary_key=True)
    phone = Column(String(20), nullable=False, index=True)
    code = Column(String(10), nullable=False)
    expires_at = Column(Float, nullable=False)
    used = Column(Boolean, nullable=False, default=False)


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(Float, nullable=False)
    expires_at = Column(Float, nullable=False)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    type = Column(String(30), nullable=False, index=True)
    created_at = Column(Float, nullable=False)


class Feedback(Base):
    __tablename__ = "feedback"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    category = Column(String(20), nullable=False, default="问题反馈")
    content = Column(Text, nullable=False)
    created_at = Column(Float, nullable=False)


def init_db():
    Base.metadata.create_all(engine)
    # 兼容旧库：create_all 不会给已存在的表加列，这里补缓存去重字段
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS bvid VARCHAR(20) DEFAULT ''"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS page_key VARCHAR(200) DEFAULT ''"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS prompt_hash VARCHAR(64) DEFAULT ''"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS input_tokens INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS output_tokens INTEGER DEFAULT 0"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS asr_seconds DOUBLE PRECISION DEFAULT 0"))
        conn.execute(text("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS fail_reason VARCHAR(50) DEFAULT ''"))


def get_session():
    return SessionLocal()
