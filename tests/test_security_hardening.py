import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from _mext.core.config import Config
from _mext.core.constants import API_BASE_URL
from core.update_service import UpdateCheckWorker


RELEASE_URL = "https://github.com/rhodesepass/neo-assetmaker/releases/download/v2.1.3"
INSTALLER_NAME = "ArknightsPassMaker_v2.1.3_Setup.exe"


class UpdateChecksumTests(unittest.TestCase):
    def make_release_data(self, assets):
        return {
            "tag_name": "v2.1.3",
            "name": "v2.1.3",
            "body": "",
            "published_at": "2026-06-06T00:00:00Z",
            "html_url": f"{RELEASE_URL}/notes",
            "assets": assets,
        }

    def test_parse_release_requires_checksum_asset(self):
        worker = UpdateCheckWorker("2.1.2")
        data = self.make_release_data(
            [
                {
                    "name": INSTALLER_NAME,
                    "browser_download_url": f"{RELEASE_URL}/{INSTALLER_NAME}",
                    "size": 100,
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "SHA-256"):
            worker._parse_release_data(data)

    def test_parse_release_accepts_repo_checksum_asset(self):
        worker = UpdateCheckWorker("2.1.2")
        data = self.make_release_data(
            [
                {
                    "name": INSTALLER_NAME,
                    "browser_download_url": f"{RELEASE_URL}/{INSTALLER_NAME}",
                    "size": 100,
                },
                {
                    "name": "SHA256SUMS",
                    "browser_download_url": f"{RELEASE_URL}/SHA256SUMS",
                    "size": 80,
                },
            ]
        )

        release = worker._parse_release_data(data)

        self.assertEqual(release.download_name, INSTALLER_NAME)
        self.assertEqual(release.checksum_url, f"{RELEASE_URL}/SHA256SUMS")

    def test_parse_checksum_text_matches_installer_name(self):
        worker = UpdateCheckWorker("2.1.2")
        digest = "a" * 64

        parsed = worker._parse_checksum_text(
            f"{digest}  {INSTALLER_NAME}\n",
            INSTALLER_NAME,
        )

        self.assertEqual(parsed, digest)


class DownloadPathTests(unittest.TestCase):
    def make_config(self, root: Path) -> Config:
        return Config(
            config_dir=root / "config",
            cache_dir=root / "cache",
            download_dir=root / "downloads",
        )

    def test_download_paths_reject_traversal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.make_config(Path(temp_dir))

            with self.assertRaises(ValueError):
                config.get_final_download_path("../evil.bin")

    def test_download_paths_reject_nested_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = self.make_config(Path(temp_dir))

            with self.assertRaises(ValueError):
                config.get_temp_download_path("nested/evil.bin")


class BakedApiConfigTests(unittest.TestCase):
    def make_config(self, root: Path) -> Config:
        return Config(
            config_dir=root / "config",
            cache_dir=root / "cache",
            download_dir=root / "downloads",
        )

    def test_api_base_url_ignores_process_environment_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                "os.environ",
                {"MM_API_BASE_URL": "http://example.invalid:9999"},
            ):
                config = self.make_config(Path(temp_dir))

        self.assertEqual(config.api_base_url, API_BASE_URL)

    def test_api_base_url_ignores_env_file_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_dir = root / "config"
            config_dir.mkdir(parents=True)
            (config_dir / ".env").write_text(
                "MM_API_BASE_URL=http://example.invalid:9999\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                config = self.make_config(root)

        self.assertEqual(config.api_base_url, API_BASE_URL)

    def test_api_base_url_ignores_project_local_env_override(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / ".env").write_text(
                "MM_API_BASE_URL=http://example.invalid:9999\n",
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                with patch("pathlib.Path.cwd", return_value=root):
                    config = self.make_config(root)

        self.assertEqual(config.api_base_url, API_BASE_URL)


if __name__ == "__main__":
    unittest.main()
