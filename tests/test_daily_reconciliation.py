"""Daily Reconciliation report: reads the existing Daily Close records.

All names and amounts are synthetic. Daily Closes are created through the real
Daily Close route so the stored snapshots match production behaviour.
"""
import csv
import io
import re
from datetime import date

import pytest

from app.models import (
    AuditLog,
    CollectionSession,
    CollectionType,
    ContributionTransaction,
    Contributor,
    TransactionStatus,
    UserRole,
)
from tests.conftest import login, make_user

OCT1 = date(2026, 10, 1)
OCT2 = date(2026, 10, 2)
SEP25 = date(2026, 9, 25)
REPORT = "/reports/daily-reconciliation"
CSV_HEADER = ["Collection Date", "Mukululo", "Friday", "Sunday", "Expected Cash", "Physical Count",
              "Difference", "Status", "Closed By", "Closed At", "Notes"]


@pytest.fixture()
def people(db):
    admin = make_user(db, "admin_recon", UserRole.ADMIN)
    entry = make_user(db, "entry_recon", UserRole.DATA_ENTRY)
    return {"admin": admin, "entry": entry}


def _contributor(db, name):
    c = Contributor(name=name, name_normalized=Contributor.normalize(name), active=True)
    db.session.add(c)
    db.session.commit()
    return c


def _txn(db, user, contributor, day, ctype, amount, status=TransactionStatus.ACTIVE):
    t = ContributionTransaction(date=day, collection_type=ctype, contributor_id=contributor.id,
                                amount=amount, status=status, created_by_id=user.id)
    db.session.add(t)
    db.session.commit()
    return t


def _close(client, day, physical, notes=None):
    data = {"physical_cash_counted": str(physical), "reconciliation_date": day.isoformat()}
    if notes:
        data["notes"] = notes
    resp = client.post(f"/collections/session?date={day.isoformat()}", data=data, follow_redirects=True)
    assert b"Daily close saved" in resp.data


@pytest.fixture()
def closes(db, client, people):
    """Three Daily Closes with known figures:
       01 Oct  M 1,000  F 2,000  S 4,000  expected 7,000  physical 7,000  BALANCED  (closed by admin)
       02 Oct  M 8,000                   expected 8,000  physical 7,500  SHORT -500 (closed by data entry)
       25 Sep  F 3,000                   expected 3,000  physical 3,500  OVER  +500 (closed by admin)
    """
    ann = _contributor(db, "ANN SYNTHETIC")
    ben = _contributor(db, "BEN SYNTHETIC")
    admin = people["admin"]
    _txn(db, admin, ann, OCT1, CollectionType.MUKULULO, 1000)
    _txn(db, admin, ann, OCT1, CollectionType.FRIDAY, 2000)
    _txn(db, admin, ben, OCT1, CollectionType.SUNDAY, 4000)
    _txn(db, admin, ben, OCT2, CollectionType.MUKULULO, 8000)
    _txn(db, admin, ann, SEP25, CollectionType.FRIDAY, 3000)

    login(client, "admin_recon")
    _close(client, OCT1, 7000, notes="Counted twice")
    _close(client, SEP25, 3500)
    client.get("/auth/logout")
    client.post("/auth/login", data={"username": "entry_recon", "password": "password123"})
    _close(client, OCT2, 7500, notes="Short by 500 - under review")
    client.get("/auth/logout")
    return {"ann": ann, "ben": ben}


def _csv_rows(client, query):
    resp = client.get(f"{REPORT}?{query}&format=csv")
    assert resp.status_code == 200
    return list(csv.reader(io.StringIO(resp.data.decode())))


def _data_rows(client, query):
    rows = _csv_rows(client, query)
    assert rows[0] == CSV_HEADER
    return {r[0]: r for r in rows[1:] if r[0] != "GRAND TOTAL"}


def _login_admin(client):
    login(client, "admin_recon")


# 1-2: route and navigation -------------------------------------------------

