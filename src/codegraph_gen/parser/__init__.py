from codegraph_gen.parser.base import BaseParser
from codegraph_gen.parser.python import PythonParser
from codegraph_gen.parser.javascript import JavaScriptParser
from codegraph_gen.parser.go import GoParser
from codegraph_gen.parser.rust import RustParser
from codegraph_gen.parser.swift import SwiftParser
from codegraph_gen.parser.cpp import CParser, CppParser
from codegraph_gen.parser.kotlin import KotlinParser

PARSERS: dict[str, type[BaseParser]] = {
    "python": PythonParser,
    "javascript": JavaScriptParser,
    "typescript": JavaScriptParser,  # uses same tree-sitter parser
    "go": GoParser,
    "rust": RustParser,
    "swift": SwiftParser,
    "c": CParser,
    "cpp": CppParser,
    "kotlin": KotlinParser,
}


def get_parser(language: str) -> BaseParser:
    """Returns an instance of the parser for the given language."""
    if language not in PARSERS:
        raise ValueError(f"Unsupported language: {language}")
    return PARSERS[language]()
