"""Тесты выбора релизного ассета по платформе (torflash.update.assets)."""

from torflash.update.assets import asset_platform, select_platform_asset


def _assets(*names):
    return [{"name": n, "browser_download_url": f"https://x/{n}"} for n in names]


class TestAssetPlatform:
    def test_windows_exe(self):
        assert asset_platform("TorFlash.exe") == "win32"
        assert asset_platform("TorFlash-windows-x86_64.exe") == "win32"

    def test_macos(self):
        assert asset_platform("TorFlash-macos.dmg") == "darwin"
        assert asset_platform("TorFlash.app.zip") == "darwin"

    def test_linux_default(self):
        assert asset_platform("TorFlash") == "linux"
        assert asset_platform("TorFlash-x86_64.AppImage") == "linux"
        assert asset_platform("TorFlash-linux-x86_64") == "linux"


class TestSelectAsset:
    def test_linux_only_release_prefers_bare_binary(self):
        assets = _assets("TorFlash", "TorFlash-x86_64.AppImage",
                         "TorFlash.sha256", "TorFlash.minisig")
        chosen = select_platform_asset(assets, "linux")
        assert chosen["name"] == "TorFlash"

    def test_linux_only_release_no_windows_asset(self):
        assets = _assets("TorFlash", "TorFlash-x86_64.AppImage")
        assert select_platform_asset(assets, "win32") is None
        assert select_platform_asset(assets, "darwin") is None

    def test_multiplatform_release(self):
        assets = _assets(
            "TorFlash", "TorFlash.sha256", "TorFlash.minisig",
            "TorFlash-x86_64.AppImage",
            "TorFlash.exe", "TorFlash.exe.sha256",
            "TorFlash-macos.dmg",
        )
        assert select_platform_asset(assets, "linux")["name"] == "TorFlash"
        assert select_platform_asset(assets, "win32")["name"] == "TorFlash.exe"
        assert select_platform_asset(assets, "darwin")["name"] == "TorFlash-macos.dmg"

    def test_sidecars_never_selected(self):
        assets = _assets("TorFlash.sha256", "TorFlash.minisig")
        assert select_platform_asset(assets, "linux") is None

    def test_unknown_platform_treated_as_linux(self):
        assets = _assets("TorFlash")
        assert select_platform_asset(assets, "freebsd13")["name"] == "TorFlash"