class TestRouteAndNavigation:
    def test_route_exists_for_admin(self, client, people, closes):
        _login_admin(client)
        assert client.get(REPORT).status_code == 200

    def test_reports_page_links_to_daily_reconciliation_separately(self, client, people, closes):
        _login_admin(client)
        html = client.get("/reports/").data.decode()
        assert 'href="/reports/daily-reconciliation"' in html
        assert "Daily Reconciliation" in html
        assert "physical cash closings" in html
        assert 'href="/reports/bank-reconciliation"' in html  # still present, unchanged

    def test_bank_reconciliation_still_separate_and_working(self, client, people, closes):
        _login_admin(client)
        assert client.get("/reports/bank-reconciliation").status_code == 200


# 3-14: contents of the report -------------------------------------------------

class TestReportContents:
    def test_closed_day_appears_with_category_amounts_and_expected(self, client, people, closes):
        _login_admin(client)
        row = _data_rows(client, "exact_date=2026-10-01")["2026-10-01"]
        assert row[1:4] == ["1000", "2000", "4000"]   # Mukululo, Friday, Sunday
        assert row[4] == "7000"                        # Expected
        assert row[5] == "7000"                        # Physical

    def test_balanced_day_shows_balanced_and_zero_difference(self, client, people, closes):
        _login_admin(client)
        row = _data_rows(client, "exact_date=2026-10-01")["2026-10-01"]
        assert row[6] == "0" and row[7] == "BALANCED"

    def test_short_day_shows_short_and_negative_difference(self, client, people, closes):
        _login_admin(client)
        row = _data_rows(client, "exact_date=2026-10-02")["2026-10-02"]
        assert row[1:4] == ["8000", "0", "0"]
        assert row[4] == "8000" and row[5] == "7500"
        assert row[6] == "-500" and row[7] == "SHORT"

    def test_over_day_shows_over_and_positive_difference(self, client, people, closes):
        _login_admin(client)
        row = _data_rows(client, "exact_date=2026-09-25")["2026-09-25"]
        assert row[6] == "500" and row[7] == "OVER"

    def test_closed_by_is_the_user_who_closed_the_day(self, client, people, closes):
        _login_admin(client)
        assert _data_rows(client, "exact_date=2026-10-01")["2026-10-01"][8] == "admin_recon"
        assert _data_rows(client, "exact_date=2026-10-02")["2026-10-02"][8] == "entry_recon"

    def test_closed_at_is_shown_in_kampala_time_and_distinct_from_collection_date(self, client, people, closes, db):
        _login_admin(client)
        row = _data_rows(client, "exact_date=2026-10-01")["2026-10-01"]
        closed_at = row[9]
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", closed_at)
        stored = CollectionSession.query.filter_by(date=OCT1).one()
        assert closed_at != stored.date.isoformat()  # close time is not the collection date

    def test_notes_are_exported_and_shown_on_the_page(self, client, people, closes):
        _login_admin(client)
        assert _data_rows(client, "exact_date=2026-10-01")["2026-10-01"][10] == "Counted twice"
        html = client.get(f"{REPORT}?exact_date=2026-10-02").data.decode()
        assert "Short by 500 - under review" in html  # full note in the mobile card


# 15-18: filters ----------------------------------------------------------------

class TestFilters:
    def test_exact_date_returns_only_that_day(self, client, people, closes):
        _login_admin(client)
        assert list(_data_rows(client, "exact_date=2026-10-02")) == ["2026-10-02"]

    def test_from_to_range(self, client, people, closes):
        _login_admin(client)
        rows = _data_rows(client, "date_from=2026-09-01&date_to=2026-10-01")
        assert sorted(rows) == ["2026-09-25", "2026-10-01"]

    def test_month_and_year_filter(self, client, people, closes):
        _login_admin(client)
        assert sorted(_data_rows(client, "month=10&year=2026")) == ["2026-10-01", "2026-10-02"]
        assert sorted(_data_rows(client, "month=9&year=2026")) == ["2026-09-25"]

    def test_year_only_filter_covers_whole_year(self, client, people, closes):
        _login_admin(client)
        assert len(_data_rows(client, "year=2026")) == 3

    def test_exact_date_takes_precedence_over_from_to(self, client, people, closes):
        _login_admin(client)
        rows = _data_rows(client, "exact_date=2026-10-02&date_from=2026-09-01&date_to=2026-10-01")
        assert list(rows) == ["2026-10-02"]

    def test_from_to_takes_precedence_over_month_year(self, client, people, closes):
        _login_admin(client)
        rows = _data_rows(client, "date_from=2026-09-01&date_to=2026-09-30&month=10&year=2026")
        assert list(rows) == ["2026-09-25"]

    def test_empty_period_shows_zeros(self, client, people, closes):
        _login_admin(client)
        html = client.get(f"{REPORT}?exact_date=2026-01-01").data.decode()
        assert "No Daily Closes in this period." in html
        assert "Days closed" in html


