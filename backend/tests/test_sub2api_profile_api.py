import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.registration.store import RegistrationRepository


class Sub2apiProfileApiTests(unittest.TestCase):
    """Phase 2：Profile 端点的状态码映射（400/404/409）。

    通过持久 patcher 让 application._gr() 返回受控仓库、_valid_session 恒真，
    从而在 TestClient 请求期间稳定走受控路径；不触发 create_app 的 startup 事件。
    """

    def setUp(self):
        from backend.web import application
        from fastapi.testclient import TestClient

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = RegistrationRepository(Path(self._tmp.name) / "results.sqlite3")

        gr_mock = mock.Mock()
        gr_mock.get_registration_repository.return_value = self.store

        self._gr_patcher = mock.patch.object(application, "_gr", return_value=gr_mock)
        self._sess_patcher = mock.patch.object(application, "_valid_session", return_value=True)
        self._gr_patcher.start()
        self._sess_patcher.start()
        self.addCleanup(self._gr_patcher.stop)
        self.addCleanup(self._sess_patcher.stop)

        self.client = TestClient(application.create_app())

    def test_profile_crud_endpoint_roundtrip(self):
        sites = self.client.get("/api/sub2api/sites")
        self.assertEqual(sites.status_code, 200)
        self.assertEqual(
            {item["key"] for item in sites.json()["sites"]},
            {
                "true-sota",
                "ctai",
                "bmapi",
                "xxcy",
                "sharezzz",
                "zaion",
                "lianjieai",
            },
        )
        bmapi = next(item for item in sites.json()["sites"] if item["key"] == "bmapi")
        self.assertEqual(
            bmapi["email_suffix_whitelist"],
            ["qq.com", "gmail.com", "126.com", "163.com", "*.edu.cn"],
        )
        catalog = {item["key"]: item for item in sites.json()["sites"]}
        self.assertEqual(catalog["xxcy"]["email_suffix_whitelist"], ["qq.com"])
        self.assertEqual(
            catalog["sharezzz"]["email_suffix_whitelist"],
            ["qq.com", "163.com", "gmail.com"],
        )
        self.assertEqual(catalog["zaion"]["email_suffix_whitelist"], ["qq.com"])
        self.assertEqual(catalog["lianjieai"]["email_suffix_whitelist"], ["*"])
        self.assertEqual(catalog["lianjieai"]["default_aff_code"], "Z4NPCESZBC9K")
        created = self.client.post(
            "/api/sub2api/profiles",
            json={"name": "Site A", "site_key": "true-sota"},
        )
        self.assertEqual(created.status_code, 200)
        profile = created.json()["profile"]
        profile_id = profile["id"]
        self.assertEqual(profile["aff_code"], "U4Z83MFFZ9LP")

        listed = self.client.get("/api/sub2api/profiles")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(len(listed.json()["profiles"]), 1)
        self.assertFalse(listed.json()["profiles"][0]["in_use"])

        # 非法 URL → 400
        bad = self.client.post(
            "/api/sub2api/profiles",
            json={"name": "Bad", "site_key": "unknown"},
        )
        self.assertEqual(bad.status_code, 400)
        custom_url = self.client.post(
            "/api/sub2api/profiles",
            json={"name": "Custom", "site_key": "true-sota", "register_url": "https://unverified.example/register"},
        )
        self.assertEqual(custom_url.status_code, 422)

        # 名称冲突 → 400（store 层业务错误）
        conflict = self.client.post(
            "/api/sub2api/profiles",
            json={"name": "SITE a", "site_key": "ctai"},
        )
        self.assertEqual(conflict.status_code, 400)

        # 更新成功
        updated = self.client.put(
            f"/api/sub2api/profiles/{profile_id}",
            json={"name": "Site A2"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["profile"]["name"], "Site A2")
        self.assertIn("qq.com", updated.json()["profile"]["whitelist"])

        custom_whitelist = self.client.put(
            f"/api/sub2api/profiles/{profile_id}",
            json={"email_domain_whitelist": "example.com"},
        )
        self.assertEqual(custom_whitelist.status_code, 422)

        # 不存在的 Profile → 404
        missing = self.client.put("/api/sub2api/profiles/9999", json={"name": "X"})
        self.assertEqual(missing.status_code, 404)

        # 未使用的 Profile 可删除
        deleted = self.client.delete(f"/api/sub2api/profiles/{profile_id}")
        self.assertEqual(deleted.status_code, 200)
        # 再删 → 404
        again = self.client.delete(f"/api/sub2api/profiles/{profile_id}")
        self.assertEqual(again.status_code, 404)

    def test_used_profile_origin_change_returns_409(self):
        profile_id = self.client.post(
            "/api/sub2api/profiles",
            json={"name": "Used", "site_key": "true-sota"},
        ).json()["profile"]["id"]
        self.store.add_result(
            {"email": "u@x.com", "status": "failure", "profile_id": profile_id}
        )
        resp = self.client.put(
            f"/api/sub2api/profiles/{profile_id}",
            json={"site_key": "ctai"},
        )
        self.assertEqual(resp.status_code, 409)
        # 删除已使用 Profile → 409（提示改用禁用）
        deleted = self.client.delete(f"/api/sub2api/profiles/{profile_id}")
        self.assertEqual(deleted.status_code, 409)
        # 禁用是允许的出路
        disabled = self.client.put(
            f"/api/sub2api/profiles/{profile_id}", json={"enabled": False}
        )
        self.assertEqual(disabled.status_code, 200)
        self.assertFalse(disabled.json()["profile"]["enabled"])


if __name__ == "__main__":
    unittest.main()
