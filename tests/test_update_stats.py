import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

spec = importlib.util.spec_from_file_location(
    "update_stats", Path(__file__).resolve().parents[1] / "scripts/update_stats.py")
stats = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stats)


class StatisticsTests(unittest.TestCase):
    def sample_stats(self):
        return {"repositories": 12, "contributed_recent": 3, "commits": 70,
                "lines_added": 160000, "lines_removed": 4000, "lines_net": 156000}

    def test_terminal_svg_escapes_profile_text_and_contains_all_statistics(self):
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        readme = readme.replace("Reading, Music", "Reading & <Music>")
        svg = stats.render_terminal(self.sample_stats(), readme)
        root = ET.fromstring(svg)
        content = " ".join(root.itertext())
        self.assertIn("Reading & <Music>", content)
        for value in ("12", "3", "70", "160,000", "4,000", "156,000"):
            self.assertIn(value, content)
        self.assertNotIn("<image", svg)
        self.assertNotIn("<script", svg)

    def test_success_updates_readme_and_terminal_together(self):
        readme_text = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(readme_text)
            with patch.object(stats, "ROOT", root), \
                 patch.dict(os.environ, {"GH_TOKEN": "test-token"}), \
                 patch.object(stats, "collect_stats", return_value=self.sample_stats()):
                stats.main()
            self.assertEqual((root / "README.md").read_text(),
                             stats.replace_block(readme_text, stats.render_block(self.sample_stats())))
            graphic = root / "assets/profile-terminal.svg"
            self.assertIn("160,000", graphic.read_text())
            ET.parse(graphic)
            self.assertFalse((root / "README.md.tmp").exists())
            self.assertFalse(graphic.with_suffix(".svg.tmp").exists())

    def test_render_error_preserves_both_existing_files(self):
        original = f"{stats.START}\nprevious\n{stats.END}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(original)
            (root / "assets").mkdir()
            graphic = root / "assets/profile-terminal.svg"
            graphic.write_text("previous graphic")
            with patch.object(stats, "ROOT", root), \
                 patch.dict(os.environ, {"GH_TOKEN": "test-token"}), \
                 patch.object(stats, "collect_stats", return_value=self.sample_stats()):
                with self.assertRaises(ValueError):
                    stats.main()
            self.assertEqual((root / "README.md").read_text(), original)
            self.assertEqual(graphic.read_text(), "previous graphic")

    def test_shared_commit_is_counted_once(self):
        commits = {"me/a": [{"oid": "shared", "additions": 20, "deletions": 3}],
                   "other/b": [{"oid": "shared", "additions": 20, "deletions": 3},
                               {"oid": "new", "additions": 5, "deletions": 2}]}
        with patch.object(stats, "repositories", side_effect=[
                ("user-id", {"me/a"}), ("user-id", {"me/a", "other/b"})]), \
             patch.object(stats, "authored_commits", side_effect=lambda api, repo, uid: commits[repo]):
            result = stats.collect_stats(None, "me")
        self.assertEqual(result, {"repositories": 1, "contributed_recent": 1,
                                 "commits": 2, "lines_added": 25,
                                 "lines_removed": 5, "lines_net": 20})

    def test_readme_edits_preserve_personal_content(self):
        original = f"My music\n{stats.START}\nold\n{stats.END}\nMy contact\n"
        result = stats.replace_block(original, f"{stats.START}\nnew\n{stats.END}")
        self.assertEqual(result, f"My music\n{stats.START}\nnew\n{stats.END}\nMy contact\n")
        with self.assertRaises(ValueError):
            stats.replace_block(original + stats.START, "new")

    def test_history_is_paginated_against_a_fixed_head(self):
        class API:
            def __init__(self):
                self.variables = []

            def query(self, query, **variables):
                self.variables.append(variables)
                if "defaultBranchRef" in query:
                    return {"repository": {"isPrivate": False,
                            "defaultBranchRef": {"target": {"oid": "fixed-head"}}}}
                page_two = variables["cursor"] == "next"
                return {"repository": {"isPrivate": False, "object": {"history": {
                    "nodes": [{"oid": "b" if page_two else "a", "additions": 1, "deletions": 0}],
                    "pageInfo": {"hasNextPage": not page_two, "endCursor": "next"}}}}}
        api = API()
        result = list(stats.authored_commits(api, "me/repo", "user-id"))
        self.assertEqual([c["oid"] for c in result], ["a", "b"])
        self.assertTrue(all(v["head"] == "fixed-head" and v["id"] == "user-id"
                            for v in api.variables[1:]))

    def test_repository_discovery_paginates_and_filters_private(self):
        class API:
            def query(self, query, **variables):
                second = variables["cursor"] == "next"
                return {"user": {"id": "user-id", "repositories": {
                    "nodes": [{"nameWithOwner": "me/b" if second else "me/a", "isPrivate": False},
                              {"nameWithOwner": "me/private", "isPrivate": True}],
                    "pageInfo": {"hasNextPage": not second, "endCursor": "next"}}}}
        self.assertEqual(stats.repositories(API(), "me"), ("user-id", {"me/a", "me/b"}))

    def test_api_errors_do_not_replace_readme(self):
        original = f"{stats.START}\nlast successful stats\n{stats.END}"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            readme = root / "README.md"
            readme.write_text(original)
            with patch.object(stats, "ROOT", root), \
                 patch.dict(os.environ, {"GH_TOKEN": "test-token"}), \
                 patch.object(stats, "collect_stats", side_effect=RuntimeError("API down")):
                with self.assertRaises(RuntimeError):
                    stats.main()
            self.assertEqual(readme.read_text(), original)
            self.assertFalse((root / "README.md.tmp").exists())


if __name__ == "__main__":
    unittest.main()
