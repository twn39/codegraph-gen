import re
from abc import ABC, abstractmethod
from pathlib import Path


class LanguageResolverStrategy(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @property
    @abstractmethod
    def file_extensions(self) -> set[str]:
        pass

    @property
    def builtin_functions(self) -> set[str]:
        return set()

    @property
    def stdlib_modules(self) -> set[str]:
        return set()

    @property
    def import_search_suffixes(self) -> list[str]:
        # Suffixes to try when doing relative path imports.
        # Defaults to the strategy's primary file extensions.
        return list(self.file_extensions)

    def is_builtin(self, symbol: str) -> bool:
        return symbol in self.builtin_functions

    def is_stdlib(self, symbol: str) -> bool:
        return symbol in self.stdlib_modules

    def extract_return_type(self, signature: str) -> str | None:
        return None

    def has_package_sibling_scope(self) -> bool:
        return False

    def is_path_target(self, target: str) -> bool:
        return target.startswith(".") or "/" in target or "\\" in target

    def should_treat_import_as_wildcard(
        self, target_file_id: str, import_map: dict[str, str]
    ) -> bool:
        return "*" in import_map.values()

    def get_import_path_candidates(self, target: str) -> list[str]:
        # Standard conversion: convert dots to slashes and try matching the target directly
        target_path_part = target.replace(".", "/")
        candidates = [target_path_part]
        for ext in self.import_search_suffixes:
            candidates.append(target_path_part + ext)
        return candidates


def _extract_arrow_return_type(signature: str) -> str | None:
    match = re.search(r"->\s*([\w::.<>]+)", signature)
    if match:
        ret_type = match.group(1).strip()
        generic_match = re.search(r"<([\w::.]+)>", ret_type)
        if generic_match:
            return generic_match.group(1).rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        return ret_type.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    return None


class PythonStrategy(LanguageResolverStrategy):
    name = "python"
    file_extensions = {".py"}
    import_search_suffixes = [".py"]
    builtin_functions = {
        "print",
        "len",
        "range",
        "str",
        "int",
        "dict",
        "list",
        "set",
        "tuple",
        "open",
        "sum",
        "min",
        "max",
        "abs",
        "enumerate",
        "zip",
        "any",
        "all",
        "map",
        "filter",
        "super",
        "repr",
        "type",
        "isinstance",
        "issubclass",
        "dir",
        "id",
        "hash",
        "input",
    }
    stdlib_modules = {
        "os",
        "sys",
        "json",
        "time",
        "math",
        "re",
        "pathlib",
        "logging",
        "subprocess",
        "shutil",
        "hashlib",
        "urllib",
        "socket",
        "threading",
        "multiprocessing",
        "typing",
        "collections",
        "itertools",
        "functools",
        "logger",
        "log",
        "pytest",
        "unittest",
    }

    def extract_return_type(self, signature: str) -> str | None:
        return _extract_arrow_return_type(signature)

    def get_import_path_candidates(self, target: str) -> list[str]:
        target_path_part = target.replace(".", "/")
        return [
            target_path_part,
            target_path_part + ".py",
            target_path_part + "/__init__.py",
        ]


class JavaScriptStrategy(LanguageResolverStrategy):
    name = "javascript"
    file_extensions = {".js", ".mjs", ".cjs"}
    import_search_suffixes = [".js", ".mjs", ".cjs"]
    builtin_functions = {
        "console",
        "require",
        "module",
        "exports",
        "process",
        "window",
        "document",
        "eval",
        "parseInt",
        "parseFloat",
        "isNaN",
        "isFinite",
        "decodeURI",
        "encodeURI",
        "Object",
        "Array",
        "String",
        "Number",
        "Boolean",
        "Date",
        "RegExp",
        "Error",
        "Map",
        "Set",
        "Promise",
        "JSON",
        "Math",
        "setTimeout",
        "clearTimeout",
        "setInterval",
        "clearInterval",
        "global",
    }
    stdlib_modules = {
        "fs",
        "path",
    }


class TypeScriptStrategy(JavaScriptStrategy):
    name = "typescript"
    file_extensions = {".ts", ".tsx"}
    import_search_suffixes = [".ts", ".tsx"]


class KotlinStrategy(LanguageResolverStrategy):
    name = "kotlin"
    file_extensions = {".kt", ".kts"}
    import_search_suffixes = [".kt", ".kts"]
    builtin_functions = {
        "print",
        "println",
        "listOf",
        "mapOf",
        "setOf",
        "mutableListOf",
        "mutableMapOf",
        "mutableSetOf",
        "arrayOf",
        "emptyList",
        "emptyMap",
        "emptySet",
        "run",
        "let",
        "also",
        "apply",
        "takeIf",
        "takeUnless",
        "repeat",
        "require",
        "check",
        "error",
    }
    stdlib_modules = {
        "java",
        "kotlin",
        "kotlinx",
    }

    def extract_return_type(self, signature: str) -> str | None:
        last_paren = signature.rfind(")")
        if last_paren != -1:
            after_paren = signature[last_paren + 1 :]
            match = re.search(r":\s*([\w<>]+)", after_paren)
            if match:
                ret_type = match.group(1).strip()
                generic_match = re.search(r"<([\w]+)>", ret_type)
                if generic_match:
                    return generic_match.group(1)
                return ret_type
        return None


class GoStrategy(LanguageResolverStrategy):
    name = "go"
    file_extensions = {".go"}
    import_search_suffixes = [".go"]
    builtin_functions = {
        "print",
        "println",
        "panic",
        "recover",
        "make",
        "new",
        "len",
        "cap",
        "append",
        "copy",
        "delete",
        "complex",
        "real",
        "imag",
        "close",
    }
    stdlib_modules = {
        "fmt",
        "sync",
        "context",
        "strings",
        "bytes",
        "errors",
        "net",
        "http",
        "os",
        "io",
        "bufio",
        "strconv",
        "time",
    }

    def has_package_sibling_scope(self) -> bool:
        return True

    def extract_return_type(self, signature: str) -> str | None:
        last_paren = signature.rfind(")")
        if last_paren != -1:
            after_paren = signature[last_paren + 1 :].strip()
            if not after_paren or after_paren == "{":
                return None
            if after_paren.startswith("("):
                after_paren = after_paren[1:].split(")")[0]
                parts = [p.strip() for p in after_paren.split(",")]
                for p in parts:
                    clean_p = p.split()[-1]
                    if clean_p not in ("error", "bool", "int", "string"):
                        return clean_p
            else:
                clean_p = after_paren.split("{")[0].strip().split()[-1]
                clean_p = clean_p.lstrip("*").lstrip("[]")
                if clean_p not in ("error", "bool", "int", "string"):
                    return clean_p
        return None


class RustStrategy(LanguageResolverStrategy):
    name = "rust"
    file_extensions = {".rs"}
    import_search_suffixes = [".rs"]
    builtin_functions = {
        "println!",
        "print!",
        "format!",
        "panic!",
        "vec!",
        "assert!",
        "assert_eq!",
        "Option",
        "Result",
        "Some",
        "None",
        "Ok",
        "Err",
        "Default",
    }
    stdlib_modules = {
        "std",
        "core",
        "alloc",
    }

    def extract_return_type(self, signature: str) -> str | None:
        return _extract_arrow_return_type(signature)

    def get_import_path_candidates(self, target: str) -> list[str]:
        target_path_part = target.replace("::", "/").replace(".", "/")
        return [
            target_path_part,
            target_path_part + ".rs",
            target_path_part + "/mod.rs",
        ]


class SwiftStrategy(LanguageResolverStrategy):
    name = "swift"
    file_extensions = {".swift"}
    import_search_suffixes = [".swift"]
    builtin_functions = {
        "print",
        "min",
        "max",
        "abs",
        "count",
        "fatalError",
        "precondition",
        "assert",
    }
    stdlib_modules = {
        "Foundation",
        "UIKit",
        "AppKit",
        "Combine",
        "SwiftUI",
    }

    def has_package_sibling_scope(self) -> bool:
        return True

    def extract_return_type(self, signature: str) -> str | None:
        return _extract_arrow_return_type(signature)


class CStrategy(LanguageResolverStrategy):
    name = "c"
    file_extensions = {".c", ".h"}
    import_search_suffixes = [".h", ".hpp", ".hxx", ".c", ".cpp", ".cc", ".cxx"]
    builtin_functions = {
        "printf",
        "scanf",
        "malloc",
        "free",
        "calloc",
        "realloc",
        "memcpy",
        "memset",
        "strcpy",
        "strlen",
        "strcmp",
        "strcat",
        "exit",
        "fopen",
        "fclose",
        "fprintf",
        "sprintf",
        "sizeof",
    }

    def is_path_target(self, target: str) -> bool:
        if super().is_path_target(target):
            return True
        return any(
            target.endswith(ext)
            for ext in (".h", ".hpp", ".hxx", ".c", ".cpp", ".cc", ".cxx")
        )

    def should_treat_import_as_wildcard(
        self, target_file_id: str, import_map: dict[str, str]
    ) -> bool:
        return True

    def extract_return_type(self, signature: str) -> str | None:
        tokens = signature.split()
        if tokens:
            idx = 0
            while idx < len(tokens) and tokens[idx] in (
                "inline",
                "static",
                "virtual",
                "friend",
                "const",
                "constexpr",
            ):
                idx += 1
            if idx < len(tokens):
                ret_type = tokens[idx]
                if "(" in ret_type or ")" in ret_type:
                    return None
                ret_type = ret_type.replace("*", "").replace("&", "").strip()
                return ret_type.split("::")[-1]
        return None


class CppStrategy(CStrategy):
    name = "cpp"
    file_extensions = {".cpp", ".cc", ".cxx", ".hpp", ".hxx"}
    builtin_functions = CStrategy.builtin_functions | {
        "cout",
        "cin",
        "endl",
        "vector",
        "string",
        "map",
        "set",
        "list",
        "shared_ptr",
        "unique_ptr",
        "make_shared",
        "make_unique",
        "move",
    }
    stdlib_modules = {
        "std",
    }


# Registry Setup
_STRATEGY_REGISTRY: dict[str, LanguageResolverStrategy] = {}
_STRATEGY_BY_NAME: dict[str, LanguageResolverStrategy] = {}

_DEFAULT_STRATEGY = PythonStrategy()

for strategy_cls in [
    PythonStrategy,
    JavaScriptStrategy,
    TypeScriptStrategy,
    KotlinStrategy,
    GoStrategy,
    RustStrategy,
    SwiftStrategy,
    CStrategy,
    CppStrategy,
]:
    inst = strategy_cls()
    _STRATEGY_BY_NAME[inst.name] = inst
    for ext in inst.file_extensions:
        _STRATEGY_REGISTRY[ext] = inst


def get_strategy_for_file(file_path: str) -> LanguageResolverStrategy:
    suffix = Path(file_path).suffix.lower()
    return _STRATEGY_REGISTRY.get(suffix, _DEFAULT_STRATEGY)


def get_strategy_by_name(lang_name: str) -> LanguageResolverStrategy:
    return _STRATEGY_BY_NAME.get(lang_name.lower(), _DEFAULT_STRATEGY)
