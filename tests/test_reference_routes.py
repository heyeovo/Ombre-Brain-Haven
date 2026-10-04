import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = re.compile(r"(?<![\w/])(/(?:api|gateway)/[A-Za-z0-9_\-/{}.]+)")
DOC_METHOD_PATH = re.compile(r"\b(GET|POST|PUT|PATCH|DELETE)\s+(/(?:api|gateway)/[A-Za-z0-9_\-/{}.]+)")


def normalize(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path.rstrip("/"))


def registered_routes() -> dict[str, set[str]]:
    """路径 → 注册的方法；没写 methods 的路由记为空集合，只核对路径。"""
    code = (ROOT / "server.py").read_text(encoding="utf-8") + (ROOT / "gateway.py").read_text(encoding="utf-8")
    routes: dict[str, set[str]] = {}
    for match in re.finditer(r'(?:custom_route|Route)\(\s*"([^"]+)"', code):
        routes.setdefault(normalize(match.group(1)), set())
    for match in re.finditer(r'(?:custom_route|Route)\(\s*"([^"]+)"[^)\n]*?methods=\[([^\]]*)\]', code):
        routes[normalize(match.group(1))].update(re.findall(r'"([A-Z]+)"', match.group(2)))
    return routes


def documented_blocks() -> list[str]:
    # 只核对代码块里的路由清单；正文里提到的 Dashboard 代理路径不算 Haven 路由。
    text = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    return re.findall(r"```[^\n]*\n(.*?)```", text, flags=re.S)


class ReferenceRoutesTest(unittest.TestCase):
    def test_documented_routes_exist(self):
        routes = registered_routes()
        paths = {path for block in documented_blocks() for path in DOC_PATH.findall(block)}
        missing = sorted(path for path in paths if normalize(path) not in routes)
        self.assertEqual(missing, [], "docs/reference.md 列了代码里不存在的路由")

    def test_documented_methods_match(self):
        routes = registered_routes()
        wrong = []
        for block in documented_blocks():
            for method, path in DOC_METHOD_PATH.findall(block):
                registered = routes.get(normalize(path))
                if registered and method not in registered:
                    wrong.append(f"{method} {path}（代码里是 {'/'.join(sorted(registered))}）")
        self.assertEqual(wrong, [], "docs/reference.md 的路由方法和代码不一致")


if __name__ == "__main__":
    unittest.main()
