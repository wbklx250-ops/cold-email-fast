"""
Pytest configuration and fixtures for backend tests.

Uses SQLite in-memory database for fast, isolated tests.
Includes UUID type handling for SQLite compatibility.
"""

import asyncio
from typing import AsyncGenerator
from uuid import UUID as PyUUID

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB

from app.db.session import get_db_session, get_db_session_with_retry


# SQLite in-memory database URL for tests
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


# Register PostgreSQL type compilers for SQLite before importing models
# This must be done before Base.metadata.create_all()
from sqlalchemy.ext.compiler import compiles

@compiles(PG_UUID, "sqlite")
def compile_uuid_sqlite(element, compiler, **kw):
    """Compile PostgreSQL UUID type to SQLite CHAR(36)."""
    return "CHAR(36)"


@compiles(JSONB, "sqlite")
def compile_jsonb_sqlite(element, compiler, **kw):
    """Compile PostgreSQL JSONB type to SQLite TEXT."""
    return "TEXT"


# Now import Base after the compiler is registered
from app.models.base import Base


@pytest.fixture(scope="session")
def event_loop():
    """Create an event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    """Create a test database engine with UUID support for SQLite."""
    engine = create_async_engine(
        TEST_DATABASE_URL,
        echo=False,
        poolclass=StaticPool,  # Required for SQLite in-memory
        connect_args={"check_same_thread": False},  # SQLite specific
    )
    
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    # Drop all tables after test
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def test_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create a test database session."""
    async_session = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    async with async_session() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(test_engine) -> AsyncGenerator[AsyncClient, None]:
    """
    Create an async test client with database dependency override.
    
    Each test gets a fresh database and client.
    """
    from app.main import app
    
    # Create session factory for this test
    async_session = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    # Override the database dependency
    async def override_get_db():
        async with async_session() as session:
            yield session
    
    app.dependency_overrides[get_db_session] = override_get_db
    app.dependency_overrides[get_db_session_with_retry] = override_get_db
    
    # Create test client with ASGI transport
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    
    # Clear overrides after test
    app.dependency_overrides.clear()
