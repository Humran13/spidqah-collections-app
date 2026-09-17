"""Tests for app.services.contributor_import / flask import-contributors.
All names used here are synthetic placeholders - not real SPIDQAH
contributor data.
"""
import pytest

from app.extensions import db
from app.models import (
    Contributor,
    ContributionTransaction,
    HistoricalOfficialMonthlyTotal,
    User,
    CHIKUMI_100_NAME,
)
from app.services.contributor_import import (
    PayloadError,
    parse_names,
    build_plan,
    apply_plan,
    ACTION_CREATE,
    ACTION_SKIP_EXISTING,
    ACTION_SKIP_DUPLICATE_IN_PAYLOAD,
)
from app.services.totals import contributor_detail_total


SYNTHETIC_NAMES = [
    "Test Alpha One",
    "Test Bravo Two",
    "Test Charlie Three",
]


class TestParsing(object):
    def test_parses_newline_separated_text(self, app):
        names = parse_names("Test Alpha One\nTest Bravo Two\n\nTest Charlie Three\n")
        assert names == ["Test Alpha One", "Test Bravo Two", "", "Test Charlie Three"]

    def test_parses_json_list_of_strings(self, app):
        names = parse_names('["Test Alpha One", "Test Bravo Two"]')
        assert names == ["Test Alpha One", "Test Bravo Two"]

    def test_parses_json_list_of_objects(self, app):
        names = parse_names('[{"name": "Test Alpha One"}, {"name": "Test Bravo Two"}]')
        assert names == ["Test Alpha One", "Test Bravo Two"]

    def test_parses_json_object_with_names_key(self, app):
        names = parse_names('{"names": ["Test Alpha One", "Test Bravo Two"]}')
        assert names == ["Test Alpha One", "Test Bravo Two"]

    def test_rejects_empty_payload(self, app):
        with pytest.raises(PayloadError):
            parse_names("   \n  \n")

    def test_rejects_malformed_json(self, app):
        with pytest.raises(PayloadError):
            parse_names("[not valid json")


class TestNewNamesImportCorrectly(object):
    def test_new_names_are_created_active_with_no_phone_or_notes(self, app, admin_user):
        plan = build_plan(SYNTHETIC_NAMES)
        assert all(p["action"] == ACTION_CREATE for p in plan)

        results = apply_plan(plan, user=admin_user)
        assert sum(1 for r in results if r["applied"] == "created") == 3

        for name in SYNTHETIC_NAMES:
            c = Contributor.query.filter_by(name_normalized=Contributor.normalize(name)).first()
            assert c is not None
            assert c.active is True
            assert c.phone is None
            assert c.notes is None
            assert c.created_by_id == admin_user.id


class TestExactDuplicateProtection(object):
    def test_existing_exact_name_is_skipped_not_duplicated(self, app, admin_user):
        db.session.add(Contributor(name="Test Alpha One", name_normalized=Contributor.normalize("Test Alpha One")))
        db.session.commit()

        plan = build_plan(["Test Alpha One", "Test Bravo Two"])
        actions = {p["name"]: p["action"] for p in plan}
        assert actions["Test Alpha One"] == ACTION_SKIP_EXISTING
        assert actions["Test Bravo Two"] == ACTION_CREATE

        apply_plan(plan, user=admin_user)
        assert Contributor.query.filter_by(name_normalized=Contributor.normalize("Test Alpha One")).count() == 1

    def test_case_differences_do_not_create_duplicates(self, app, admin_user):
        db.session.add(Contributor(name="Test Alpha One", name_normalized=Contributor.normalize("Test Alpha One")))
        db.session.commit()

        plan = build_plan(["test alpha one", "TEST ALPHA ONE"])
        # The first matches the pre-existing row (SKIP_EXISTING); the
        # second is identical after normalizing and is caught as a
        # within-payload duplicate before it even reaches the DB check.
        assert all(p["action"] in (ACTION_SKIP_EXISTING, ACTION_SKIP_DUPLICATE_IN_PAYLOAD) for p in plan)
        apply_plan(plan, user=admin_user)
        assert Contributor.query.filter(Contributor.name_normalized == Contributor.normalize("Test Alpha One")).count() == 1

    def test_whitespace_differences_do_not_create_duplicates(self, app, admin_user):
        db.session.add(Contributor(name="Test Alpha One", name_normalized=Contributor.normalize("Test Alpha One")))
        db.session.commit()

        plan = build_plan(["  Test   Alpha    One  ", "Test Alpha One"])
        assert all(p["action"] in (ACTION_SKIP_EXISTING, ACTION_SKIP_DUPLICATE_IN_PAYLOAD) for p in plan)
        apply_plan(plan, user=admin_user)
        assert Contributor.query.filter_by(name_normalized=Contributor.normalize("Test Alpha One")).count() == 1

    def test_duplicate_within_the_same_payload_creates_only_one(self, app, admin_user):
        plan = build_plan(["Test Alpha One", "Test Alpha One", "test alpha one"])
        actions = [p["action"] for p in plan]
        assert actions.count(ACTION_CREATE) == 1
        assert actions.count(ACTION_SKIP_DUPLICATE_IN_PAYLOAD) == 2

        apply_plan(plan, user=admin_user)
        assert Contributor.query.filter_by(name_normalized=Contributor.normalize("Test Alpha One")).count() == 1

    def test_rerunning_the_same_payload_creates_zero_duplicates(self, app, admin_user):
        plan1 = build_plan(SYNTHETIC_NAMES)
        apply_plan(plan1, user=admin_user)
        assert Contributor.query.count() == len(SYNTHETIC_NAMES) + 1  # +1 for CHIKUMI 100 fixture

        plan2 = build_plan(SYNTHETIC_NAMES)
        assert all(p["action"] == ACTION_SKIP_EXISTING for p in plan2)
        apply_plan(plan2, user=admin_user)
        assert Contributor.query.count() == len(SYNTHETIC_NAMES) + 1  # unchanged


