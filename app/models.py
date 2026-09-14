import enum
from datetime import datetime, date

from flask_login import UserMixin
from sqlalchemy import (
    UniqueConstraint,
    CheckConstraint,
    Index,
    func,
)
from werkzeug.security import generate_password_hash, check_password_hash

from app.extensions import db


def utcnow():
    return datetime.utcnow()


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class UserRole(str, enum.Enum):
    ADMIN = "ADMIN"
    DATA_ENTRY = "DATA_ENTRY"
    COLLECTOR = "COLLECTOR"
    VIEWER = "VIEWER"


class CollectionType(str, enum.Enum):
    MUKULULO = "MUKULULO"
    FRIDAY = "FRIDAY"
    SUNDAY = "SUNDAY"


class FundType(str, enum.Enum):
    """The two financial ownership funds / bank accounts."""
    MUKULULO = "MUKULULO"
    FRIDAY_SUNDAY = "FRIDAY_SUNDAY"


# Which CollectionType(s) belong to which FundType. Friday and Sunday stay
# distinguishable at the transaction level but both belong to the combined
# Friday & Sunday fund/bank account. Chikumi 100 is just a contributor under
# MUKULULO - it is NOT a separate fund and must never be added a second time.
FUND_COLLECTION_TYPES = {
    FundType.MUKULULO: [CollectionType.MUKULULO],
    FundType.FRIDAY_SUNDAY: [CollectionType.FRIDAY, CollectionType.SUNDAY],
}


def fund_for_collection_type(collection_type: CollectionType) -> FundType:
    for fund, types in FUND_COLLECTION_TYPES.items():
        if collection_type in types:
            return fund
    raise ValueError(f"Unknown collection type {collection_type}")


class TransactionStatus(str, enum.Enum):
    ACTIVE = "ACTIVE"
    VOID = "VOID"


class SessionStatus(str, enum.Enum):
    BALANCED = "BALANCED"
    SHORT = "SHORT"
    OVER = "OVER"


class ReconciliationStatus(str, enum.Enum):
    RECONCILED = "RECONCILED"
    DIFFERENCE = "DIFFERENCE"


class AdjustmentDirection(str, enum.Enum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"


class AuditAction(str, enum.Enum):
    CREATE = "CREATE"
    EDIT = "EDIT"
    VOID = "VOID"
    LOCK = "LOCK"
    UNLOCK = "UNLOCK"
    LOGIN = "LOGIN"
    LOGIN_FAILED = "LOGIN_FAILED"
    DEACTIVATE = "DEACTIVATE"
    ACTIVATE = "ACTIVATE"
    RESET_PASSWORD = "RESET_PASSWORD"
    MERGE = "MERGE"
    OVERRIDE = "OVERRIDE"


# ---------------------------------------------------------------------------
# User / Auth
# ---------------------------------------------------------------------------

class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=True, index=True)
    display_name = db.Column(db.String(150), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.Enum(UserRole, name="user_role"), nullable=False, default=UserRole.VIEWER)
    active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime, nullable=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime, nullable=True)

    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    @property
    def is_active(self):
        return self.active

    def has_role(self, *roles):
        return self.role in roles

    @property
    def is_admin(self):
        return self.role == UserRole.ADMIN

    def __repr__(self):
        return f"<User {self.username} ({self.role.value})>"


# ---------------------------------------------------------------------------
# Contributors
# ---------------------------------------------------------------------------

