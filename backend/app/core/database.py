from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker
import os

# 데이터베이스에 접속하기 위한 주소입니다.
# FastAPI가 멈추지 않고 여러 요청을 동시에 처리할 수 있도록 비동기(asyncpg) 드라이버를 사용합니다.
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://postgres:password@localhost:5432/review_db",
)

# 데이터베이스와 실제 통신을 담당하는 '엔진(Engine)'을 생성합니다.
engine = create_async_engine(DATABASE_URL, echo=False)

# 실제 DB 작업을 수행할 때마다 열고 닫을 '세션(Session)'을 만들어내는 공장(Factory)입니다.
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

# 우리가 만들 데이터베이스 테이블 클래스들이 공통으로 상속받을 기본(Base) 뼈대입니다.
Base = declarative_base()

# FastAPI에서 API 요청이 들어올 때마다 DB 세션을 하나씩 꺼내주고,
# 처리가 끝나면 안전하게 닫아주는 역할을 하는 '의존성 주입(Dependency)' 함수입니다.
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session