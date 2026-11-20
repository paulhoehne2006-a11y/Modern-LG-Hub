"""The TV runs an older Python than CI: keep the shipped code parseable as Python 3.7."""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = [os.path.join(ROOT, "custom-launcher", "com.custom.launcher", "launcherd.py")] + [
    os.path.join(ROOT, "gemini-tv-controller", f)
    for f in sorted(os.listdir(os.path.join(ROOT, "gemini-tv-controller"))) if f.endswith(".py")]


class CompatTest(unittest.TestCase):
    def test_python37_syntax(self):
        for path in FILES:
            with self.subTest(path=os.path.basename(path)):
                with open(path, encoding="utf-8") as f:
                    ast.parse(f.read(), filename=path, feature_version=(3, 7))

    def test_no_secrets_in_tracked_backend_files(self):
        for path in FILES:
            with open(path, encoding="utf-8") as f:
                text = f.read()
            self.assertNotRegex(text, r"AIza[0-9A-Za-z_\-]{30,}", path)


if __name__ == "__main__":
    unittest.main()
