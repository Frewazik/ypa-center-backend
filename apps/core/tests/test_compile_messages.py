from __future__ import annotations

import gettext
import importlib.util
import io
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "compile_messages.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("compile_messages", _SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Не загрузить {_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _po(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "django.po"
    path.write_text(text, encoding="utf-8")
    return path


_HEADER = 'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=UTF-8\\n"\n\n'


class TestCompileMessages:
    def test_built_catalog_is_readable_by_gettext(self, tmp_path: Path) -> None:
        script = _load_script()
        po = _po(
            tmp_path,
            _HEADER + 'msgid "Type to search"\nmsgstr "Поиск"\n\n'
            'msgid "Return to site"\nmsgstr ""\n"Вернуться "\n"на сайт"\n',
        )

        catalog = gettext.GNUTranslations(
            io.BytesIO(script.build_mo(script.parse_po(po)))
        )

        assert catalog.gettext("Type to search") == "Поиск"
        assert catalog.gettext("Return to site") == "Вернуться на сайт"

    def test_plural_forms_fail_loudly(self, tmp_path: Path) -> None:
        script = _load_script()
        po = _po(
            tmp_path, _HEADER + 'msgid "row"\nmsgid_plural "rows"\nmsgstr[0] "строка"\n'
        )

        with pytest.raises(ValueError, match="неподдерживаемая"):
            script.parse_po(po)
