from pathlib import Path
from pydantic import BaseModel, Field

# Default exclusions for files and directories we want to ignore
DEFAULT_EXCLUSIONS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "third_party",
    "dist",
    "build",
    "__pycache__",
    ".pytest_cache",
    ".codegraph",
}

# Mapping of supported languages to file extensions
LANGUAGE_EXTENSIONS = {
    "python": {".py"},
    "javascript": {".js", ".mjs", ".cjs"},
    "typescript": {".ts", ".tsx"},
    "go": {".go"},
    "rust": {".rs"},
    "swift": {".swift"},
    "c": {".c", ".h"},
    "cpp": {".cpp", ".cc", ".cxx", ".hpp", ".hxx"},
}

ALL_EXTENSIONS = {ext for exts in LANGUAGE_EXTENSIONS.values() for ext in exts}


class CodegraphConfig(BaseModel):
    """Configuration class for codegraph parsing and exporting."""

    workspace_dir: Path
    output_dir: Path = Field(default_factory=lambda: Path(".codegraph"))
    exclusions: set[str] = Field(default_factory=lambda: DEFAULT_EXCLUSIONS)
    languages: set[str] = Field(default_factory=lambda: set(LANGUAGE_EXTENSIONS.keys()))

    @property
    def absolute_output_dir(self) -> Path:
        if self.output_dir.is_absolute():
            return self.output_dir
        return self.workspace_dir / self.output_dir
