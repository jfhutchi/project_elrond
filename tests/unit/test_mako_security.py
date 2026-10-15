"""Characterize the Windows dependency boundary behind GHSA-5639-2j2p-m4mx."""

import sys
from pathlib import Path

import pytest
from mako.exceptions import TemplateLookupException
from mako.lookup import TemplateLookup


def test_mako_renders_templates_inside_configured_directory(tmp_path: Path) -> None:
    templates = tmp_path / "templates"
    templates.mkdir()
    (templates / "safe.mako").write_text("Hello ${name}", encoding="utf-8")
    lookup = TemplateLookup(directories=[str(templates)])

    assert lookup.get_template("safe.mako").render(name="Elrond") == "Hello Elrond"


@pytest.mark.skipif(sys.platform != "win32", reason="native Windows drive-path regression")
@pytest.mark.parametrize("drive", ["C:", "c:", "D:"])
@pytest.mark.parametrize("separator", ["/", "\\"])
def test_mako_rejects_drive_uri_escaping_template_directory(
    tmp_path: Path, drive: str, separator: str
) -> None:
    templates = tmp_path / "root" / "templates"
    templates.mkdir(parents=True)
    (tmp_path / "outside.mako").write_text("OUTSIDE_TEMPLATE_ROOT", encoding="utf-8")
    lookup = TemplateLookup(directories=[str(templates)])
    uri = separator.join([drive, "..", "..", "..", "outside.mako"])

    with pytest.raises(TemplateLookupException):
        lookup.get_template(uri)
