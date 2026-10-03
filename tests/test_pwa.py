import io
import json
import re

import pytest
from PIL import Image

from tests.conftest import login

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_bytes(width, height, color=(200, 30, 30, 255)):
    buf = io.BytesIO()
    Image.new("RGBA", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _post_pwa(client, name="SPIDQAH Collections", short="SPIDQAH", logo=None, filename="logo.png", remove=False):
    data = {"app_name": name, "short_name": short}
    if logo is not None:
        data["logo"] = (io.BytesIO(logo), filename)
    if remove:
        data["remove_logo"] = "y"
    return client.post("/admin/pwa", data=data, content_type="multipart/form-data", follow_redirects=True)


@pytest.fixture()
def upload_dir(app, tmp_path):
    app.config["UPLOAD_DIR"] = str(tmp_path)
    return tmp_path


@pytest.fixture()
def admin_client(client, admin_user, upload_dir):
    login(client, "admin1")
    return client


def _manifest(client):
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert "manifest+json" in resp.headers["Content-Type"]
    return json.loads(resp.data)


class TestManifest:
    def test_manifest_is_valid_json_with_default_names(self, client, upload_dir):
        body = _manifest(client)
        assert body["name"] == "SPIDQAH Collections"
        assert body["short_name"] == "SPIDQAH"
        assert body["display"] == "standalone"
        assert body["start_url"] == "/"
        assert body["scope"] == "/"
        assert body["id"] == "/"
        assert body["theme_color"].startswith("#")
        assert body["background_color"].startswith("#")

    def test_manifest_lists_required_icon_sizes_and_maskable_variants(self, client, upload_dir):
        icons = _manifest(client)["icons"]
        sizes_purposes = {(i["sizes"], i["purpose"]) for i in icons}
        assert ("192x192", "any") in sizes_purposes
        assert ("512x512", "any") in sizes_purposes
        assert ("192x192", "maskable") in sizes_purposes
        assert ("512x512", "maskable") in sizes_purposes
        assert all(i["type"] == "image/png" for i in icons)

    def test_admin_display_name_and_short_name_appear_in_manifest(self, admin_client):
        resp = _post_pwa(admin_client, name="Mukululo Collections App", short="MUKULULO")
        assert resp.status_code == 200
        body = _manifest(admin_client)
        assert body["name"] == "Mukululo Collections App"
        assert body["short_name"] == "MUKULULO"

    def test_manifest_and_icon_urls_are_versioned(self, admin_client):
        before = _manifest(admin_client)["icons"][0]["src"]
        _post_pwa(admin_client, short="NEWSHORT")
        after = _manifest(admin_client)["icons"][0]["src"]
        assert "?v=" in before and "?v=" in after
        assert before != after  # changing settings changes the URL browsers fetch

    def test_html_links_manifest_and_apple_touch_icon(self, admin_client):
        resp = admin_client.get("/")
        html = resp.data.decode()
        assert re.search(r'<link rel="manifest" href="/manifest\.webmanifest\?v=', html)
        assert re.search(r'<link rel="apple-touch-icon" href="/pwa/icons/apple-touch-icon\.png\?v=', html)
        assert 'name="apple-mobile-web-app-capable" content="yes"' in html
        assert 'name="apple-mobile-web-app-title" content="SPIDQAH"' in html

    def test_page_title_uses_configured_app_name(self, admin_client):
        _post_pwa(admin_client, name="Field Collections")
        html = admin_client.get("/").data.decode()
        assert "<title>Dashboard · Field Collections</title>" in html or "Field Collections</title>" in html


class TestAdminAccess:
    def test_non_admin_cannot_view_or_change_pwa_settings(self, client, data_entry_user, upload_dir):
        login(client, "dataentry1")
        assert client.get("/admin/pwa").status_code == 403
        resp = client.post("/admin/pwa", data={"app_name": "Hacked", "short_name": "HACK"})
        assert resp.status_code == 403
        assert _manifest(client)["name"] == "SPIDQAH Collections"

    def test_viewer_cannot_change_pwa_settings(self, client, viewer_user, upload_dir):
        login(client, "viewer1")
        resp = client.post("/admin/pwa", data={"app_name": "Hacked", "short_name": "HACK"})
        assert resp.status_code == 403

    def test_anonymous_cannot_change_pwa_settings(self, client, upload_dir):
        resp = client.post("/admin/pwa", data={"app_name": "Hacked", "short_name": "HACK"}, follow_redirects=False)
        assert resp.status_code in (302, 401, 403)
        assert _manifest(client)["short_name"] == "SPIDQAH"

    def test_admin_settings_change_is_audited(self, admin_client, db):
        from app.models import AuditLog
        _post_pwa(admin_client, name="Audited Name", short="AUD")
        entry = AuditLog.query.filter_by(entity_type="AppSetting").order_by(AuditLog.id.desc()).first()
        assert entry is not None
        assert "Audited Name" in entry.after_json


class TestLogoUpload:
    def test_valid_png_logo_is_accepted_and_generates_icon_set(self, admin_client, upload_dir):
        resp = _post_pwa(admin_client, logo=_png_bytes(400, 400))
        assert b"App settings saved" in resp.data
        assert (upload_dir / "pwa" / "logo-master.png").exists()

        for name, size in (
            ("icon-192.png", 192),
            ("icon-512.png", 512),
            ("icon-maskable-192.png", 192),
            ("icon-maskable-512.png", 512),
            ("apple-touch-icon.png", 180),
            ("favicon-32.png", 32),
        ):
            icon = admin_client.get(f"/pwa/icons/{name}")
            assert icon.status_code == 200, name
            assert icon.data.startswith(PNG_SIGNATURE), name
            assert Image.open(io.BytesIO(icon.data)).size == (size, size), name

    def test_apple_touch_icon_has_no_transparency(self, admin_client, upload_dir):
        _post_pwa(admin_client, logo=_png_bytes(300, 300, color=(0, 0, 0, 0)))
        icon = Image.open(io.BytesIO(admin_client.get("/pwa/icons/apple-touch-icon.png").data))
        assert icon.mode == "RGB"

    def test_logo_keeps_aspect_ratio_with_padding(self, admin_client, upload_dir):
        # A wide 4:1 red logo must stay wide (not stretched into a square).
        _post_pwa(admin_client, logo=_png_bytes(800, 200))
        icon = Image.open(io.BytesIO(admin_client.get("/pwa/icons/icon-512.png").data)).convert("RGB")
        reds = [(x, y) for x in range(512) for y in range(512) if icon.getpixel((x, y))[0] > 150 and icon.getpixel((x, y))[1] < 90]
        xs = [p[0] for p in reds]
        ys = [p[1] for p in reds]
        width, height = max(xs) - min(xs) + 1, max(ys) - min(ys) + 1
        assert width / height == pytest.approx(4.0, rel=0.05)
        assert min(xs) > 0 and max(xs) < 511  # padded, not edge-to-edge

    def test_maskable_logo_stays_inside_safe_zone(self, admin_client, upload_dir):
        _post_pwa(admin_client, logo=_png_bytes(400, 400))
        icon = Image.open(io.BytesIO(admin_client.get("/pwa/icons/icon-maskable-512.png").data)).convert("RGB")
        reds = [(x, y) for x in range(512) for y in range(512) if icon.getpixel((x, y))[0] > 150 and icon.getpixel((x, y))[1] < 90]
        assert reds, "logo not rendered"
        assert min(p[0] for p in reds) >= 512 * 0.1
        assert max(p[0] for p in reds) <= 512 * 0.9

    def test_text_file_renamed_to_png_is_rejected(self, admin_client, upload_dir):
        resp = _post_pwa(admin_client, logo=b"<html><script>alert(1)</script></html>")
        assert b"not a valid image" in resp.data
        assert not (upload_dir / "pwa" / "logo-master.png").exists()

    def test_svg_disguised_as_png_is_rejected(self, admin_client, upload_dir):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="10" height="10"/></svg>'
        resp = _post_pwa(admin_client, logo=svg, filename="logo.png")
        assert b"not a valid image" in resp.data or b"Unsupported" in resp.data
        assert not (upload_dir / "pwa" / "logo-master.png").exists()

    def test_gif_is_rejected_even_when_valid(self, admin_client, upload_dir):
        buf = io.BytesIO()
        Image.new("RGB", (200, 200), (1, 2, 3)).save(buf, format="GIF")
        resp = _post_pwa(admin_client, logo=buf.getvalue(), filename="logo.gif")
        assert b"Unsupported" in resp.data or b"PNG, JPEG or WEBP" in resp.data
        assert not (upload_dir / "pwa" / "logo-master.png").exists()

    def test_oversized_logo_is_rejected(self, admin_client, upload_dir):
        too_big = b"\x89PNG\r\n\x1a\n" + b"0" * (2 * 1024 * 1024 + 10)
        resp = _post_pwa(admin_client, logo=too_big)
        assert b"larger than 2 MB" in resp.data
        assert not (upload_dir / "pwa" / "logo-master.png").exists()

    def test_too_small_logo_is_rejected(self, admin_client, upload_dir):
        resp = _post_pwa(admin_client, logo=_png_bytes(40, 40))
        assert b"Logo dimensions must be between" in resp.data

    def test_extreme_aspect_ratio_is_rejected(self, admin_client, upload_dir):
        resp = _post_pwa(admin_client, logo=_png_bytes(1200, 130))
        assert b"too narrow or too wide" in resp.data

    def test_uploaded_bytes_are_re_encoded_not_served_raw(self, admin_client, upload_dir):
        raw = _png_bytes(256, 256)
        _post_pwa(admin_client, logo=raw)
        stored = (upload_dir / "pwa" / "logo-master.png").read_bytes()
        assert stored != raw  # the stored master is our re-encoded copy
        assert stored.startswith(PNG_SIGNATURE)

    def test_logo_file_is_stored_under_fixed_name_only(self, admin_client, upload_dir):
        _post_pwa(admin_client, logo=_png_bytes(256, 256), filename="../../evil.png")
        names = sorted(p.name for p in (upload_dir / "pwa").iterdir())
        assert names == ["logo-master.png"]

    def test_remove_logo_returns_to_default_icon(self, admin_client, upload_dir):
        _post_pwa(admin_client, logo=_png_bytes(256, 256))
        assert (upload_dir / "pwa" / "logo-master.png").exists()
        _post_pwa(admin_client, remove=True)
        assert not (upload_dir / "pwa" / "logo-master.png").exists()
        assert admin_client.get("/pwa/icons/icon-192.png").status_code == 200

    def test_logo_upload_by_non_admin_is_refused(self, client, collector_user, upload_dir):
        login(client, "collector1")
        resp = _post_pwa(client, logo=_png_bytes(256, 256))
        assert resp.status_code == 403
        assert not (upload_dir / "pwa" / "logo-master.png").exists()


class TestDefaultsAndIcons:
    def test_app_works_and_icons_render_without_custom_logo(self, client, admin_user, upload_dir):
        login(client, "admin1")
        assert client.get("/").status_code == 200
        icon = client.get("/pwa/icons/icon-192.png")
        assert icon.status_code == 200
        assert icon.data.startswith(PNG_SIGNATURE)

    def test_unknown_icon_names_are_not_served(self, client, upload_dir):
        assert client.get("/pwa/icons/..%2Fsecret.png").status_code == 404
        assert client.get("/pwa/icons/not-a-real-icon.png").status_code == 404


class TestServiceWorker:
    def test_service_worker_route_serves_javascript(self, client, upload_dir):
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        assert "javascript" in resp.headers["Content-Type"]
        assert "__SW_CONFIG__" not in resp.data.decode()
        assert resp.headers["Cache-Control"] == "no-cache"

    def test_service_worker_only_caches_static_and_icons(self, client, upload_dir):
        source = client.get("/sw.js").data.decode()
        # Only static assets and icons are stored in the cache.
        assert "STATIC_CACHE" in source
        assert 'request.method !== "GET"' in source
        assert "request.mode === \"navigate\"" in source
        assert 'fetch(request, { cache: "no-store" })' in source
        # Pages/API/reports are never handed to a cache-first or SWR strategy.
        assert source.count("respondWith(staleWhileRevalidate(request))") == 1
        assert source.count("respondWith(cacheFirst(request))") == 1

    def test_service_worker_build_changes_when_settings_change(self, admin_client):
        build_before = re.search(r'"build":\s*"([^"]+)"', admin_client.get("/sw.js").data.decode()).group(1)
        _post_pwa(admin_client, short="BUILDCHG")
        build_after = re.search(r'"build":\s*"([^"]+)"', admin_client.get("/sw.js").data.decode()).group(1)
        assert build_before != build_after

    def test_offline_page_contains_no_financial_data(self, client, upload_dir):
        resp = client.get("/pwa/offline")
        assert resp.status_code == 200
        assert b"You are offline" in resp.data
        assert b"UGX" not in resp.data


class TestCacheHeaders:
    def test_authenticated_financial_pages_are_not_stored(self, admin_client):
        resp = admin_client.get("/reports/contributor")
        assert resp.status_code == 200
        assert "no-store" in resp.headers["Cache-Control"]

    def test_anonymous_login_page_is_not_marked_no_store(self, client, upload_dir):
        resp = client.get("/auth/login")
        assert "no-store" not in resp.headers.get("Cache-Control", "")

    def test_manifest_and_icons_are_revalidated_not_stale(self, client, upload_dir):
        assert client.get("/manifest.webmanifest").headers["Cache-Control"] == "no-cache"
        assert client.get("/pwa/icons/icon-192.png").headers["Cache-Control"] == "no-cache"

    def test_static_assets_are_versioned_by_content(self, client, admin_user, upload_dir):
        login(client, "admin1")
        html = client.get("/").data.decode()
        assert re.search(r'/static/css/app\.css\?v=[0-9a-f]{12}', html)
        assert re.search(r'/static/js/pwa\.js\?v=[0-9a-f]{12}', html)
