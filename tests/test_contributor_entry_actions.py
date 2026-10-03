"""Edit / Delete actions on contributor entries, role-based reason rules, and
the safeguards that must still hold. All names and amounts are synthetic."""
from datetime import date

import pytest

from app.models import (
    AuditAction,
    AuditLog,
    BankAccount,
    BankDeposit,
    CollectionSession,
    CollectionType,
    ContributionTransaction,
    Contributor,
    FundType,
    HistoricalOfficialMonthlyTotal,
    TransactionStatus,
    UserRole,
)
from app.services.reports import contributor_report_rows
from app.services.totals import contributor_detail_total
from tests.conftest import login, make_user

DAY = date(2026, 10, 1)
EDIT_MARKER = 'aria-label="Edit entry"'
DELETE_MARKER = 'id="deleteEntryModal"'


@pytest.fixture()
def admin(db, admin_user):
    return admin_user


@pytest.fixture()
def ann(db, admin):
    c = Contributor(name="ANN SYNTHETIC", name_normalized="ANN SYNTHETIC", active=True, created_by_id=admin.id)
    db.session.add(c)
    db.session.commit()
    return c


@pytest.fixture()
def friday_txn(db, admin, ann):
    t = ContributionTransaction(date=DAY, collection_type=CollectionType.FRIDAY, contributor_id=ann.id,
                                amount=2000, created_by_id=admin.id)
    db.session.add(t)
    db.session.commit()
    return t


def _profile(client, ann):
    resp = client.get(f"/contributors/{ann.id}?year=2026")
    assert resp.status_code == 200
    return resp.data.decode()


def _edit_data(**overrides):
    data = {
        "contributor_name": "ANN SYNTHETIC",
        "date": DAY.isoformat(),
        "collection_type": "MUKULULO",
        "amount": "2000",
        "note": "",
        "reason": "",
    }
    data.update(overrides)
    return data


def _delete_data(**overrides):
    data = {"reason": "", "confirm": "y"}
    data.update(overrides)
    return data


# 1-5: visibility by role --------------------------------------------------

def test_admin_sees_edit_on_contributor_entries(client, admin, ann, friday_txn):
    login(client, "admin1")
    html = _profile(client, ann)
    assert EDIT_MARKER in html
    assert f"/collections/transactions/{friday_txn.id}/edit" in html


def test_admin_sees_delete_on_contributor_entries(client, admin, ann, friday_txn):
    login(client, "admin1")
    html = _profile(client, ann)
    assert DELETE_MARKER in html
    assert f'data-delete-url="/collections/transactions/{friday_txn.id}/void"' in html


def test_viewer_sees_no_edit_or_delete(client, db, ann, friday_txn):
    make_user(db, "viewer2", UserRole.VIEWER)
    login(client, "viewer2")
    html = _profile(client, ann)
    assert "ANN SYNTHETIC" in html
    assert EDIT_MARKER not in html and DELETE_MARKER not in html


def test_collector_follows_existing_permissions_no_actions(client, db, ann, friday_txn):
    make_user(db, "collector2", UserRole.COLLECTOR)
    login(client, "collector2")
    html = _profile(client, ann)
    assert EDIT_MARKER not in html and DELETE_MARKER not in html
    assert client.post(f"/collections/transactions/{friday_txn.id}/edit", data=_edit_data(reason="x")).status_code == 403
    assert client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(reason="x")).status_code == 403


def test_data_entry_follows_existing_edit_permission(client, db, ann, friday_txn):
    make_user(db, "dataentry2", UserRole.DATA_ENTRY)
    login(client, "dataentry2")
    html = _profile(client, ann)
    assert EDIT_MARKER in html and DELETE_MARKER in html


# 6-11: edit/delete with reason rules --------------------------------------

def test_admin_edits_with_blank_reason(client, db, admin, friday_txn):
    login(client, "admin1")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/edit", data=_edit_data(), follow_redirects=True)
    assert b"Entry corrected" in resp.data
    db.session.refresh(friday_txn)
    assert friday_txn.collection_type == CollectionType.MUKULULO


def test_admin_edits_with_reason(client, db, admin, friday_txn):
    login(client, "admin1")
    client.post(f"/collections/transactions/{friday_txn.id}/edit", data=_edit_data(reason="Wrong category"))
    entry = AuditLog.query.filter_by(entity_type="ContributionTransaction", entity_id=friday_txn.id,
                                     action=AuditAction.EDIT).one()
    assert entry.reason == "Wrong category"


