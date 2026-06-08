from codegraph.parser.base import BaseParser
from codegraph.parser.python import PythonParser
from codegraph.parser.javascript import JavaScriptParser
from codegraph.parser.go import GoParser
from codegraph.parser.rust import RustParser
from codegraph.parser.swift import SwiftParser

PARSERS: dict[str, type[BaseParser]] = {
    "python": PythonParser,
    "javascript": JavaScriptParser,
    "typescript": JavaScriptParser, # uses same tree-sitter parser
    "go": GoParser,
    "rust": RustParser,
    "swift": SwiftParser,
}

def get_parser(language: str) -> BaseParser:
    """Returns an instance of the parser for the given language."""
    if language not in PARSERS:
        raise ValueError(f"Unsupported language: {language}")
    return PARSERS[language]()
