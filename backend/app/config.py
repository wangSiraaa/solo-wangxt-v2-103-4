"""应用配置。

DATABASE_URL 指向 PostgreSQL（生产/演示部署用 docker-compose 启动）。
若未设置（如本地单元测试），自动回退到 backend 目录下的 SQLite 文件，
业务 SQL 均为 SQLAlchemy ORM 方言无关写法，两种库行为一致。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

_database_url = os.environ.get("DATABASE_URL", "").strip()

# 容器镜像内前端静态资源目录可通过 FRONTEND_DIST 覆盖
_frontend_dist = os.environ.get("FRONTEND_DIST", "").strip()

if _database_url:
    if _database_url.startswith("postgres://"):
        # 兼容某些平台下发的 Heroku 风格 URL
        _database_url = _database_url.replace("postgres://", "postgresql+psycopg://", 1)
    elif _database_url.startswith("postgresql://"):
        _database_url = _database_url.replace("postgresql://", "postgresql+psycopg://", 1)
else:
    _database_url = f"sqlite:///{BASE_DIR / 'isolation_demo.db'}"


class Settings:
    database_url: str = _database_url
    frontend_dist: Path = (
        Path(_frontend_dist)
        if _frontend_dist
        else BASE_DIR.parent / "frontend" / "dist" / "frontend" / "browser"
    )
    cors_origins: list[str] = ["http://localhost:4200", "http://127.0.0.1:4200"]


settings = Settings()
