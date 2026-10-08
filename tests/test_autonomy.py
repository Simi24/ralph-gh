import unittest

from conductor.autonomy import decide_merge

ALLOW = (r"^(src/|tests/)", r"^README\.md$")


class DecideMergeTest(unittest.TestCase):
    def allowed(self, *args) -> bool:
        return decide_merge(*args).allowed

    def test_halt_each_pr_never_merges(self) -> None:
        self.assertFalse(self.allowed("halt-each-pr", False, ALLOW, ["src/a.py"]))

    def test_respect_hitl_arch_merges_unless_flagged_or_unknown(self) -> None:
        self.assertTrue(self.allowed("respect-hitl-arch", False, (), None))
        self.assertFalse(self.allowed("respect-hitl-arch", True, (), None))
        self.assertFalse(self.allowed("respect-hitl-arch", None, (), None))

    def test_yolo_needs_every_file_on_the_allowlist(self) -> None:
        self.assertTrue(self.allowed("yolo", False, ALLOW, ["src/a.py", "README.md"]))
        self.assertFalse(self.allowed("yolo", False, ALLOW, ["src/a.py", "install.sh"]))
        self.assertFalse(self.allowed("yolo", False, ALLOW, ["src/a.py", "docs/README.md"]))

    def test_yolo_fails_closed(self) -> None:
        for name, args in [
            ("empty allowlist", (False, (), ["src/a.py"])),
            ("unreadable diff", (False, ALLOW, None)),
            ("empty diff", (False, ALLOW, [])),
            ("invalid regex", (False, ("(",), ["src/a.py"])),
            ("hitl-arch flagged", (True, ALLOW, ["src/a.py"])),
            ("hitl-arch unknown", (None, ALLOW, ["src/a.py"])),
        ]:
            with self.subTest(name):
                self.assertFalse(self.allowed("yolo", *args))

    def test_unknown_mode_never_merges(self) -> None:
        self.assertFalse(self.allowed("YOLO", False, ALLOW, ["src/a.py"]))
        self.assertFalse(self.allowed("", False, ALLOW, ["src/a.py"]))


if __name__ == "__main__":
    unittest.main()
