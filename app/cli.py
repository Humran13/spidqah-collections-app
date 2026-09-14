import getpass
import sys

import click
from flask.cli import with_appcontext

from app.extensions import db
from app.models import (
    User,
    UserRole,
    Contributor,
    BankAccount,
    FundType,
    CHIKUMI_100_NAME,
)
from app.services.settings import ensure_defaults


def register_cli(app):

    @app.cli.command("init-base-data")
    @with_appcontext
    def init_base_data():
        """Idempotently create structural data every deployment needs:
        app settings, the two bank accounts, and the canonical CHIKUMI 100
        contributor. Safe to run repeatedly. Creates no fabricated
        financial figures."""
        ensure_defaults()

        if not BankAccount.query.filter_by(name="Mukululo Bank Account").first():
            db.session.add(BankAccount(name="Mukululo Bank Account", fund_type=FundType.MUKULULO))
        if not BankAccount.query.filter_by(name="Friday & Sunday Bank Account").first():
            db.session.add(BankAccount(name="Friday & Sunday Bank Account", fund_type=FundType.FRIDAY_SUNDAY))

        normalized = Contributor.normalize(CHIKUMI_100_NAME)
        if not Contributor.query.filter_by(name_normalized=normalized).first():
            db.session.add(Contributor(name=CHIKUMI_100_NAME, name_normalized=normalized))

        db.session.commit()
        click.echo("Base data ensured (settings, bank accounts, CHIKUMI 100).")

    @app.cli.command("create-admin")
    @click.option("--username", default=None)
    @click.option("--email", default=None)
    @click.option("--password", default=None)
    @click.option("--non-interactive", is_flag=True, default=False)
    @with_appcontext
    def create_admin(username, email, password, non_interactive):
        """Create (or promote) the first Admin user.

        Reads from --options, falling back to FIRST_ADMIN_* env vars, and
        finally to interactive prompts unless --non-interactive is set.
        """
        username = username or app.config.get("FIRST_ADMIN_USERNAME")
        email = email or app.config.get("FIRST_ADMIN_EMAIL")
        password = password or app.config.get("FIRST_ADMIN_PASSWORD")

        existing = User.query.filter_by(username=username).first() if username else None
        if existing and existing.role == UserRole.ADMIN and existing.active:
            click.echo(f"Admin user '{username}' already exists - leaving password unchanged.")
            return

        if not existing and User.query.filter_by(role=UserRole.ADMIN).count() > 0 and not username:
            click.echo("An Admin user already exists. Nothing to do.")
            return

        if not username:
            if non_interactive:
                click.echo("Missing --username / FIRST_ADMIN_USERNAME", err=True)
                sys.exit(1)
            username = click.prompt("Admin username")
        if not password:
            if non_interactive:
                click.echo("Missing --password / FIRST_ADMIN_PASSWORD", err=True)
                sys.exit(1)
            password = getpass.getpass("Admin password: ")
            confirm = getpass.getpass("Confirm password: ")
            if password != confirm:
                click.echo("Passwords do not match.", err=True)
                sys.exit(1)

        if len(password) < 8:
            click.echo("Password must be at least 8 characters.", err=True)
            sys.exit(1)

        existing = User.query.filter_by(username=username).first()
        if existing:
            existing.role = UserRole.ADMIN
            existing.active = True
            existing.set_password(password)
            click.echo(f"Existing user '{username}' promoted to Admin and password reset.")
        else:
            user = User(
                username=username,
                email=email,
                display_name=username,
                role=UserRole.ADMIN,
                active=True,
            )
            user.set_password(password)
            db.session.add(user)
            click.echo(f"Admin user '{username}' created.")

        db.session.commit()

    @app.cli.command("seed-demo")
    @with_appcontext
    def seed_demo():
        """Insert clearly-marked DEMO data for local development only.
        Never run this in production."""
        from datetime import date, timedelta
        from app.models import ContributionTransaction, CollectionType, TransactionStatus

        init_base_data.callback()

        demo_names = ["Demo Abbas", "Demo Hajj Kayongo", "Demo Hajjat Ramlah"]
        contributors = []
        for name in demo_names:
            normalized = Contributor.normalize(name)
            c = Contributor.query.filter_by(name_normalized=normalized).first()
            if not c:
                c = Contributor(name=name, name_normalized=normalized, notes="DEMO data")
                db.session.add(c)
            contributors.append(c)

        chikumi = Contributor.query.filter_by(
            name_normalized=Contributor.normalize(CHIKUMI_100_NAME)
        ).first()
        db.session.flush()

        today = date.today()
        demo_rows = [
            (today, CollectionType.MUKULULO, contributors[0], 5000),
            (today, CollectionType.MUKULULO, contributors[1], 5000),
            (today, CollectionType.MUKULULO, contributors[2], 10000),
            (today, CollectionType.MUKULULO, chikumi, 100000),
            (today, CollectionType.FRIDAY, contributors[0], 20000),
            (today - timedelta(days=2), CollectionType.SUNDAY, contributors[1], 15000),
        ]
        admin = User.query.filter_by(role=UserRole.ADMIN).first()
        for d, ctype, contributor, amount in demo_rows:
            db.session.add(ContributionTransaction(
                date=d,
                collection_type=ctype,
                contributor_id=contributor.id,
                amount=amount,
                note="DEMO seed",
                created_by_id=admin.id if admin else None,
                status=TransactionStatus.ACTIVE,
            ))
        db.session.commit()
        click.echo("Demo data seeded.")
