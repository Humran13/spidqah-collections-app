from flask_wtf import FlaskForm
from wtforms import StringField, SelectField, PasswordField, BooleanField, IntegerField, DateField, TextAreaField
from wtforms.validators import DataRequired, Optional, Length, Email, NumberRange

ROLE_CHOICES = [
    ("ADMIN", "Admin"),
    ("DATA_ENTRY", "Data Entry"),
    ("COLLECTOR", "Collector"),
    ("VIEWER", "Viewer / Treasurer"),
]


class UserForm(FlaskForm):
    username = StringField("Username", validators=[DataRequired(), Length(max=80)])
    display_name = StringField("Display name", validators=[DataRequired(), Length(max=150)])
    email = StringField("Email", validators=[Optional(), Email(), Length(max=255)])
    role = SelectField("Role", choices=ROLE_CHOICES, validators=[DataRequired()])
    active = BooleanField("Active", default=True)
    password = PasswordField("Password (leave blank to keep current)", validators=[Optional(), Length(min=8)])


class HistoricalTotalForm(FlaskForm):
    year = IntegerField("Year", validators=[DataRequired(), NumberRange(min=2000, max=2100)])
    month = IntegerField("Month (1-12)", validators=[DataRequired(), NumberRange(min=1, max=12)])
    mukululo_total = StringField("Mukululo official total (UGX)", validators=[DataRequired()])
    friday_total = StringField("Friday official total (UGX)", validators=[DataRequired()])
    sunday_total = StringField("Sunday official total (UGX)", validators=[DataRequired()])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])


class UnlockForm(FlaskForm):
    reason = TextAreaField("Reason for unlocking", validators=[DataRequired(), Length(min=3, max=500)])


class SettingsForm(FlaskForm):
    org_name = StringField("Organization name", validators=[DataRequired(), Length(max=150)])
    go_live_date = DateField("Go-live date", validators=[DataRequired()])


class MergeContributorForm(FlaskForm):
    source_id = SelectField("Duplicate contributor (will be merged away)", coerce=int, validators=[DataRequired()])
    target_id = SelectField("Keep this contributor", coerce=int, validators=[DataRequired()])
