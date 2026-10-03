"""Exact Date filters, filtered grand totals, combined report, Daily Close date
synchronisation and contribution correction (edit / delete) safeguards.

All data here is synthetic.
"""
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
from app.services.report_filters import range_from_args
from app.services.reports import contributor_report_rows, daily_category_rows
from tests.conftest import login, make_user

DAY_A = date(2026, 10, 1)
DAY_B = date(2026, 10, 2)
DAY_EMPTY = date(2026, 10, 9)


def _contributor(db, name):
    c = Contributor(name=name, name_normalized=Contributor.normalize(name), active=True)
    db.session.add(c)
    db.session.commit()
    return c


def _txn(db, admin, contributor, day, ctype, amount, status=TransactionStatus.ACTIVE, note=None):
    t = ContributionTransaction(
        date=day, collection_type=ctype, contributor_id=contributor.id, amount=amount,
        status=status, created_by_id=admin.id, note=note,
    )
    db.session.add(t)
    db.session.commit()
    return t


@pytest.fixture()
def admin(db, admin_user):
    return admin_user


@pytest.fixture()
def seeded(db, admin):
    """Two contributors, two days, all three categories."""
    ann = _contributor(db, "ANN SYNTHETIC")
    ben = _contributor(db, "BEN SYNTHETIC")
    _txn(db, admin, ann, DAY_A, CollectionType.MUKULULO, 1000)
    _txn(db, admin, ann, DAY_A, CollectionType.FRIDAY, 2000)
    _txn(db, admin, ben, DAY_A, CollectionType.SUNDAY, 4000)
    _txn(db, admin, ben, DAY_B, CollectionType.MUKULULO, 8000)
    return {"ann": ann, "ben": ben}


# ---------------------------------------------------------------------------
# Shared filter semantics
# ---------------------------------------------------------------------------

class TestRangeFromArgs:
    def test_exact_date_overrides_from_and_to(self):
        rng = range_from_args({"exact_date": "2026-10-01", "date_from": "2026-01-01", "date_to": "2026-12-31"}, today=date(2026, 10, 3))
        assert rng.start == rng.end == date(2026, 10, 1)
        assert rng.is_exact

    def test_invalid_exact_date_falls_back_to_from_and_to(self):
        rng = range_from_args({"exact_date": "31/02/2026", "date_from": "2026-10-01", "date_to": "2026-10-05"})
        assert (rng.start, rng.end, rng.is_exact) == (date(2026, 10, 1), date(2026, 10, 5), False)

    def test_defaults_to_this_year_through_today(self):
        rng = range_from_args({}, today=date(2026, 10, 3))
        assert (rng.start, rng.end) == (date(2026, 1, 1), date(2026, 10, 3))

    def test_query_args_round_trip_for_links_and_exports(self):
        rng = range_from_args({"exact_date": "2026-10-01"})
        assert rng.query_args() == {"exact_date": "2026-10-01"}
        rng = range_from_args({"date_from": "2026-10-01", "date_to": "2026-10-02"})
        assert rng.query_args() == {"date_from": "2026-10-01", "date_to": "2026-10-02"}


# ---------------------------------------------------------------------------
# Contributor report: Exact Date, filtered grand totals
# ---------------------------------------------------------------------------