def test_data_entry_edit_with_blank_reason_is_rejected(client, db, friday_txn):
    make_user(db, "dataentry3", UserRole.DATA_ENTRY)
    login(client, "dataentry3")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/edit", data=_edit_data(), follow_redirects=True)
    assert b"A reason is required for this change." in resp.data
    db.session.refresh(friday_txn)
    assert friday_txn.collection_type == CollectionType.FRIDAY
    assert AuditLog.query.filter_by(entity_type="ContributionTransaction", entity_id=friday_txn.id,
                                    action=AuditAction.EDIT).count() == 0


def test_admin_deletes_with_blank_reason(client, db, admin, friday_txn):
    login(client, "admin1")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(), follow_redirects=True)
    assert b"Entry deleted" in resp.data
    db.session.refresh(friday_txn)
    assert friday_txn.status == TransactionStatus.VOID
    assert friday_txn.void_reason is None


def test_admin_deletes_with_reason(client, db, admin, friday_txn):
    login(client, "admin1")
    client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(reason="Duplicate entry"))
    db.session.refresh(friday_txn)
    assert friday_txn.void_reason == "Duplicate entry"


def test_data_entry_delete_with_blank_reason_is_rejected(client, db, friday_txn):
    make_user(db, "dataentry4", UserRole.DATA_ENTRY)
    login(client, "dataentry4")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(), follow_redirects=True)
    assert b"A reason is required for this change." in resp.data
    db.session.refresh(friday_txn)
    assert friday_txn.status == TransactionStatus.ACTIVE


def test_reason_rule_follows_role_not_username(client, db, friday_txn):
    # An account named "admin" that is NOT an Admin must still need a reason.
    make_user(db, "admin_like", UserRole.DATA_ENTRY)
    login(client, "admin_like")
    client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data())
    db.session.refresh(friday_txn)
    assert friday_txn.status == TransactionStatus.ACTIVE


def test_short_reason_is_rejected_for_everyone(client, db, admin, friday_txn):
    login(client, "admin1")
    client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(reason="no"))
    db.session.refresh(friday_txn)
    assert friday_txn.status == TransactionStatus.ACTIVE


# 12-13: soft void -----------------------------------------------------------

def test_delete_is_soft_void_not_physical_delete(client, db, admin, friday_txn):
    login(client, "admin1")
    txn_id = friday_txn.id
    client.post(f"/collections/transactions/{txn_id}/void", data=_delete_data())
    row = db.session.get(ContributionTransaction, txn_id)
    assert row is not None
    assert row.status == TransactionStatus.VOID
    assert row.amount == 2000 and row.collection_type == CollectionType.FRIDAY


def test_voided_entry_excluded_from_totals(client, db, admin, ann, friday_txn):
    before = contributor_detail_total(None, DAY, DAY, ann.id)
    login(client, "admin1")
    client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(reason="Entered twice"))
    assert before == 2000
    assert contributor_detail_total(None, DAY, DAY, ann.id) == 0
    _, totals = contributor_report_rows(DAY, DAY)
    assert totals["GRAND"] == 0


def test_voided_entry_remains_visible_on_profile_marked_voided(client, db, admin, ann, friday_txn):
    login(client, "admin1")
    client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(reason="Entered twice"))
    html = _profile(client, ann)
    assert "VOIDED" in html
    assert f"/collections/transactions/{friday_txn.id}/void" not in html  # no actions on voided rows
    assert f"/collections/transactions/{friday_txn.id}/edit" not in html


def test_voided_entry_cannot_be_edited_directly(client, db, admin, friday_txn):
    login(client, "admin1")
    client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(reason="Entered twice"))
    resp = client.post(f"/collections/transactions/{friday_txn.id}/edit", data=_edit_data(), follow_redirects=True)
    assert b"Deleted entries cannot be edited" in resp.data


# 14-15: audit -----------------------------------------------------------------

def test_admin_edit_audit_records_before_after_with_blank_reason(client, db, admin, friday_txn):
    login(client, "admin1")
    client.post(f"/collections/transactions/{friday_txn.id}/edit", data=_edit_data(amount="2500"))
    entry = AuditLog.query.filter_by(entity_type="ContributionTransaction", entity_id=friday_txn.id,
                                     action=AuditAction.EDIT).one()
    assert entry.reason is None
    assert entry.user_id == admin.id
    assert entry.timestamp is not None
    assert '"collection_type": "FRIDAY"' in entry.before_json and '"amount": 2000' in entry.before_json
    assert '"collection_type": "MUKULULO"' in entry.after_json and '"amount": 2500' in entry.after_json