class TestSimilarNamesAreNotAutoMerged(object):
    def test_similar_but_non_identical_names_are_both_kept_distinct(self, app, admin_user):
        """Two names differing by only a letter or two must both be
        created, never silently merged - only a human should decide
        whether such near-identical spellings refer to the same person."""
        plan = build_plan(["Test Person Alpha", "Test Person Alfa"])
        actions = {p["name"]: p["action"] for p in plan}
        assert actions["Test Person Alpha"] == ACTION_CREATE
        assert actions["Test Person Alfa"] == ACTION_CREATE

        # Both are new (neither exists in the DB yet) - the warning must
        # still surface by comparing against other names in this same
        # payload, not just against what is already in the database.
        by_name = {p["name"]: p for p in plan}
        second_item_similar_names = [s["name"] for s in by_name["Test Person Alfa"]["similar"]]
        assert "Test Person Alpha" in second_item_similar_names

        apply_plan(plan, user=admin_user)
        assert Contributor.query.filter_by(name_normalized=Contributor.normalize("Test Person Alpha")).count() == 1
        assert Contributor.query.filter_by(name_normalized=Contributor.normalize("Test Person Alfa")).count() == 1
        # Both exist as separate, distinct contributors.
        assert Contributor.query.filter(
            Contributor.name_normalized.in_([
                Contributor.normalize("Test Person Alpha"), Contributor.normalize("Test Person Alfa"),
            ])
        ).count() == 2

    def test_similar_name_is_reported_as_a_warning_but_still_created(self, app, admin_user):
        db.session.add(Contributor(name="Test Delta Existing", name_normalized=Contributor.normalize("Test Delta Existing")))
        db.session.commit()

        plan = build_plan(["Test Delta Existin"])  # one character short - similar, not identical
        item = plan[0]
        assert item["action"] == ACTION_CREATE
        assert len(item["similar"]) >= 1  # flagged as a warning only

        apply_plan(plan, user=admin_user)
        assert Contributor.query.filter_by(name_normalized=Contributor.normalize("Test Delta Existin")).count() == 1
        assert Contributor.query.filter_by(name_normalized=Contributor.normalize("Test Delta Existing")).count() == 1


class TestNoFinancialDataCreated(object):
    def test_no_contribution_transactions_are_created(self, app, admin_user):
        assert ContributionTransaction.query.count() == 0
        apply_plan(build_plan(SYNTHETIC_NAMES), user=admin_user)
        assert ContributionTransaction.query.count() == 0

    def test_no_financial_totals_change(self, app, admin_user):
        db.session.add(HistoricalOfficialMonthlyTotal(
            year=2026, month=1, mukululo_total=100000, friday_total=20000, sunday_total=10000,
            locked=True, created_by_id=admin_user.id,
        ))
        db.session.commit()

        before = contributor_detail_total()
        apply_plan(build_plan(SYNTHETIC_NAMES), user=admin_user)
        after = contributor_detail_total()
        assert before == after == 0

        row = HistoricalOfficialMonthlyTotal.query.filter_by(year=2026, month=1).first()
        assert row.mukululo_total == 100000
        assert row.friday_total == 20000
        assert row.sunday_total == 10000

    def test_new_contributors_have_no_phone_number_imported(self, app, admin_user):
        apply_plan(build_plan(SYNTHETIC_NAMES), user=admin_user)
        for name in SYNTHETIC_NAMES:
            c = Contributor.query.filter_by(name_normalized=Contributor.normalize(name)).first()
            assert c.phone is None


class TestDryRunAndIdempotency(object):
    def test_dry_run_makes_zero_database_changes(self, app, admin_user):
        build_plan(SYNTHETIC_NAMES)  # dry-run equivalent: build_plan never writes
        assert Contributor.query.filter(
            Contributor.name_normalized.in_([Contributor.normalize(n) for n in SYNTHETIC_NAMES])
        ).count() == 0

    def test_apply_is_idempotent(self, app, admin_user):
        results1 = apply_plan(build_plan(SYNTHETIC_NAMES), user=admin_user)
        assert sum(1 for r in results1 if r["applied"] == "created") == 3

        results2 = apply_plan(build_plan(SYNTHETIC_NAMES), user=admin_user)
        assert all(r["applied"] == "skipped" for r in results2)
        assert Contributor.query.filter(
            Contributor.name_normalized.in_([Contributor.normalize(n) for n in SYNTHETIC_NAMES])
        ).count() == 3


class TestDoesNotTouchUnrelatedData(object):
    def test_chikumi_100_remains_intact(self, app, admin_user):
        before = Contributor.query.filter_by(name_normalized=Contributor.normalize(CHIKUMI_100_NAME)).first()
        assert before is not None

        apply_plan(build_plan(SYNTHETIC_NAMES), user=admin_user)

        after = Contributor.query.filter_by(name_normalized=Contributor.normalize(CHIKUMI_100_NAME)).first()
        assert after is not None
        assert after.id == before.id
        assert after.name == CHIKUMI_100_NAME
        assert after.active is True

    def test_existing_users_and_permissions_remain_untouched(self, app, admin_user, data_entry_user):
        admin_hash = admin_user.password_hash
        de_hash = data_entry_user.password_hash

        apply_plan(build_plan(SYNTHETIC_NAMES), user=admin_user)

        assert db.session.get(User, admin_user.id).password_hash == admin_hash
        assert db.session.get(User, admin_user.id).role == admin_user.role
        assert db.session.get(User, data_entry_user.id).password_hash == de_hash
        assert db.session.get(User, data_entry_user.id).role == data_entry_user.role
