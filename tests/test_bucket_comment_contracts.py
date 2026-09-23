import ast
import tempfile
import unittest
from pathlib import Path

import frontmatter

from bucket_manager import BucketManager


SERVER_PATH = Path(__file__).resolve().parents[1] / "server.py"


def load_server_function(name: str, namespace: dict):
    tree = ast.parse(SERVER_PATH.read_text(encoding="utf-8"))
    function = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    )
    module = ast.Module(body=[function], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(SERVER_PATH), "exec"), namespace)
    return namespace[name]


class BucketCommentStorageTest(unittest.IsolatedAsyncioTestCase):
    async def test_update_comment_preserves_identity_and_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "memory-1.md"
            post = frontmatter.Post("正文", **{
                "comments": [{
                    "id": "ring-1", "content": "旧内容", "author": "言之",
                    "kind": "feel", "source": "comment_bucket",
                }],
                "comment_count": 1,
            })
            path.write_text(frontmatter.dumps(post), encoding="utf-8")
            manager = BucketManager.__new__(BucketManager)
            manager._find_bucket_file = lambda bucket_id: str(path) if bucket_id == "memory-1" else None

            result = await manager.update_comment("memory-1", "ring-1", "新内容")

            self.assertEqual(result["status"], "updated")
            saved = frontmatter.load(path)["comments"][0]
            self.assertEqual(saved["content"], "新内容")
            self.assertEqual(saved["author"], "言之")
            self.assertEqual(saved["kind"], "feel")
            self.assertEqual(saved["source"], "comment_bucket")


class BucketRecallCommentTest(unittest.TestCase):
    def test_rendered_bucket_content_appends_rings_with_read_bucket_labels(self):
        identity = lambda value: str(value or "")
        namespace = {
            "strip_wikilinks": identity,
            "strip_display_temperature_sections": identity,
            "strip_followup_sections": identity,
            "strip_temperature_meaning_lines": identity,
        }
        namespace["_rendered_bucket_comments"] = load_server_function("_rendered_bucket_comments", namespace)
        render = load_server_function("_rendered_bucket_content", namespace)

        result = render({
            "content": "桶正文",
            "metadata": {"comments": [{
                "id": "ring-1", "created": "2026-09-24T01:02:03Z",
                "author": "言之", "content": "后来我仍然在意。",
            }]},
        })

        self.assertIn("桶正文", result)
        self.assertIn("[年轮#1] [ring-1] [日期:2026-09-24] [作者:言之]", result)
        self.assertIn("后来我仍然在意。", result)


if __name__ == "__main__":
    unittest.main()