class TestContributorReportFilters:
    def test_from_to_range_still_works(self, client, admin, seeded):
        login(client, "admin1")
        resp = client.get("/reports/contributor?date_from=2026-10-01&date_to=2026-10-01")
        assert resp.status_code == 200
        assert b"ANN SYNTHETIC" in resp.data and b"BEN SYNTHETIC" in resp.data

    def test_exact_date_returns_only_that_day(self, client, admin, seeded):
        login(client, "admin1")
        resp = client.get("/reports/contributor?exact_date=2026-10-02")
        html = resp.data.decode()
        assert "BEN SYNTHETIC" in html
        assert "ANN SYNTHETIC" not in html  # ANN only has DAY_A entries

    def test_exact_date_wins_over_conflicting_from_to(self, client, admin, seeded):
        login(client, "admin1")
        resp = client.get("/reports/contributor?exact_date=2026-10-02&date_from=2026-10-01&date_to=2026-10-01")
        assert "ANN SYNTHETIC" not in resp.data.decode()
        assert "BEN SYNTHETIC" in resp.data.decode()

    def test_empty_day_shows_empty_state_and_zero_totals(self, client, admin, seeded):
        login(client, "admin1")
        html = client.get(f"/reports/contributor?exact_date={DAY_EMPTY.isoformat()}").data.decode()
        assert "No contributions in this range." in html
        assert "UGX 0" in html

    def test_csv_export_preserves_exact_date_and_adds_grand_total_row(self, client, admin, seeded):
        login(client, "admin1")
        html = client.get("/reports/contributor?exact_date=2026-10-01").data.decode()
        assert "exact_date=2026-10-01" in html  # export link carries the filter
        csv = client.get("/reports/contributor?exact_date=2026-10-01&format=csv").data.decode()
        lines = [line for line in csv.splitlines() if line]
        assert lines[0] == "Contributor,Mukululo,Friday,Sunday,Total"
        assert lines[-1] == "GRAND TOTAL,1000,2000,4000,7000"
        assert "BEN SYNTHETIC,0,0,4000,4000" in csv
        assert "8000" not in csv  # BEN's DAY_B Mukululo entry is outside the exact date
        assert "ANN SYNTHETIC,1000,2000,0,3000" in csv

    def test_grand_totals_are_per_category_and_cover_whole_result(self, db, admin):
        # 30 contributors: more than any page size, so totals must not be page-limited.
        contributors = [_contributor(db, f"SYN CONTRIB {i:02d}") for i in range(30)]
        for c in contributors:
            _txn(db, admin, c, DAY_A, CollectionType.MUKULULO, 100)
            _txn(db, admin, c, DAY_A, CollectionType.FRIDAY, 10)
        rows, totals = contributor_report_rows(DAY_A, DAY_A)
        assert len(rows) == 30
        assert totals == {"MUKULULO": 3000, "FRIDAY": 300, "SUNDAY": 0, "GRAND": 3300}
        assert sum(r["total"] for r in rows) == totals["GRAND"]

    def test_voided_entries_do_not_contribute(self, db, admin, seeded):
        ben = seeded["ben"]
        _txn(db, admin, ben, DAY_A, CollectionType.MUKULULO, 5000, status=TransactionStatus.VOID)
        rows, totals = contributor_report_rows(DAY_A, DAY_A)
        assert totals["GRAND"] == 7000

    def test_edited_category_moves_amount_exactly_once(self, db, admin, seeded):
        ann = seeded["ann"]
        friday_txn = ContributionTransaction.query.filter_by(contributor_id=ann.id, collection_type=CollectionType.FRIDAY).one()
        friday_txn.collection_type = CollectionType.MUKULULO
        db.session.commit()
        _, totals = contributor_report_rows(DAY_A, DAY_A)
        assert totals["FRIDAY"] == 0
        assert totals["MUKULULO"] == 3000
        assert totals["GRAND"] == 7000  # unchanged overall total

    def test_inactive_contributor_amounts_still_reconcile(self, db, admin, seeded):
        seeded["ben"].active = False
        db.session.commit()
        rows, totals = contributor_report_rows(DAY_A, DAY_A)
        assert sum(r["total"] for r in rows) == totals["GRAND"] == 7000


# ---------------------------------------------------------------------------
# Combined Mukululo + Friday + Sunday report
# ---------------------------------------------------------------------------

