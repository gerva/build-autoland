import unittest
import tempfile
import shutil
import mock
import requests

import autoland.bugzilla
from autoland.bugzilla import Bugzilla
import autoland.errors


class TestAutolandBugzilla(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    @staticmethod
    def test_request_json_data():
        with mock.patch("requests.request", autospec=True) as m_requests:
            bz = Bugzilla(api_url="htttp://fake")
            bz.request_json("url", data={"a": "b", "c": False, "d": None})
            m_requests.assert_called_with(
                "GET", "url", params=None, verify=True,
                data='{"a": "b", "c": false, "d": null}',
                headers={'Accept': 'application/json',
                         'Content-Type': 'application/json'})

    @staticmethod
    def test_request_json_headers():
        with mock.patch("requests.request", autospec=True) as m_requests:
            bz = Bugzilla(api_url="htttp://fake")
            bz.request_json("url", headers={'Accept': 'application/json'})
            m_requests.assert_called_with(
                "GET", "url", headers={'Accept': 'application/json'},
                data=None, params=None, verify=True)

    def test_request_json_decode_failure(self):
        with mock.patch("requests.request", autospec=True) as m_requests:
            r = m_requests.return_value
            r.json.side_effect = ValueError()
            bz = Bugzilla(api_url="http://fake")
            with self.assertRaises(ValueError):
                bz.request_json("meh")

    def test_request_json_http_failure(self):
        with mock.patch("requests.request", autospec=True) as m_requests:
            m_requests.side_effect = requests.RequestException()
            bz = Bugzilla(api_url="http://fake")
            with self.assertRaises(requests.RequestException):
                bz.request_json("meh")

    @staticmethod
    def test_request():
        with mock.patch("autoland.bugzilla.Bugzilla.request_json",
                        autospec=True) as r_json:
            bz = Bugzilla(api_url="htttp://fake", username="user",
                          password="pass")
            bz.request("/path", data="data", method="PUT")
            r_json.assert_called_with(bz, "htttp://fake/path", method="PUT",
                                      data="data", params={"username": "user",
                                                           "password": "pass"})

    def test_get_patch(self):
        bz = Bugzilla(api_url="http://fake", attachment_url="fake")
        with mock.patch("requests.get", autospec=True) as get:
            r = get.return_value
            content = "Super patch"
            r.content = content
            patch_file = bz.download_patch(patch_id=1111, path=self.tmpdir)
            self.assertEquals(content, open(patch_file).read())

    def test_get_patch_invalid_patch(self):
        bz = Bugzilla(api_url="http://fake", attachment_url="fake")
        with mock.patch("requests.get", autospec=True) as get:
            r = get.return_value
            r.content = "The attachment id 1111 is invalid"
            with self.assertRaises(autoland.errors.InvalidAttachment):
                bz.download_patch(patch_id=1111, path=self.tmpdir)

    def test_get_patch_http_failure(self):
        bz = Bugzilla(api_url="http://fake", attachment_url="fake")
        with mock.patch("requests.get", autospec=True) as get:
            r = get.return_value
            r.raise_for_status.side_effect = requests.HTTPError()
            with self.assertRaises(requests.RequestException):
                bz.download_patch(patch_id=1111, path=self.tmpdir)

    def test_get_patch_overwrite_refetches(self):
        bz = Bugzilla(api_url="http://fake", attachment_url="fake")
        with mock.patch("requests.get", autospec=True) as get:
            r = get.return_value
            content = "Super patch"
            r.content = content
            bz.download_patch(patch_id=1111, path=self.tmpdir)
            r.raise_for_status.side_effect = requests.HTTPError()
            with self.assertRaises(requests.RequestException):
                bz.download_patch(patch_id=1111, path=self.tmpdir,
                             overwrite_patch=True)

    def test_get_user_info(self):
        with mock.patch("autoland.bugzilla.Bugzilla.request",
                        autospec=True) as r:
            r.return_value = {
                "real_name": "Real User [:user]",
                "ref": "https://api-dev.bugzilla.mozilla.org/latest/user/1111",
                "name": "user@example.com",
                "id": 1111
            }
            bz = Bugzilla(api_url="fake")
            u = bz.get_user_info("user@example.com")
            self.assertDictEqual(
                u, {"name": "Real User", "email": "user@example.com"})
            r.return_value = {
                "real_name": "Real User [:user]",
                "ref": "https://api-dev.bugzilla.mozilla.org/latest/user/1111",
                "name": "user@example.com",
                "email": "user@example.net",
                "id": 1111
            }
            u = bz.get_user_info("user@example.com")
            self.assertDictEqual(
                u, {"name": "Real User", "email": "user@example.net"},
                "Should return 'email' field if set")
            self.assertEquals(bz.get_user_info(""), None,
                              "Should return None if no email is passed")
            r.return_value = {
                "unreal_name": "Real User [:user]",
                "ref": "https://api-dev.bugzilla.mozilla.org/latest/user/1111",
                "name": "user@example.com",
                "email": "user@example.net",
                "id": 1111
            }
            u = bz.get_user_info("user@example.com")
            self.assertEquals(bz.get_user_info(""), None,
                              "Should return None if no real_name set")

    def test_bugs_from_comments(self):
        bz = Bugzilla(api_url="fake")
        ret = bz.bugs_from_comments("this is a comment with no bug mentioned")
        self.assertEquals(ret, [])
        ret = bz.bugs_from_comments("this comment is about bug 10480 only")
        self.assertEquals(ret, [10480])
        ret = bz.bugs_from_comments("comment is about bugs 10480, 10411")
        self.assertEquals(ret, [10480, 10411])
        ret = bz.bugs_from_comments("comment about b10480")
        self.assertEquals(ret, [10480])
        ret = bz.bugs_from_comments("comment about 10480")
        self.assertEquals(ret, [])

    @staticmethod
    def test_notify_bug():
        bz = Bugzilla(api_url="fake")
        with mock.patch("autoland.bugzilla.Bugzilla.request",
                        autospec=True) as r:
            bz.notify_bug(bug_id=1111, message="msg")
            r.assert_called_with(bz, path="bug/1111/comment",
                                 data={"text": "msg", "is_private": False},
                                 method="POST")

    def test_has_comment(self):
        bz = Bugzilla(api_url="fake")
        with mock.patch("autoland.bugzilla.Bugzilla.request",
                        autospec=True) as r:
            r.return_value = {'comments': [{'text': 'comment text'}]}
            self.assertTrue(bz.has_comment(bug_id=4, comment='comment text'))
            self.assertFalse(bz.has_comment(bug_id=5, comment='comment'))
            r.return_value = {}
            self.assertFalse(bz.has_comment(bug_id=4, comment='comment text'))

    def test_get_wating_auoland_bugs(self):
        bz = Bugzilla(api_url="http://fake", attachment_url="fake")
        with mock.patch("autoland.bugzilla.Bugzilla.request_json",
                        autospec=True) as r_json:
            r_json.return_value = {"result": "pass"}
            self.assertEquals("pass", bz.get_wating_auoland_bugs())
            r_json.return_value = {"error": "yes"}
            self.assertEquals([], bz.get_wating_auoland_bugs())

    @staticmethod
    def test_autoland_update_attachment():
        bz = Bugzilla(api_url="http://fake", webui_login="user",
                      webui_password="pass", webui_url="webui")
        with mock.patch("autoland.bugzilla.Bugzilla.request_json",
                        autospec=True) as r_json:
            bz.update_attachment({"a": "b", "c": None})
            r_json.assert_called_with(
                bz, "webui", method="POST",
                data={'version': 1.1,
                      'params': {'a': 'b', 'c': None, 'Bugzilla_login': 'user',
                                 'Bugzilla_password': 'pass'},
                      'method': 'TryAutoLand.update'})

    @staticmethod
    def test_get_bug():
        bz = Bugzilla(api_url="fake")
        with mock.patch("autoland.bugzilla.Bugzilla.request",
                        autospec=True) as r:
            bz.get_bug(1111)
            r.assert_called_with(bz, "bug/1111")

    def test_get_reviews(self):
        bz = Bugzilla(api_url="fake")
        self.assertEquals([], bz.get_reviews({}))
        with mock.patch("autoland.bugzilla.Bugzilla.get_user_info",
                        autospec=True) as u_info:
            u_info.return_value = "user@example.com"
            r = {"type": "review", "reviewer": "user@example.com",
                 "result": "ok"}
            a = {"flags": [{"name": "review", "status": "ok",
                            "setter": {"name": "Real User"}}]}
            self.assertEquals([r], bz.get_reviews(a))

    def test_get_approvals(self):
        bz = Bugzilla(api_url="fake")
        self.assertEquals([], bz.get_approvals({}))
        with mock.patch("autoland.bugzilla.Bugzilla.get_user_info",
                        autospec=True) as u_info:
            u_info.return_value = "user@example.com"
            r = {"type": "mozilla-beta", "approver": "user@example.com",
                 "result": "ok"}
            a = {"flags": [{"name": "approval-mozilla-beta", "status": "ok",
                 "setter": {"name": "Real User"}}, {"a": "x"}]}
            self.assertEquals([r], bz.get_approvals(a))

    def test_check_patch_approvals(self):
        bz = Bugzilla(api_url="fake")
        with self.assertRaises(autoland.errors.ApprovalException):
            bz.check_patch_approvals({'id': 1}, "branch", "perms")
        p = {"id": 1, "approvals": [{"type": "release", "result": "+",
                                     "approver": {"email": "user@m"}}]}
        bz.ldap = mock.MagicMock()
        bz.ldap.in_ldap_group.return_value = False
        with self.assertRaises(autoland.errors.ApprovalException):
            bz.check_patch_approvals(p, "release", "perms")
        bz.ldap.in_ldap_group.return_value = True
        self.assertEquals(None, bz.check_patch_approvals(p, "release",
                                                         "perms"))
        with self.assertRaises(autoland.errors.ApprovalException):
            bz.check_patch_approvals(p, "beta", "perms")
        p["approvals"][0]["result"] = "?"
        with self.assertRaises(autoland.errors.ApprovalException):
            bz.check_patch_approvals(p, "release", "perms")
        # Multpiple approvals
        p = {"id": 1,
             "approvals": [
                 {"type": "release", "result": "+",
                  "approver": {"email": "u@m"}},
                 {"type": "release", "result": "?",
                  "approver": {"email": "u@m"}},
             ]}
        with self.assertRaises(autoland.errors.ApprovalException):
            bz.check_patch_approvals(p, "release", "perms")
        p["approvals"][0]["result"] = "-"
        with self.assertRaises(autoland.errors.ApprovalException):
            bz.check_patch_approvals(p, "release", "perms")

    def test_check_patch_reviews(self):
        bz = Bugzilla(api_url="fake")
        with self.assertRaises(autoland.errors.ReviewException):
            bz.check_patch_reviews({'id': 1}, "perms")
        bz.ldap = mock.MagicMock()
        bz.ldap.in_ldap_group.return_value = False
        p = {"id": 1,
             "reviews": [
                 {"result": "+", "reviewer": {"email": "u@m"}},
             ]}
        with self.assertRaises(autoland.errors.ReviewException):
            bz.check_patch_reviews(p, "perms")

        bz.ldap.in_ldap_group.return_value = True
        self.assertEquals(None, bz.check_patch_reviews(p, "perms"))
        p["reviews"][0]["result"] = "?"
        with self.assertRaises(autoland.errors.ReviewException):
            bz.check_patch_reviews(p, "perms")
        # Multpiple approvals
        p = {"id": 1,
             "reviews": [
                 {"result": "+", "reviewer": {"email": "u@m"}},
                 {"result": "+", "reviewer": {"email": "u@m"}},
             ]}
        self.assertEquals(None, bz.check_patch_reviews(p, "perms"))
        p["reviews"][0]["result"] = "-"
        with self.assertRaises(autoland.errors.ReviewException):
            bz.check_patch_reviews(p, "perms")
        p["reviews"][0]["result"] = "?"
        with self.assertRaises(autoland.errors.ReviewException):
            bz.check_patch_reviews(p, "perms")
