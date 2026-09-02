import unittest

from backend.web.jobs import RegistrationJobCoordinator


class RegistrationJobProgressTests(unittest.TestCase):
    def test_tracks_success_failure_stage_and_email(self):
        manager = RegistrationJobCoordinator()
        manager._target_count = 3
        manager._running = True

        manager._append_log("[*] 开始第 1/3 个账号（Profile Site A） ---")
        manager._append_log("[*] Sub2API 获取邮箱: first@example.com")
        manager._append_log("[+] 注册成功: first@example.com")
        manager._append_log("[-] 失败 [浏览器断开]: failed")

        status = manager.status()
        self.assertEqual(status["completed_count"], 2)
        self.assertEqual(status["success_count"], 1)
        self.assertEqual(status["failure_count"], 1)
        self.assertEqual(status["current_email"], "first@example.com")
        self.assertEqual(status["progress_percent"], 66.7)

    def test_browser_start_failure_counts_multiple_tasks_and_caps_target(self):
        manager = RegistrationJobCoordinator()
        manager._target_count = 2
        manager._append_log("[-] 浏览器启动失败，5 个任务均记为失败: boom")

        status = manager.status()
        self.assertEqual(status["completed_count"], 2)
        self.assertEqual(status["failure_count"], 2)
        self.assertEqual(status["progress_percent"], 100.0)


if __name__ == "__main__":
    unittest.main()
