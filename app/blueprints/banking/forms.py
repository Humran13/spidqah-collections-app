from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, DateField, TextAreaField, BooleanField
from wtforms.validators import DataRequired, Optional, Length

FUND_CHOICES = [("MUKULULO", "Mukululo"), ("FRIDAY_SUNDAY", "Friday & Sunday")]
DIRECTION_CHOICES = [("INCREASE", "Increase"), ("DECREASE", "Decrease")]


class DepositForm(FlaskForm):
    deposit_date = DateField("Deposit date", validators=[DataRequired()])
    fund = SelectField("Money belongs to (fund)", choices=FUND_CHOICES, validators=[DataRequired()])
    bank_account_id = SelectField("Physically deposited into", coerce=int, validators=[DataRequired()])
    amount = StringField("Amount (UGX)", validators=[DataRequired()])
    reference = StringField("Bank reference", validators=[Optional(), Length(max=150)])
    slip_reference = StringField("Deposit slip reference", validators=[Optional(), Length(max=150)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])
    override = BooleanField("Override: allow deposit to exceed amount awaiting banking")
    override_reason = TextAreaField("Reason for override", validators=[Optional(), Length(max=500)])


class AdjustmentForm(FlaskForm):
    bank_account_id = SelectField("Bank account", coerce=int, validators=[DataRequired()])
    date = DateField("Date", validators=[DataRequired()])
    amount = StringField("Amount (UGX)", validators=[DataRequired()])
    direction = SelectField("Direction", choices=DIRECTION_CHOICES, validators=[DataRequired()])
    reason = TextAreaField("Reason", validators=[DataRequired(), Length(min=3, max=500)])


class ReconciliationForm(FlaskForm):
    bank_account_id = SelectField("Bank account", coerce=int, validators=[DataRequired()])
    balance_date = DateField("Balance date", validators=[DataRequired()])
    actual_balance = StringField("Actual bank balance (UGX)", validators=[DataRequired()])
    note = TextAreaField("Note / reference", validators=[Optional(), Length(max=1000)])


class BankAccountForm(FlaskForm):
    name = StringField("Account name", validators=[DataRequired(), Length(max=150)])
    opening_balance = StringField("Opening balance (UGX)", validators=[DataRequired()])
    opening_balance_date = DateField("Opening balance as of", validators=[DataRequired()])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])
