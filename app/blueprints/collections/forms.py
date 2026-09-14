from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, DateField, TextAreaField, IntegerField
from wtforms.validators import DataRequired, Optional, Length, NumberRange

COLLECTION_TYPE_CHOICES = [("MUKULULO", "Mukululo"), ("FRIDAY", "Friday"), ("SUNDAY", "Sunday")]


class HistoricalEntryForm(FlaskForm):
    date = DateField("Date", validators=[DataRequired()])
    collection_type = SelectField("Collection", choices=COLLECTION_TYPE_CHOICES, validators=[DataRequired()])
    contributor_name = StringField("Contributor", validators=[DataRequired(), Length(max=150)])
    amount = StringField("Amount (UGX)", validators=[DataRequired()])
    note = StringField("Note", validators=[Optional(), Length(max=500)])


class EditTransactionForm(FlaskForm):
    date = DateField("Date", validators=[DataRequired()])
    collection_type = SelectField("Collection", choices=COLLECTION_TYPE_CHOICES, validators=[DataRequired()])
    amount = StringField("Amount (UGX)", validators=[DataRequired()])
    note = StringField("Note", validators=[Optional(), Length(max=500)])
    reason = TextAreaField("Reason for change", validators=[DataRequired(), Length(min=3, max=500)])


class VoidTransactionForm(FlaskForm):
    reason = TextAreaField("Reason for voiding", validators=[DataRequired(), Length(min=3, max=500)])


class SessionCloseForm(FlaskForm):
    date = DateField("Date", validators=[DataRequired()])
    physical_cash_counted = StringField("Physical cash counted (UGX)", validators=[DataRequired()])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])
