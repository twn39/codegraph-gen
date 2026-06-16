import logging
from pathlib import Path
import pytest
from codegraph_gen.parser import get_parser
from codegraph_gen.parser.base import (
    register_parser,
    BaseParser,
    _PARSER_REGISTRY,
)
from codegraph_gen.schema import (
    ExtractionResult,
)


def test_registry_lookups():
    from codegraph_gen.parser.python import PythonParser
    from codegraph_gen.parser.javascript import JavaScriptParser
    from codegraph_gen.parser.go import GoParser
    from codegraph_gen.parser.rust import RustParser
    from codegraph_gen.parser.swift import SwiftParser
    from codegraph_gen.parser.kotlin import KotlinParser
    from codegraph_gen.parser.cpp import CParser, CppParser
    from codegraph_gen.parser.ocaml import OCamlParser

    assert isinstance(get_parser("python"), PythonParser)
    assert isinstance(get_parser("javascript"), JavaScriptParser)
    assert isinstance(get_parser("typescript"), JavaScriptParser)
    assert isinstance(get_parser("go"), GoParser)
    assert isinstance(get_parser("rust"), RustParser)
    assert isinstance(get_parser("swift"), SwiftParser)
    assert isinstance(get_parser("kotlin"), KotlinParser)
    assert isinstance(get_parser("c"), CParser)
    assert isinstance(get_parser("cpp"), CppParser)
    assert isinstance(get_parser("ocaml"), OCamlParser)

    # Case insensitivity
    assert isinstance(get_parser("PyThOn"), PythonParser)

    with pytest.raises(ValueError, match="Unsupported language"):
        get_parser("unknown_lang")


def test_custom_parser_registration():
    @register_parser("dummy_lang")
    class DummyParser(BaseParser):
        def parse_file(self, file_path: Path, workspace_dir: Path) -> ExtractionResult:
            return ExtractionResult()

    assert "dummy_lang" in _PARSER_REGISTRY
    assert isinstance(get_parser("dummy_lang"), DummyParser)

    # clean up registry after test
    del _PARSER_REGISTRY["dummy_lang"]


def test_defensive_loading_safety(caplog):
    # Simulate a parser module that has import-time errors inside the parser directory
    parser_dir = Path(__file__).parent.parent / "src" / "codegraph_gen" / "parser"
    bad_module_path = parser_dir / "bad_test_parser.py"

    try:
        bad_module_path.write_text(
            "raise ValueError('Simulated import failure in parser')\n"
        )

        # Trigger scanning logic by reloading the parser package
        import importlib
        import codegraph_gen.parser

        # Remove sys.modules caching for this bad module to ensure it tries to load
        import sys

        if "codegraph_gen.parser.bad_test_parser" in sys.modules:
            del sys.modules["codegraph_gen.parser.bad_test_parser"]

        with caplog.at_level(logging.ERROR):
            importlib.reload(codegraph_gen.parser)

        # Verify defensive loading caught and logged the exception, and didn't crash
        assert any("Defensive Loading" in record.message for record in caplog.records)
        assert any(
            "Simulated import failure" in record.message for record in caplog.records
        )
    finally:
        if bad_module_path.exists():
            bad_module_path.unlink()

        # Clean up sys.modules entry
        import sys

        if "codegraph_gen.parser.bad_test_parser" in sys.modules:
            del sys.modules["codegraph_gen.parser.bad_test_parser"]
