"""MongoDB-backed API fixtures. Tests only use a database with this exact prefix."""
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient


@pytest.fixture(scope="session")
def mongo_uri() -> str:
    return os.getenv("TEST_MONGO_URI", "mongodb://localhost:27017")


@pytest.fixture
def client(monkeypatch, mongo_uri):
    name = f"hrone_test_{uuid.uuid4().hex}"
    if not name.startswith("hrone_test_"):
        raise RuntimeError("Refusing to use a database outside the test prefix")
    monkeypatch.setenv("MONGO_URI", mongo_uri)
    monkeypatch.setenv("MONGO_DB", name)
    import app.main as api

    api.MONGO_URI = mongo_uri
    api.MONGO_DB = name
    with TestClient(api.app) as test_client:
        yield test_client
    # The database name is generated here and cannot target a user-selected database.
    MongoClient(mongo_uri).drop_database(name)


@pytest.fixture
def db(client, mongo_uri):
    from app.main import db as database

    return database