class TestCombinedReport:
    def test_includes_all_three_categories_in_separate_columns(self, client, admin, seeded):
        login(client, "admin1")
        html = client.get("/reports/combined?date_from=2026-10-01&date_to=2026-10-02").data.decode()
        for heading in ("Mukululo", "Friday", "Sunday"):
            assert heading in html

    def test_totals_reconcile_with_component_reports(self, db, seeded):
        combined_rows, combined_totals = daily_category_rows(DAY_A, DAY_B)
        assert combined_totals["MUKULULO"] == 1000 + 8000
        assert combined_totals["FRIDAY"] == 2000
        assert combined_totals["SUNDAY"] == 4000
        assert combined_totals["GRAND"] == 15000
        assert sum(r["total"] for r in combined_rows) == combined_totals["GRAND"]  # no double counting

    def test_exact_date_and_csv(self, client, admin, seeded):
        login(client, "admin1")
        csv = client.get("/reports/combined?exact_date=2026-10-02&format=csv").data.decode()
        lines = [line for line in csv.splitlines() if line]
        assert lines[0] == "Date,Mukululo,Friday,Sunday,Total"
        assert lines[1] == "2026-10-02,8000,0,0,8000"
        assert lines[-1] == "GRAND TOTAL,8000,0,0,8000"

    def test_empty_day(self, client, admin, seeded):
        login(client, "admin1")
        html = client.get(f"/reports/combined?exact_date={DAY_EMPTY.isoformat()}").data.decode()
        assert "No entries in this range." in html

    def test_viewer_can_read_combined_report(self, client, viewer_user, seeded):
        login(client, "viewer1")
        assert client.get("/reports/combined").status_code == 200


class TestCollectionReportExactDate:
    def test_friday_report_exact_date_and_total(self, client, admin, seeded):
        login(client, "admin1")
        html = client.get("/reports/collection/friday?exact_date=2026-10-01").data.decode()
        assert "Total for period" in html and "UGX 2,000" in html

    def test_banking_report_exact_date(self, client, db, admin, seeded):
        bank = BankAccount.query.filter_by(fund_type=FundType.MUKULULO).first()
        db.session.add(BankDeposit(deposit_date=DAY_A, fund=FundType.MUKULULO, bank_account_id=bank.id, amount=500, created_by_id=admin.id))
        db.session.add(BankDeposit(deposit_date=DAY_B, fund=FundType.MUKULULO, bank_account_id=bank.id, amount=900, created_by_id=admin.id))
        db.session.commit()
        login(client, "admin1")
        html = client.get("/reports/banking?exact_date=2026-10-01").data.decode()
        assert "UGX 500" in html and "UGX 900" not in html


# ---------------------------------------------------------------------------
# Daily Close: one synchronised reconciliation date
# ---------------------------------------------------------------------------

class TestDailyCloseDate:
    def test_left_figures_and_physical_count_use_the_same_date(self, client, admin, seeded):
        login(client, "admin1")
        html = client.get(f"/collections/session?date={DAY_A.isoformat()}").data.decode()
        assert "Collections on 01 Oct 2026" in html
        assert "Physical cash counted on 01 Oct 2026" in html
        assert "UGX 7,000" in html  # DAY_A grand total, not DAY_B's

    def test_changing_date_changes_left_side_figures(self, client, admin, seeded):
        login(client, "admin1")
        html_b = client.get(f"/collections/session?date={DAY_B.isoformat()}").data.decode()
        assert "UGX 8,000" in html_b and "UGX 7,000" not in html_b

    def test_saving_uses_selected_date_only(self, client, db, admin, seeded):
        login(client, "admin1")
        resp = client.post(
            f"/collections/session?date={DAY_A.isoformat()}",
            data={"physical_cash_counted": "6500", "notes": "", "reconciliation_date": DAY_A.isoformat()},
            follow_redirects=True,
        )
        assert b"Daily close saved: SHORT" in resp.data
        row = CollectionSession.query.filter_by(date=DAY_A).one()
        assert row.mukululo_system_total == 1000
        assert row.friday_system_total == 2000
        assert row.sunday_system_total == 4000
        assert row.difference == 6500 - 7000
        assert CollectionSession.query.filter_by(date=DAY_B).first() is None

    def test_posted_different_date_is_rejected_and_nothing_saved(self, client, db, admin, seeded):
        login(client, "admin1")
        resp = client.post(
            f"/collections/session?date={DAY_A.isoformat()}",
            data={"physical_cash_counted": "9999", "reconciliation_date": DAY_B.isoformat()},
            follow_redirects=True,
        )
        assert b"did not match this page" in resp.data
        assert CollectionSession.query.count() == 0

    def test_existing_count_loads_for_its_own_day_only(self, client, db, admin, seeded):
        login(client, "admin1")
        client.post(f"/collections/session?date={DAY_A.isoformat()}",
                    data={"physical_cash_counted": "7000", "reconciliation_date": DAY_A.isoformat()})
        html_a = client.get(f"/collections/session?date={DAY_A.isoformat()}").data.decode()
        html_b = client.get(f"/collections/session?date={DAY_B.isoformat()}").data.decode()
        assert 'value="7000"' in html_a
        assert 'value="7000"' not in html_b
        assert "No daily close saved for this date yet." in html_b

    def test_one_date_cannot_overwrite_another(self, client, db, admin, seeded):
        login(client, "admin1")
        client.post(f"/collections/session?date={DAY_A.isoformat()}",
                    data={"physical_cash_counted": "7000", "reconciliation_date": DAY_A.isoformat()})
        client.post(f"/collections/session?date={DAY_B.isoformat()}",
                    data={"physical_cash_counted": "100", "reconciliation_date": DAY_B.isoformat()})
        assert CollectionSession.query.filter_by(date=DAY_A).one().physical_cash_counted == 7000
        assert CollectionSession.query.filter_by(date=DAY_B).one().physical_cash_counted == 100

    def test_only_admin_and_data_entry_can_close_days(self, client, collector_user, seeded):
        login(client, "collector1")
        assert client.get("/collections/session").status_code == 403