class Contributor(db.Model):
    __tablename__ = "contributors"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    name_normalized = db.Column(db.String(150), nullable=False, index=True)
    phone = db.Column(db.String(30), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    # Set when this contributor's record was merged into another one.
    merged_into_id = db.Column(db.Integer, db.ForeignKey("contributors.id"), nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    merged_into = db.relationship("Contributor", remote_side=[id])

    __table_args__ = (
        UniqueConstraint("name_normalized", name="uq_contributor_name_normalized"),
        Index("ix_contributors_active", "active"),
    )

    @staticmethod
    def normalize(name: str) -> str:
        return " ".join(name.strip().split()).upper()

    def __repr__(self):
        return f"<Contributor {self.name}>"


CHIKUMI_100_NAME = "CHIKUMI 100"


# ---------------------------------------------------------------------------
# Contribution Transactions
# ---------------------------------------------------------------------------

class ContributionTransaction(db.Model):
    """A single contributor's contribution on a given date.

    ``is_historical_backentry`` marks entries created through the Historical
    Contributor Entry workflow to reconstruct contributor history. Whether a
    transaction counts toward the *official* accounting totals is determined
    purely by comparing ``date`` against the configured go-live date (see
    app.services.totals) - not by this flag. The flag exists so the UI can
    clearly label reconstructed entries.
    """
    __tablename__ = "contribution_transactions"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    collection_type = db.Column(db.Enum(CollectionType, name="collection_type"), nullable=False, index=True)
    contributor_id = db.Column(db.Integer, db.ForeignKey("contributors.id"), nullable=False, index=True)
    amount = db.Column(db.Integer, nullable=False)  # integer UGX, never float
    note = db.Column(db.String(500), nullable=True)

    is_historical_backentry = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.Enum(TransactionStatus, name="transaction_status"), nullable=False, default=TransactionStatus.ACTIVE)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=True)

    voided_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    voided_at = db.Column(db.DateTime, nullable=True)
    void_reason = db.Column(db.String(500), nullable=True)

    contributor = db.relationship("Contributor", backref=db.backref("transactions", lazy="dynamic"))
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    updated_by = db.relationship("User", foreign_keys=[updated_by_id])
    voided_by = db.relationship("User", foreign_keys=[voided_by_id])

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_transaction_amount_positive"),
        Index("ix_transactions_date_type", "date", "collection_type"),
    )

    @property
    def fund(self) -> FundType:
        return fund_for_collection_type(self.collection_type)

    def __repr__(self):
        return f"<ContributionTransaction {self.date} {self.collection_type.value} {self.amount}>"


# ---------------------------------------------------------------------------
# Historical Official Monthly Totals (locked source of truth pre go-live)
# ---------------------------------------------------------------------------

class HistoricalOfficialMonthlyTotal(db.Model):
    __tablename__ = "historical_official_monthly_totals"

    id = db.Column(db.Integer, primary_key=True)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1-12

    mukululo_total = db.Column(db.Integer, nullable=False, default=0)
    friday_total = db.Column(db.Integer, nullable=False, default=0)
    sunday_total = db.Column(db.Integer, nullable=False, default=0)

    notes = db.Column(db.Text, nullable=True)
    locked = db.Column(db.Boolean, nullable=False, default=False)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_modified_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    last_modified_at = db.Column(db.DateTime, nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    last_modified_by = db.relationship("User", foreign_keys=[last_modified_by_id])

    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_historical_total_year_month"),
        CheckConstraint("month >= 1 AND month <= 12", name="ck_historical_month_range"),
        CheckConstraint("mukululo_total >= 0 AND friday_total >= 0 AND sunday_total >= 0", name="ck_historical_totals_nonneg"),
    )

    @property
    def friday_sunday_combined_total(self):
        return self.friday_total + self.sunday_total

    def __repr__(self):
        return f"<HistoricalOfficialMonthlyTotal {self.year}-{self.month:02d} locked={self.locked}>"


# ---------------------------------------------------------------------------
# Collection Session / Daily Reconciliation
# ---------------------------------------------------------------------------

class CollectionSession(db.Model):
    __tablename__ = "collection_sessions"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, unique=True, index=True)

    mukululo_system_total = db.Column(db.Integer, nullable=False, default=0)
    friday_system_total = db.Column(db.Integer, nullable=False, default=0)
    sunday_system_total = db.Column(db.Integer, nullable=False, default=0)

    physical_cash_counted = db.Column(db.Integer, nullable=True)
    difference = db.Column(db.Integer, nullable=True)
    status = db.Column(db.Enum(SessionStatus, name="session_status"), nullable=True)

    notes = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime, nullable=True)

    created_by = db.relationship("User", foreign_keys=[created_by_id])

    @property
    def grand_total(self):
        return self.mukululo_system_total + self.friday_system_total + self.sunday_system_total

    def __repr__(self):
        return f"<CollectionSession {self.date} {self.status}>"


