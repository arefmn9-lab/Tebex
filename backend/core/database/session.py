from sqlalchemy.orm import sessionmaker

from .connection import engine

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)