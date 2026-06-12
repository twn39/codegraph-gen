import os
from pathlib import Path
from pydantic import BaseModel, Field
from codegraph.parser.base import ExtractionResult

# Default exclusions for files and directories we want to ignore
DEFAULT_EXCLUSIONS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "third_party",
    "dist",
    "build",
    ".build",
    "__pycache__",
    ".pytest_cache",
    ".codegraph",
    ".idea",
    ".vscode",
    "target",
    "out",
    "bin",
    "obj",
    "vendor",
    "Pods",
    "Carthage",
    "DerivedData",
    "build_output",
    ".next",
    ".nuxt",
    ".cache",
    "build_mac",
    "build_ios",
    "build_ios_sim",
}


# Mapping of supported languages to file extensions
LANGUAGE_EXTENSIONS = {
    "python": {".py"},
    "javascript": {".js", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx"},
    "kotlin": {".kt", ".kts"},
    "go": {".go"},
    "rust": {".rs"},
    "swift": {".swift"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hxx"},
}

ALL_EXTENSIONS = {ext for exts in LANGUAGE_EXTENSIONS.values() for ext in exts}


class CacheEntry(BaseModel):
    mtime: float
    size: int
    hash: str
    result: ExtractionResult


class CodegraphConfig(BaseModel):
    """Configuration class for codegraph parsing and exporting."""

    workspace_dir: Path
    output_dir: Path = Field(default_factory=lambda: Path(".codegraph"))
    exclusions: set[str] = Field(default_factory=lambda: DEFAULT_EXCLUSIONS)
    languages: set[str] = Field(default_factory=lambda: set(LANGUAGE_EXTENSIONS.keys()))
    max_workers: int = Field(default_factory=lambda: os.cpu_count() or 4)
    use_cache: bool = Field(default=True)

    @property
    def absolute_output_dir(self) -> Path:
        if self.output_dir.is_absolute():
            return self.output_dir
        return self.workspace_dir / self.output_dir
