import importlib.util
from pathlib import Path
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "package_release", ROOT / "scripts/package_release.py"
)
packaging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packaging)


class PackagingTests(unittest.TestCase):
    def fixture(self, root):
        for name in packaging.release_files(ROOT):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / name).read_bytes())

    def test_allowlist_and_reproducible_archive(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "input"
            self.fixture(root)
            (root / ".env").write_text("PRIVATE_FIXTURE=must-not-export")
            (root / "unreviewed.py").write_text("# not approved")
            first = packaging.build(root, base / "first")
            packaging.build(root, base / "second")
            self.assertFalse((first / ".env").exists())
            self.assertFalse((first / "unreviewed.py").exists())
            for artifact in (base / "first").glob("*.tar.gz"):
                self.assertEqual(
                    artifact.read_bytes(),
                    (base / "second" / artifact.name).read_bytes(),
                )
            with tarfile.open(base / "first/commitments-0.2.0.tar.gz") as tar:
                self.assertEqual(
                    set(tar.getnames()),
                    {
                        f"commitments/{n}"
                        for n in packaging.plugin_files(packaging.release_files(root))
                    },
                )
            with self.assertRaises(FileExistsError):
                packaging.build(root, base / "first")

    def test_symlink_cannot_export_external_content(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "input"
            self.fixture(root)
            secret = base / "private.txt"
            secret.write_text("private fixture")
            (root / "README.md").unlink()
            (root / "README.md").symlink_to(secret)
            with self.assertRaisesRegex(ValueError, "Symlink"):
                packaging.build(root, base / "output")
            self.assertFalse((base / "output").exists())

    def test_missing_file_fails_before_export(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root = base / "input"
            self.fixture(root)
            (root / "desktop/plugin.js").unlink()
            with self.assertRaisesRegex(ValueError, "Missing release file"):
                packaging.build(root, base / "output")
            self.assertFalse((base / "output").exists())
