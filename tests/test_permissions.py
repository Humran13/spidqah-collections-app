from tests.conftest import login


class TestAuthentication(object):
    def test_anonymous_user_redirected_to_login(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code in (301, 302)
        assert "/auth/login" in resp.headers["Location"]

    def test_valid_login_reaches_dashboard(self, client, admin_user):
        resp = login(client, "admin1")
        assert resp.status_code == 200
        assert b"Dashboard" in resp.data or b"Monthly Dashboard" in resp.data

    def test_invalid_password_rejected(self, client, admin_user):
        resp = client.post("/auth/login", data={"username": "admin1", "password": "wrong"}, follow_redirects=True)
        assert b"Invalid username or password" in resp.data


class TestRolePermissions(object):
    def test_collector_can_enter_collections_but_not_historical_backentry(self, client, collector_user):
        login(client, "collector1")
        resp = client.get("/collections/entry")
        assert resp.status_code == 200

        resp = client.get("/collections/historical-entry")
        assert resp.status_code == 403

    def test_viewer_cannot_enter_collections(self, client, viewer_user):
        login(client, "viewer1")
        resp = client.get("/collections/entry")
        assert resp.status_code == 403

    def test_viewer_can_view_reports(self, client, viewer_user):
        login(client, "viewer1")
        resp = client.get("/reports/")
        assert resp.status_code == 200

    def test_data_entry_can_use_historical_backentry(self, client, data_entry_user):
        login(client, "dataentry1")
        resp = client.get("/collections/historical-entry")
        assert resp.status_code == 200

    def test_data_entry_cannot_access_admin(self, client, data_entry_user):
        login(client, "dataentry1")
        resp = client.get("/admin/")
        assert resp.status_code == 403

    def test_collector_cannot_access_admin(self, client, collector_user):
        login(client, "collector1")
        resp = client.get("/admin/users")
        assert resp.status_code == 403

    def test_admin_can_access_admin_area(self, client, admin_user):
        login(client, "admin1")
        resp = client.get("/admin/")
        assert resp.status_code == 200

    def test_only_admin_can_reach_settings(self, client, data_entry_user):
        login(client, "dataentry1")
        resp = client.get("/admin/settings")
        assert resp.status_code == 403
