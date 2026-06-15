class FileSymbolScope:
    def __init__(self, file_path: str, language: str):
        self.file_path = file_path
        self.language = language
        # Maps local symbol name -> fully qualified Node ID (e.g. {"MyClass": "foo.py::MyClass"})
        self.declared_symbols: dict[str, str] = {}
        # Maps import alias or local name -> (target_file_id, original_name)
        self.imported_symbols: dict[str, tuple[str, str]] = {}
        # List of target files that were wildcard imported (e.g. from X import *)
        self.wildcard_imports: list[str] = []
