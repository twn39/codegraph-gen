from __future__ import annotations

import re
from abc import ABC, abstractmethod


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
        target_path_part = target.replace(".", "/")
        candidates = [target_path_part]
        for ext in self.import_search_suffixes:
            candidates.append(target_path_part + ext)
        return candidates

    def extend_resolver_chain(self, default_chain: list) -> list:
        return default_chain

    def compute_transfer_type(
        self, resolved_target_type: str, resolved_target_id: str
    ) -> str | None:
        return None


_WRAPPER_CONTAINERS: frozenset[str] = frozenset(
    {
        "Optional",
        "Union",
        "Option",
        "Future",
        "Promise",
        "Result",
        "Box",
        "Arc",
        "Rc",
        "Annotated",
    }
)


def _unwrap_type_name(raw: str) -> str | None:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in "\"'" and raw[-1] == raw[0]:
        raw = raw[1:-1].strip()
    if not raw or raw in ("None", "none", "void", "nil", "()"):
        return None
    # Support PEP 604 unions: "Client | None" or "None | Client"
    if "|" in raw:
        parts = [p.strip() for p in raw.split("|") if p.strip()]
        non_none = [p for p in parts if p not in ("None", "none", "null", "nil")]
        if non_none:
            return _unwrap_type_name(non_none[0])
        return None

    # Support generics with square brackets: "Optional[Client]" or "dict[str, Any]"
    if "[" in raw and raw.endswith("]"):
        head, inside = raw.split("[", 1)
        head = head.strip()
        inside = inside[:-1].strip()
        if head in _WRAPPER_CONTAINERS:
            first_arg = inside.split(",", 1)[0].strip()
            unwrapped = _unwrap_type_name(first_arg)
            if unwrapped:
                return unwrapped
        return head.rsplit("::", 1)[-1].rsplit(".", 1)[-1]

    # Support generics with angle brackets: "List<MyClass>" or "Result<Client, Error>"
    if "<" in raw and raw.endswith(">"):
        head, inside = raw.split("<", 1)
        inside = inside[:-1].strip()
        first_arg = inside.split(",", 1)[0].strip()
        unwrapped = _unwrap_type_name(first_arg)
        if unwrapped:
            return unwrapped
        return head.rsplit("::", 1)[-1].rsplit(".", 1)[-1]

    # Normalize Self / self / leaf name
    leaf = raw.rsplit("::", 1)[-1].rsplit(".", 1)[-1].strip()
    return leaf or None



def extract_arrow_return_type(signature: str) -> str | None:
    match = re.search(r"->\s*([^:{(\n]+)", signature)
    if match:
        ret_type = match.group(1).strip()
        return _unwrap_type_name(ret_type)
    return None