# ---------------------------------------------------------------------------
# Contribution edit / delete
# ---------------------------------------------------------------------------

def _edit_payload(**overrides):
    payload = {
        "contributor_name": "ANN SYNTHETIC",
        "date": DAY_A.isoformat(),
        "collection_type": "FRIDAY",
        "amount": "2000",
        "note": "",
        "reason": "Correcting test entry",
    }
    payload.update(overrides)
    return payload


def _friday_txn_for(seeded_ann):
    return ContributionTransaction.query.filter_by(contributor_id=seeded_ann.id, collection_type=CollectionType.FRIDAY).one()


class TestContributionEdit:
    def test_admin_corrects_friday_to_mukululo_without_duplicating(self, client, db, admin, seeded):
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        count_before = ContributionTransaction.query.count()

        resp = client.post(f"/collections/transactions/{txn.id}/edit",
                           data=_edit_payload(collection_type="MUKULULO"), follow_redirects=True)
        assert b"Entry corrected" in resp.data

        db.session.refresh(txn)
        assert txn.collection_type == CollectionType.MUKULULO
        assert txn.amount == 2000
        assert ContributionTransaction.query.count() == count_before  # no duplicate

        _, totals = contributor_report_rows(DAY_A, DAY_A)
        assert totals["FRIDAY"] == 0 and totals["MUKULULO"] == 3000

    def test_edit_writes_before_and_after_audit(self, client, db, admin, seeded):
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        client.post(f"/collections/transactions/{txn.id}/edit",
                    data=_edit_payload(collection_type="MUKULULO", amount="2500"))
        entry = AuditLog.query.filter_by(entity_type="ContributionTransaction", entity_id=txn.id,
                                         action=AuditAction.EDIT).one()
        assert '"collection_type": "FRIDAY"' in entry.before_json and '"amount": 2000' in entry.before_json
        assert '"collection_type": "MUKULULO"' in entry.after_json and '"amount": 2500' in entry.after_json
        assert entry.reason == "Correcting test entry"
        assert entry.user_id == admin.id

    def test_contributor_and_date_can_be_corrected(self, client, db, admin, seeded):
        login(client, "admin1")
        _contributor(db, "CAROL SYNTHETIC")
        txn = _friday_txn_for(seeded["ann"])
        client.post(f"/collections/transactions/{txn.id}/edit",
                    data=_edit_payload(contributor_name="carol  synthetic", date=DAY_B.isoformat(), collection_type="FRIDAY"))
        db.session.refresh(txn)
        assert txn.contributor.name == "CAROL SYNTHETIC"
        assert txn.date == DAY_B

    def test_unknown_contributor_is_refused_and_not_created(self, client, db, admin, seeded):
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        count = Contributor.query.count()
        resp = client.post(f"/collections/transactions/{txn.id}/edit",
                           data=_edit_payload(contributor_name="NOBODY SYNTHETIC"), follow_redirects=True)
        assert b"No active contributor" in resp.data
        assert Contributor.query.count() == count
        db.session.refresh(txn)
        assert txn.contributor.name == "ANN SYNTHETIC"

    def test_collector_cannot_edit(self, client, db, collector_user, seeded):
        login(client, "collector1")
        txn = _friday_txn_for(seeded["ann"])
        assert client.get(f"/collections/transactions/{txn.id}/edit").status_code == 403
        assert client.post(f"/collections/transactions/{txn.id}/edit",
                           data=_edit_payload(collection_type="MUKULULO")).status_code == 403
        db.session.refresh(txn)
        assert txn.collection_type == CollectionType.FRIDAY

    def test_viewer_cannot_edit_or_delete(self, client, db, viewer_user, seeded):
        login(client, "viewer1")
        txn = _friday_txn_for(seeded["ann"])
        assert client.post(f"/collections/transactions/{txn.id}/void",
                           data={"reason": "nope", "confirm": "y"}).status_code == 403
        db.session.refresh(txn)
        assert txn.status == TransactionStatus.ACTIVE

    def test_reason_is_required(self, client, db, admin, seeded):
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        client.post(f"/collections/transactions/{txn.id}/edit", data=_edit_payload(collection_type="MUKULULO", reason=""))
        db.session.refresh(txn)
        assert txn.collection_type == CollectionType.FRIDAY

    def test_locked_historical_month_blocks_edit(self, client, db, admin, seeded):
        db.session.add(HistoricalOfficialMonthlyTotal(year=2026, month=10, mukululo_total=0, friday_total=0,
                                                      sunday_total=0, locked=True, created_by_id=admin.id))
        db.session.commit()
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        resp = client.post(f"/collections/transactions/{txn.id}/edit",
                           data=_edit_payload(collection_type="MUKULULO"), follow_redirects=True)
        assert b"locked as an official historical total" in resp.data
        db.session.refresh(txn)
        assert txn.collection_type == CollectionType.FRIDAY

    def test_edit_that_would_drop_banked_fund_below_zero_is_refused(self, client, db, admin, seeded):
        # Friday & Sunday has UGX 6,000 collected and UGX 6,000 banked.
        bank = BankAccount.query.filter_by(fund_type=FundType.FRIDAY_SUNDAY).first()
        db.session.add(BankDeposit(deposit_date=DAY_B, fund=FundType.FRIDAY_SUNDAY, bank_account_id=bank.id,
                                   amount=6000, created_by_id=admin.id))
        db.session.commit()
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        resp = client.post(f"/collections/transactions/{txn.id}/edit",
                           data=_edit_payload(collection_type="MUKULULO"), follow_redirects=True)
        assert b"below the amount already banked" in resp.data
        db.session.refresh(txn)
        assert txn.collection_type == CollectionType.FRIDAY  # nothing silently changed


