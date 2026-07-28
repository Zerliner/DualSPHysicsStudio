from pathlib import Path

from dualsphysics_studio.catalog import SemanticCatalog
from dualsphysics_studio.settings import AppSettings
from dualsphysics_studio.version import APP_VERSION


def test_product_version_is_1_0() -> None:
    assert APP_VERSION == "1.0.0"


def test_catalog_resolves_parameter_key() -> None:
    catalog = SemanticCatalog()
    text = catalog.explain("parameter", {"key": "StepAlgorithm", "value": "2"})
    assert "Symplectic" in text
    assert catalog.version == "5.4"


def test_settings_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    value = AppSettings(gencase_root="D:/DualSPHysics_v5.4")
    value.remember_file(str(tmp_path / "案例.xml"))
    value.save(path)
    loaded = AppSettings.load(path)
    assert loaded.gencase_root.endswith("DualSPHysics_v5.4")
    assert loaded.recent_files and "案例.xml" in loaded.recent_files[0]
