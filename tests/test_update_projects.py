import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import update_projects as sync


def repository(name, **changes):
    repo = {
        "name": name, "owner": {"login": "getl-x"},
        "private": False, "visibility": "public", "fork": False,
        "archived": False, "disabled": False, "language": "Python",
        "created_at": "2026-10-01T00:00:00Z", "description": "一个实用工具",
    }
    repo.update(changes)
    return repo


class ProjectSyncTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "owner": "getl-x", "excluded_repositories": [],
            "featured": [{"repository": "daybook", "title": "Daybook"}],
        }

    def test_new_repository_is_discovered_without_config_change(self):
        selected = sync.select_repositories([
            repository("tiny-site", created_at="2026-08-01T00:00:00Z"),
            repository("new-project"), repository("daybook"),
        ], self.settings)
        self.assertEqual([repo["name"] for repo, _ in selected],
                         ["daybook", "new-project", "tiny-site"])
        self.assertIn("new-project</a>", sync.render_projects(selected, "getl-x"))

    def test_private_fork_archived_foreign_and_profile_repos_are_excluded(self):
        repos = [repository("keep"), repository("getl-x"),
                 repository("private", private=True),
                 repository("internal", visibility="internal"),
                 repository("fork", fork=True),
                 repository("archived", archived=True),
                 repository("disabled", disabled=True),
                 repository("foreign", owner={"login": "someone-else"})]
        self.assertEqual([repo["name"] for repo, _ in sync.select_repositories(repos, self.settings)], ["keep"])

    def test_exclusion_list_also_applies_to_featured_cards(self):
        self.settings["excluded_repositories"] = ["DAYBOOK"]
        self.assertEqual(sync.select_repositories([repository("daybook")], self.settings), [])

    def test_curated_copy_and_order_are_preserved(self):
        self.settings["featured"][0].update(description="定制简介", subtitle="日记", tags="离线 · 自托管")
        selected = sync.select_repositories([repository("daybook", description="仓库简介")], self.settings)
        content = sync.render_projects(selected, "getl-x")
        self.assertIn("定制简介", content)
        self.assertIn("Daybook</a> · 日记", content)
        self.assertNotIn("仓库简介", content)

    def test_untrusted_metadata_is_escaped_and_missing_description_has_fallback(self):
        card = sync.render_card(repository("demo", description='<img src=x onerror="alert(1)"> &'), {}, "getl-x")
        self.assertIn("&lt;img", card)
        self.assertNotIn('<img src=x', card)
        self.assertIn("项目简介待补充", sync.render_card(repository("demo", description=None), {}, "getl-x"))

    def test_duplicate_api_entries_do_not_create_duplicate_cards(self):
        selected = sync.select_repositories([repository("demo"), repository("demo")], self.settings)
        self.assertEqual(len(selected), 1)

    def test_order_is_stable_regardless_of_api_order_or_update_times(self):
        repos = [repository("b", updated_at="2026-10-05"), repository("a", updated_at="2026-10-01")]
        self.assertEqual(sync.select_repositories(repos, self.settings),
                         sync.select_repositories(list(reversed(repos)), self.settings))

    def test_removing_or_archiving_a_repository_removes_its_card(self):
        content = sync.render_projects(sync.select_repositories([
            repository("daybook"), repository("old-project", archived=True),
        ], self.settings), "getl-x")
        previous = "intro\n" + sync.START + "\nold-project\n" + sync.END + "\nfooter"
        updated = sync.replace_project_section(previous, content)
        self.assertNotIn("old-project", updated)
        self.assertTrue(updated.startswith("intro\n" + sync.START))
        self.assertTrue(updated.endswith(sync.END + "\nfooter"))
        self.assertEqual(sync.replace_project_section(updated, content), updated)

    def test_missing_duplicate_or_reversed_markers_fail_safely(self):
        for text in ["no markers", sync.START + sync.START + sync.END, sync.END + sync.START]:
            with self.subTest(text=text), self.assertRaises(ValueError):
                sync.replace_project_section(text, "cards")

    def test_pagination_fetches_more_than_one_hundred_repositories(self):
        batches = [[repository(f"repo-{i}") for i in range(100)], [repository("repo-100")]]
        with patch.object(sync, "urlopen", side_effect=[io.StringIO(json.dumps(batch)) for batch in batches]) as request:
            repos = sync.fetch_repositories("getl-x")
        self.assertEqual(len(repos), 101)
        self.assertIn("page=2", request.call_args[0][0].full_url)

    def test_api_failure_does_not_return_partial_repository_list(self):
        first = io.StringIO(json.dumps([repository(f"repo-{i}") for i in range(100)]))
        error = HTTPError("https://api.github.com", 403, "Forbidden", {}, None)
        with patch.object(sync, "urlopen", side_effect=[first, error]):
            with self.assertRaisesRegex(RuntimeError, "HTTP 403"):
                sync.fetch_repositories("getl-x")

    def test_monthly_state_refresh_works_without_changing_profile_copy(self):
        repos = [repository("daybook")]
        content = sync.render_projects(sync.select_repositories(repos, self.settings), "getl-x")
        old_readme = "intro\n" + sync.START + "\n" + content + "\n" + sync.END + "\nfooter"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "profile-projects.json").write_text(json.dumps(self.settings), encoding="utf-8")
            (root / "README.md").write_text(old_readme, encoding="utf-8")
            (root / ".github").mkdir()
            state = root / ".github" / "project-sync-state.json"
            state.write_text('{"verified_month":"2026-09","public_projects":1}', encoding="utf-8")
            current_state = '{"verified_month":"2026-10","public_projects":1}\n'
            with patch.object(sync, "ROOT", root), patch.object(sync, "fetch_repositories", return_value=repos), patch.object(sync, "render_sync_state", return_value=current_state):
                self.assertEqual(sync.main(["--check"]), 1)
                self.assertIn("2026-09", state.read_text())
                self.assertEqual(sync.main([]), 0)
                self.assertEqual(sync.main(["--check"]), 0)
            self.assertEqual((root / "README.md").read_text(), old_readme)
            self.assertEqual(state.read_text(), current_state)


if __name__ == "__main__":
    unittest.main()
