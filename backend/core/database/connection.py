from sqlalchemy import create_engine

DATABASE_URL = "postgresql://postgres:postgres@postgres:5432/clinicos"

engine = create_engine(
    DATABASE_URL,
    echo=True
)