"""数据库模型与会话管理（PostgreSQL + SQLAlchemy）。

时间字段统一存 Unix 时间戳（float），与 app.py 的 time.time() 保持一致。
"""
import os

from sqlalchemy import Column, Float, ForeignKey, Integer, String, Text, create_engine
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


class Billing(Base):
    __tablename__ = "billing"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount = Column(Float, nullable=False)  # 正=充值，负=消费
    type = Column(String(20), nullable=False)  # recharge / consume
    task_id = Column(Integer, nullable=True)
    created_at = Column(Float, nullable=False)


def init_db():
    Base.metadata.create_all(engine)


def get_session():
    return SessionLocal()
