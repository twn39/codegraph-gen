import importlib
import logging
import pkgutil
import sys
from pathlib import Path
from codegraph_gen.parser.base import BaseParser, _PARSER_REGISTRY

logger = logging.getLogger(__name__)

# Dynamic package scan & load to trigger @register_parser registrations
package_dir = str(Path(__file__).parent)
for _, module_name, _ in pkgutil.iter_modules([package_dir]):
    if module_name == "base":
        continue
    full_module_name = f"{__name__}.{module_name}"
    if full_module_name not in sys.modules:
        try:
            importlib.import_module(full_module_name)
        except Exception as e:
            logger.error(
                f"Defensive Loading: Failed to import parser module {full_module_name}: {e}",
                exc_info=True,
            )


def get_parser(language: str) -> BaseParser:
    """Returns an instance of the parser for the given language."""
    lang_lower = language.lower()
    if lang_lower not in _PARSER_REGISTRY:
        raise ValueError(f"Unsupported language: {language}")
    return _PARSER_REGISTRY[lang_lower]()