# 19-22: summary (whole filtered set) ----------------------------------------------

class TestSummary:
    def _summary_text(self, client, query):
        html = client.get(f"{REPORT}?{query}").data.decode()
        return html

    def test_days_closed_total_expected_physical_and_net(self, client, people, closes):
        _login_admin(client)
        html = self._summary_text(client, "month=10&year=2026")
        cards = html.split('id="recon-summary"')[1].split('id="recon-counts"')[0]
        assert re.search(r"Days closed</div><div class=\"fw-bold fs-5\">2<", cards)
        assert "UGX 15,000" in cards          # total expected (7,000 + 8,000)
        assert "UGX 14,500" in cards          # total physical (7,000 + 7,500)
        assert "-UGX 500" in cards            # net difference

    def test_status_counts_and_shortage_over_totals(self, client, people, closes):
        _login_admin(client)
        html = self._summary_text(client, "month=10&year=2026")
        counts = html.split('id="recon-counts"')[1]
        assert "Balanced days" in counts and "Short days" in counts and "Over days" in counts
        assert "UGX 500" in counts  # total short (sum of negative differences)
        assert "UGX 0" in counts    # total over

    def test_summary_covers_full_filtered_set_not_just_one_day(self, client, people, closes):
        _login_admin(client)
        html = self._summary_text(client, "date_from=2026-09-01&date_to=2026-10-31")
        cards = html.split('id="recon-summary"')[1].split('id="recon-counts"')[0]
        counts = html.split('id="recon-counts"')[1]
        assert "UGX 18,000" in cards   # 7,000 + 8,000 + 3,000 expected across all three closes
        assert "UGX 18,000" in cards   # physical 7,000 + 7,500 + 3,500
        # Shortage in October and overage in September net to zero overall,
        # but both are still visible in the totals:
        assert "UGX 0" in cards
        assert "UGX 500" in counts and counts.count("UGX 500") >= 2


# 23-25: accounting consistency ------------------------------------------------

class TestAccountingConsistency:
    def test_voided_contribution_does_not_inflate_expected(self, client, db, people, closes):
        ann = closes["ann"]
        _txn(db, people["admin"], ann, OCT1, CollectionType.MUKULULO, 9999, status=TransactionStatus.VOID)
        _login_admin(client)
        row = _data_rows(client, "exact_date=2026-10-01")["2026-10-01"]
        assert row[4] == "7000"

    def test_correction_recalculates_expected_and_difference_in_report(self, client, db, people, closes):
        ben_txn = ContributionTransaction.query.filter_by(date=OCT2).one()
        _login_admin(client)
        client.post(f"/collections/transactions/{ben_txn.id}/void",
                    data={"reason": "", "confirm": "y"}, follow_redirects=True)
        row = _data_rows(client, "exact_date=2026-10-02")["2026-10-02"]
        assert row[4] == "0"               # expected recalculated after the delete
        assert row[6] == "7500"            # physical 7,500 against expected 0
        assert row[7] == "OVER"

    def test_physical_count_preserved_after_recalculation(self, client, db, people, closes):
        ben_txn = ContributionTransaction.query.filter_by(date=OCT2).one()
        _login_admin(client)
        client.post(f"/collections/transactions/{ben_txn.id}/void",
                    data={"reason": "", "confirm": "y"}, follow_redirects=True)
        row = _data_rows(client, "exact_date=2026-10-02")["2026-10-02"]
        assert row[5] == "7500"
        stored = CollectionSession.query.filter_by(date=OCT2).one()
        assert stored.physical_cash_counted == 7500