def test_admin_delete_audit_recorded_with_blank_reason(client, db, admin, friday_txn):
    login(client, "admin1")
    client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data())
    entry = AuditLog.query.filter_by(entity_type="ContributionTransaction", entity_id=friday_txn.id,
                                     action=AuditAction.VOID).one()
    assert entry.reason is None
    assert entry.user_id == admin.id
    assert '"contributor": "ANN SYNTHETIC"' in entry.before_json
    assert '"amount": 2000' in entry.before_json
    assert '"status": "VOID"' in entry.after_json


# 16-18: existing safety guards ------------------------------------------------

def test_banked_guard_still_blocks_delete(client, db, admin, friday_txn):
    bank = BankAccount.query.filter_by(fund_type=FundType.FRIDAY_SUNDAY).first()
    db.session.add(BankDeposit(deposit_date=DAY, fund=FundType.FRIDAY_SUNDAY, bank_account_id=bank.id,
                               amount=2000, created_by_id=admin.id))
    db.session.commit()
    login(client, "admin1")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(), follow_redirects=True)
    assert b"below the amount already banked" in resp.data
    db.session.refresh(friday_txn)
    assert friday_txn.status == TransactionStatus.ACTIVE


def test_banked_guard_still_blocks_admin_edit_that_moves_banked_money(client, db, admin, friday_txn):
    bank = BankAccount.query.filter_by(fund_type=FundType.FRIDAY_SUNDAY).first()
    db.session.add(BankDeposit(deposit_date=DAY, fund=FundType.FRIDAY_SUNDAY, bank_account_id=bank.id,
                               amount=2000, created_by_id=admin.id))
    db.session.commit()
    login(client, "admin1")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/edit", data=_edit_data(), follow_redirects=True)
    assert b"below the amount already banked" in resp.data
    db.session.refresh(friday_txn)
    assert friday_txn.collection_type == CollectionType.FRIDAY


def test_locked_period_guard_still_blocks_delete(client, db, admin, friday_txn):
    db.session.add(HistoricalOfficialMonthlyTotal(year=2026, month=10, mukululo_total=0, friday_total=0,
                                                  sunday_total=0, locked=True, created_by_id=admin.id))
    db.session.commit()
    login(client, "admin1")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data(), follow_redirects=True)
    assert b"locked as an official historical total" in resp.data
    db.session.refresh(friday_txn)
    assert friday_txn.status == TransactionStatus.ACTIVE


def test_open_daily_close_recalculated_and_count_preserved(client, db, admin, friday_txn):
    login(client, "admin1")
    client.post(f"/collections/session?date={DAY.isoformat()}",
                data={"physical_cash_counted": "2000", "reconciliation_date": DAY.isoformat()})
    client.post(f"/collections/transactions/{friday_txn.id}/void", data=_delete_data())
    row = CollectionSession.query.filter_by(date=DAY).one()
    assert row.physical_cash_counted == 2000
    assert row.friday_system_total == 0
    assert row.difference == 2000 and row.status.value == "OVER"


def test_return_target_cannot_redirect_off_site(client, db, admin, friday_txn):
    login(client, "admin1")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/void",
                       data=_delete_data(return_to="https://example.invalid/evil"), follow_redirects=False)
    assert resp.status_code == 302
    assert "example.invalid" not in resp.headers["Location"]


def test_profile_return_target_is_used_after_delete(client, db, admin, ann, friday_txn):
    login(client, "admin1")
    resp = client.post(f"/collections/transactions/{friday_txn.id}/void",
                       data=_delete_data(reason="Entered twice", return_to=f"/contributors/{ann.id}?year=2026"),
                       follow_redirects=False)
    assert resp.headers["Location"].endswith(f"/contributors/{ann.id}?year=2026")


# 19-20: layout structure (real browser checks live in the Playwright run) ----

def test_desktop_profile_has_actions_column_for_admin(client, admin, ann, friday_txn):
    login(client, "admin1")
    html = _profile(client, ann)
    assert '<th class="text-end">Actions</th>' in html


def test_mobile_cards_have_touch_sized_actions_for_admin(client, admin, ann, friday_txn):
    login(client, "admin1")
    html = _profile(client, ann)
    assert 'class="btn btn-outline-primary flex-fill py-2"' in html
    assert 'class="btn btn-outline-danger flex-fill py-2"' in html
