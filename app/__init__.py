import os

from flask import Flask

from app.config import get_config
from app.extensions import db, migrate, login_manager, csrf, limiter


def create_app(config_name=None):
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    from app.models import User

    @login_manager.user_loader
    def load_user(user_id):
        return db.session.get(User, int(user_id))

    from app.blueprints.auth.routes import auth_bp
    from app.blueprints.main.routes import main_bp
    from app.blueprints.collections.routes import collections_bp
    from app.blueprints.contributors.routes import contributors_bp
    from app.blueprints.banking.routes import banking_bp
    from app.blueprints.reports.routes import reports_bp
    from app.blueprints.admin.routes import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(collections_bp)
    app.register_blueprint(contributors_bp)
    app.register_blueprint(banking_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(admin_bp)

    from app.utils import format_ugx, to_local
    from app.services.settings import get_go_live_date

    @app.template_filter("ugx")
    def ugx_filter(amount):
        return format_ugx(amount)

    @app.template_filter("localtime")
    def localtime_filter(dt, fmt="%d %b %Y, %H:%M"):
        local = to_local(dt)
        return local.strftime(fmt) if local else ""

    @app.context_processor
    def inject_globals():
        org_name = app.config.get("ORG_NAME", "SPIDQAH")
        try:
            go_live = get_go_live_date()
        except Exception:
            go_live = app.config["DEFAULT_GO_LIVE_DATE"]
        return {"org_name": org_name, "go_live_date": go_live}

    from app.cli import register_cli
    register_cli(app)

    from app.errors import register_error_handlers
    register_error_handlers(app)

    @app.after_request
    def set_security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    return app