# ---------------------------------------------------------------------------
# Bank Accounts, Deposits, Adjustments, Reconciliation
# ---------------------------------------------------------------------------

class BankAccount(db.Model):
    __tablename__ = "bank_accounts"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False, unique=True)
    fund_type = db.Column(db.Enum(FundType, name="bank_account_fund_type"), nullable=False)

    opening_balance = db.Column(db.Integer, nullable=False, default=0)
    opening_balance_date = db.Column(db.Date, nullable=True)

    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)

    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    def __repr__(self):
        return f"<BankAccount {self.name}>"


class BankDeposit(db.Model):
    """Money moving from cash-in-hand into a bank account.

    ``fund`` is which fund the money legally BELONGS TO (ownership).
    ``bank_account_id`` is where it was PHYSICALLY deposited - these can
    differ (e.g. Friday/Sunday money temporarily deposited into the
    Mukululo bank account while a prior loan is being repaid) and that
    must never silently change ownership.
    """
    __tablename__ = "bank_deposits"

    id = db.Column(db.Integer, primary_key=True)
    deposit_date = db.Column(db.Date, nullable=False, index=True)
    fund = db.Column(db.Enum(FundType, name="deposit_fund_type"), nullable=False, index=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_accounts.id"), nullable=False)

    amount = db.Column(db.Integer, nullable=False)
    reference = db.Column(db.String(150), nullable=True)
    slip_reference = db.Column(db.String(150), nullable=True)
    notes = db.Column(db.Text, nullable=True)

    exceeds_available = db.Column(db.Boolean, nullable=False, default=False)
    override_reason = db.Column(db.String(500), nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bank_account = db.relationship("BankAccount", backref="deposits")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_deposit_amount_positive"),
    )

    def __repr__(self):
        return f"<BankDeposit {self.deposit_date} {self.fund.value} {self.amount}>"


class BankAdjustment(db.Model):
    __tablename__ = "bank_adjustments"

    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_accounts.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    direction = db.Column(db.Enum(AdjustmentDirection, name="adjustment_direction"), nullable=False)
    reason = db.Column(db.String(500), nullable=False)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bank_account = db.relationship("BankAccount", backref="adjustments")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_adjustment_amount_positive"),
    )

    @property
    def signed_amount(self):
        return self.amount if self.direction == AdjustmentDirection.INCREASE else -self.amount


class BankReconciliation(db.Model):
    __tablename__ = "bank_reconciliations"

    id = db.Column(db.Integer, primary_key=True)
    bank_account_id = db.Column(db.Integer, db.ForeignKey("bank_accounts.id"), nullable=False)
    balance_date = db.Column(db.Date, nullable=False)
    actual_balance = db.Column(db.Integer, nullable=False)
    estimated_balance_snapshot = db.Column(db.Integer, nullable=False)
    difference = db.Column(db.Integer, nullable=False)
    status = db.Column(db.Enum(ReconciliationStatus, name="reconciliation_status"), nullable=False)
    note = db.Column(db.Text, nullable=True)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)

    bank_account = db.relationship("BankAccount", backref="reconciliations")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    def __repr__(self):
        return f"<BankReconciliation {self.bank_account_id} {self.balance_date} {self.status}>"


# ---------------------------------------------------------------------------
# Audit Log (generic, used across the app for financially sensitive changes)
# ---------------------------------------------------------------------------

class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    entity_type = db.Column(db.String(80), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=True, index=True)
    action = db.Column(db.Enum(AuditAction, name="audit_action"), nullable=False)

    before_json = db.Column(db.Text, nullable=True)
    after_json = db.Column(db.Text, nullable=True)
    reason = db.Column(db.String(500), nullable=True)

    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    ip_address = db.Column(db.String(64), nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<AuditLog {self.entity_type}#{self.entity_id} {self.action.value}>"


# ---------------------------------------------------------------------------
# App Settings (key/value)
# ---------------------------------------------------------------------------

class AppSetting(db.Model):
    __tablename__ = "app_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), nullable=False, unique=True)
    value = db.Column(db.Text, nullable=True)

    updated_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)

    def __repr__(self):
        return f"<AppSetting {self.key}={self.value}>"
