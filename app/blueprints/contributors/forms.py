from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, BooleanField
from wtforms.validators import DataRequired, Optional, Length


class ContributorForm(FlaskForm):
    name = StringField("Name", validators=[DataRequired(), Length(max=150)])
    phone = StringField("Phone", validators=[Optional(), Length(max=30)])
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=1000)])
    active = BooleanField("Active", default=True)
