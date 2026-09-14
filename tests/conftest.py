from datetime import date

import pytest

from app import create_app
from app.extensions import db as _db
from app.models import (
    User,
    UserRole,
    Contributor,
    BankAccount,
    FundType,
    CHIKUMI_100_NAME,
)
from app.services.settings import set_go_live_date, ensure_defaults


@pytest.fixture()
def app():
    application = create_app("testing")
    with application.app_context():
        _db.create_all()
        ensure_defaults()
        set_go_live_date(date(2026, 9, 18))
        _db.session.add(BankAccount(name="Mukululo Bank Account", fund_type=FundType.MUKULULO))
        _db.session.add(BankAccount(name="Friday & Sunday Bank Account", fund_type=FundType.FRIDAY_SUNDAY))
        _db.session.add(Contributor(name=CHIKUMI_100_NAME, name_normalized=Contributor.normalize(CHIKUMI_100_NAME)))
        _db.session.commit()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture()
def db(app):
    return _db


@pytest.fixture()
def client(app):
    return app.test_client()


def make_user(db, username, role, password="password123"):
    user = User(username=username, display_name=username, role=role, active=True)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture()
def admin_user(db):
    return make_user(db, "admin1", UserRole.ADMIN)


@pytest.fixture()
def data_entry_user(db):
    return make_user(db, "dataentry1", UserRole.DATA_ENTRY)


@pytest.fixture()
def collector_user(db):
    return make_user(db, "collector1", UserRole.COLLECTOR)


@pytest.fixture()
def viewer_user(db):
    return make_user(db, "viewer1", UserRole.VIEWER)


def login(client, username, password="password123"):
    return client.post("/auth/login", data={"username": username, "password": password}, follow_redirects=True)