class TestContributionDelete:
    def _void(self, client, txn, **overrides):
        data = {"reason": "Entered twice", "confirm": "y"}
        data.update(overrides)
        return client.post(f"/collections/transactions/{txn.id}/void", data=data, follow_redirects=True)

    def test_delete_requires_explicit_confirmation(self, client, db, admin, seeded):
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        resp = self._void(client, txn, confirm="")
        assert b"Please confirm before deleting" in resp.data
        db.session.refresh(txn)
        assert txn.status == TransactionStatus.ACTIVE

    def test_delete_page_has_ui_confirmation(self, client, db, admin, seeded):
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        html = client.get(f"/collections/transactions/{txn.id}/void").data.decode()
        assert "onsubmit=\"return confirm(" in html

    def test_admin_deletes_entry_it_is_excluded_from_totals_and_kept(self, client, db, admin, seeded):
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        txn_id = txn.id
        resp = self._void(client, txn)
        assert b"Entry deleted" in resp.data

        row = db.session.get(ContributionTransaction, txn_id)
        assert row is not None and row.status == TransactionStatus.VOID
        _, totals = contributor_report_rows(DAY_A, DAY_A)
        assert totals["FRIDAY"] == 0 and totals["GRAND"] == 5000
        _, combined = daily_category_rows(DAY_A, DAY_A)
        assert combined["FRIDAY"] == 0

        entry = AuditLog.query.filter_by(entity_type="ContributionTransaction", entity_id=txn_id).order_by(AuditLog.id.desc()).first()
        assert entry.action.value == "VOID"
        assert '"status": "ACTIVE"' in entry.before_json
        assert '"status": "VOID"' in entry.after_json
        assert entry.reason == "Entered twice"

    def test_voided_entry_cannot_be_edited(self, client, db, admin, seeded):
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        self._void(client, txn)
        resp = client.post(f"/collections/transactions/{txn.id}/edit",
                           data=_edit_payload(collection_type="MUKULULO"), follow_redirects=True)
        assert b"Deleted entries cannot be edited" in resp.data

    def test_voided_entries_are_hidden_from_contributor_profile_totals(self, client, db, admin, seeded):
        login(client, "admin1")
        self._void(client, _friday_txn_for(seeded["ann"]))
        html = client.get(f"/contributors/{seeded['ann'].id}?year=2026").data.decode()
        assert "UGX 1,000" in html  # ANN's remaining Mukululo only

    def test_delete_that_would_drop_banked_fund_below_zero_is_refused(self, client, db, admin, seeded):
        bank = BankAccount.query.filter_by(fund_type=FundType.FRIDAY_SUNDAY).first()
        db.session.add(BankDeposit(deposit_date=DAY_B, fund=FundType.FRIDAY_SUNDAY, bank_account_id=bank.id,
                                   amount=6000, created_by_id=admin.id))
        db.session.commit()
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        resp = self._void(client, txn)
        assert b"below the amount already banked" in resp.data
        db.session.refresh(txn)
        assert txn.status == TransactionStatus.ACTIVE

    def test_delete_in_locked_month_is_refused(self, client, db, admin, seeded):
        db.session.add(HistoricalOfficialMonthlyTotal(year=2026, month=10, mukululo_total=0, friday_total=0,
                                                      sunday_total=0, locked=True, created_by_id=admin.id))
        db.session.commit()
        login(client, "admin1")
        txn = _friday_txn_for(seeded["ann"])
        resp = self._void(client, txn)
        assert b"locked as an official historical total" in resp.data
        db.session.refresh(txn)
        assert txn.status == TransactionStatus.ACTIVE

    def test_data_entry_role_keeps_existing_correction_privilege(self, client, db, seeded):
        make_user(db, "dataentry9", UserRole.DATA_ENTRY)
        login(client, "dataentry9")
        assert client.get(f"/collections/transactions/{_friday_txn_for(seeded['ann']).id}/edit").status_code == 200


