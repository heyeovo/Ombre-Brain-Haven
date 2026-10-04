import ast
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# 2026-10-04 审计：这两处 return 后面的代码曾悄悄失效（hold 合并写回、breath 心愿浮现）。
CHECKED_FILES = ("server.py", "bucket_manager.py", "gateway.py")
TERMINATORS = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def unreachable_statements(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        for field in ("body", "orelse", "finalbody"):
            block = getattr(node, field, None)
            if not isinstance(block, list):
                continue
            for index, statement in enumerate(block[:-1]):
                if isinstance(statement, TERMINATORS):
                    found.append(f"{path.name}:{block[index + 1].lineno}")
                    break
    return found


class UnreachableCodeTest(unittest.TestCase):
    def test_no_statements_after_return_or_raise(self):
        found = [line for name in CHECKED_FILES for line in unreachable_statements(ROOT / name)]
        self.assertEqual(found, [])

    def test_breath_wish_block_is_reachable(self):
        source = (ROOT / "server.py").read_text(encoding="utf-8")
        wish = source.index('parts.append("=== 还记得这个吗 ===\\n" + wish_result)')
        calm = source.index('return "权重池平静，没有需要处理的记忆。"')
        self.assertLess(wish, calm)


if __name__ == "__main__":
    unittest.main()
