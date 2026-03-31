from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker

# DB 연결 주소 (계정:비밀번호@호스트:포트/DB이름)
DATABASE_URL = "postgresql+asyncpg://postgres:password@localhost:5432/review_db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

Base = declarative_base()

# API 호출 시마다 DB 세션을 열고 닫아주는 의존성 함수
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session