# 26-28: exports ----------------------------------------------------------------

class TestExports:
    def test_csv_respects_month_filter_and_has_grand_total(self, client, people, closes):
        _login_admin(client)
        rows = _csv_rows(client, "month=10&year=2026")
        data = [r for r in rows[1:] if r[0] != "GRAND TOTAL"]
        assert [r[0] for r in data] == ["2026-10-01", "2026-10-02"]
        grand = rows[-1]
        assert grand[0] == "GRAND TOTAL"
        assert grand[4] == "15000" and grand[5] == "14500" and grand[6] == "-500"

    def test_csv_columns_match_specification(self, client, people, closes):
        _login_admin(client)
        assert _csv_rows(client, "exact_date=2026-10-01")[0] == CSV_HEADER

    def test_export_link_carries_active_filter(self, client, people, closes):
        _login_admin(client)
        html = client.get(f"{REPORT}?exact_date=2026-10-02").data.decode()
        assert "format=csv" in html and "exact_date=2026-10-02" in html
        assert "format=print" in html

    def test_print_view_renders_with_header_and_summary(self, client, people, closes):
        _login_admin(client)
        resp = client.get(f"{REPORT}?month=10&year=2026&format=print")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Daily Reconciliation Report" in html
        assert "SPIDQAH Collections" in html
        assert "Net difference" in html and "Balanced" in html and "Short" in html and "Over" in html
        assert "Period:" in html


# 29-31: permissions and read-only -------------------------------------------------

class TestPermissionsAndReadOnly:
    def test_data_entry_can_view(self, client, people, closes):
        login(client, "entry_recon")
        assert client.get(REPORT).status_code == 200

    def test_viewer_is_blocked_server_side(self, client, db, closes):
        make_user(db, "viewer_recon", UserRole.VIEWER)
        login(client, "viewer_recon")
        assert client.get(REPORT).status_code == 403
        assert client.get(f"{REPORT}?format=csv").status_code == 403
        assert client.get(f"{REPORT}?format=print").status_code == 403

    def test_collector_is_blocked_server_side(self, client, db, closes):
        make_user(db, "collector_recon", UserRole.COLLECTOR)
        login(client, "collector_recon")
        assert client.get(REPORT).status_code == 403

    def test_anonymous_is_redirected_to_login(self, client, closes):
        resp = client.get(REPORT, follow_redirects=False)
        assert resp.status_code in (302, 401)

    def test_viewer_does_not_see_the_link_on_reports_page(self, client, db, closes):
        make_user(db, "viewer_link", UserRole.VIEWER)
        login(client, "viewer_link")
        assert "/reports/daily-reconciliation" not in client.get("/reports/").data.decode()

    def test_viewing_and_exporting_changes_no_financial_data(self, client, db, people, closes):
        def snapshot():
            return (
                [(s.id, s.date, s.mukululo_system_total, s.friday_system_total, s.sunday_system_total,
                  s.physical_cash_counted, s.difference, str(s.status), s.created_at, s.updated_at)
                 for s in CollectionSession.query.order_by(CollectionSession.id).all()],
                [(t.id, t.amount, str(t.status), t.collection_type.value) for t in
                 ContributionTransaction.query.order_by(ContributionTransaction.id).all()],
                AuditLog.query.count(),
            )

        _login_admin(client)  # login writes its own audit row; take the baseline after it
        before = snapshot()
        for q in ("", "month=10&year=2026", "format=csv", "format=print", "exact_date=2026-10-02"):
            assert client.get(f"{REPORT}?{q}").status_code == 200
        assert snapshot() == before


# 32: mobile structure -----------------------------------------------------------

class TestMobileLayout:
    def test_phone_cards_and_desktop_table_both_rendered_with_breakpoint_classes(self, client, people, closes):
        _login_admin(client)
        html = client.get(f"{REPORT}?month=10&year=2026").data.decode()
        assert 'class="d-lg-none"' in html
        assert 'class="card d-none d-lg-block"' in html
        assert html.count('class="mobile-row-card"') == 2
        assert 'data-recon-date="2026-10-02"' in html
