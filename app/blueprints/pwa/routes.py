"""Public PWA endpoints: manifest, service worker, generated icons, offline page.

None of these expose financial data. The manifest and icons contain only the
Admin-chosen display names and a logo; the service worker contains only the
build configuration. They are public so browsers can install the app before
login.
"""
import json
import os

from flask import Blueprint, Response, abort, current_app, url_for

from app.services import pwa as pwa_service

pwa_bp = Blueprint("pwa", __name__)

_APP_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SW_PATH = os.path.join(_APP_DIR, "pwa", "service_worker.js")
_OFFLINE_PATH = os.path.join(_APP_DIR, "pwa", "offline.html")


def _no_cache(response):
    response.headers["Cache-Control"] = "no-cache"
    return response


@pwa_bp.route("/manifest.webmanifest")
def manifest():
    settings = pwa_service.get_pwa_settings()
    version = settings["version"]
    home = url_for("main.dashboard")  # respects any reverse-proxy sub-path
    icons = []
    for name, (size, maskable) in (
        ("icon-192.png", (192, False)),
        ("icon-512.png", (512, False)),
        ("icon-maskable-192.png", (192, True)),
        ("icon-maskable-512.png", (512, True)),
    ):
        icons.append({
            "src": url_for("pwa.icon", name=name, v=version),
            "sizes": f"{size}x{size}",
            "type": "image/png",
            "purpose": "maskable" if maskable else "any",
        })
    body = {
        "id": home,
        "name": settings["app_name"],
        "short_name": settings["short_name"],
        "description": f"{settings['app_name']} - collections and reconciliation",
        "start_url": home,
        "scope": home,
        "display": "standalone",
        "background_color": pwa_service.BACKGROUND_COLOR,
        "theme_color": pwa_service.THEME_COLOR,
        "lang": "en",
        "icons": icons,
    }
    response = Response(json.dumps(body, ensure_ascii=False), mimetype="application/manifest+json")
    return _no_cache(response)


@pwa_bp.route("/sw.js")
def service_worker():
    with open(_SW_PATH, encoding="utf-8") as fh:
        source = fh.read()
    config = {
        "build": f"{pwa_service.get_pwa_settings()['version']}-{current_app.config['ASSET_VERSION']}",
        "staticPrefix": url_for("static", filename=""),
        "iconPrefix": url_for("pwa.icon", name="icon-192.png").rsplit("/", 1)[0] + "/",
        "offlineUrl": url_for("pwa.offline"),
    }
    # json.dumps of a fixed dict of server-generated strings: safe to inline.
    source = source.replace("__SW_CONFIG__", json.dumps(config))
    response = Response(source, mimetype="text/javascript")
    response.headers["Service-Worker-Allowed"] = url_for("main.dashboard")
    return _no_cache(response)


@pwa_bp.route("/pwa/icons/<name>")
def icon(name):
    if name not in pwa_service.ICON_SPECS:
        abort(404)
    data = pwa_service.render_icon(name)
    response = Response(data, mimetype="image/png")
    return _no_cache(response)


@pwa_bp.route("/pwa/offline")
def offline():
    with open(_OFFLINE_PATH, encoding="utf-8") as fh:
        response = Response(fh.read(), mimetype="text/html")
    return _no_cache(response)