class TestDailyCloseRefreshAfterCorrection:
    def test_existing_close_recalculates_after_category_correction(self, client, db, admin, seeded):
        login(client, "admin1")
        client.post(f"/collections/session?date={DAY_A.isoformat()}",
                    data={"physical_cash_counted": "7000", "reconciliation_date": DAY_A.isoformat()})
        assert CollectionSession.query.filter_by(date=DAY_A).one().status.value == "BALANCED"

        txn = _friday_txn_for(seeded["ann"])
        client.post(f"/collections/transactions/{txn.id}/edit",
                    data=_edit_payload(collection_type="MUKULULO"))
        row = CollectionSession.query.filter_by(date=DAY_A).one()
        assert row.physical_cash_counted == 7000          # the count entered is preserved
        assert row.mukululo_system_total == 3000          # snapshot now matches the active data
        assert row.friday_system_total == 0
        assert row.difference == 0 and row.status.value == "BALANCED"

        audit = AuditLog.query.filter_by(entity_type="CollectionSession", entity_id=row.id).one()
        assert "Recalculated after contribution correction" in audit.reason

    def test_delete_updates_close_variance(self, client, db, admin, seeded):
        login(client, "admin1")
        client.post(f"/collections/session?date={DAY_A.isoformat()}",
                    data={"physical_cash_counted": "7000", "reconciliation_date": DAY_A.isoformat()})
        client.post(f"/collections/transactions/{_friday_txn_for(seeded['ann']).id}/void",
                    data={"reason": "Duplicate", "confirm": "y"})
        row = CollectionSession.query.filter_by(date=DAY_A).one()
        assert row.friday_system_total == 0
        assert row.difference == 2000 and row.status.value == "OVER"
