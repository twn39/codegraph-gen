import logging
from pathlib import Path
import tree_sitter
import tree_sitter_python
from codegraph_gen.parser.base import (
    BaseParser,
    ExtractionResult,
    NodeSchema,
    EdgeSchema,
    ASTVisitor,
)

logger = logging.getLogger(__name__)


class PythonVisitor(ASTVisitor):
    def __init__(self, source: bytes, rel_path: str, result: ExtractionResult, parser):
        super().__init__(source, rel_path, result)
        self.parser = parser

    def visit_class_definition(self, node: tree_sitter.Node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node:
            class_name = self.get_text(name_node)
            parent_id = self.get_current_parent_id()
            class_id = f"{self.rel_path}::{class_name}"

            start_line, end_line = self.get_line_range(node)
            self.result.nodes.append(
                NodeSchema(
                    id=class_id,
                    label=class_name,
                    type="class",
                    source_file=self.rel_path,
                    line_start=start_line,
                    line_end=end_line,
                    signature=self.parser._get_signature(node, self.source),
                    docstring=self.parser._get_docstring(node, self.source),
                )
            )

            self.result.edges.append(
                EdgeSchema(source=parent_id, target=class_id, relation="contains")
            )

            # Check inheritance
            superclasses = node.child_by_field_name("superclasses")
            if superclasses:
                for child in superclasses.children:
                    if child.type in ("identifier", "attribute"):
                        parent_class_name = self.get_text(child)
                        self.result.edges.append(
                            EdgeSchema(
                                source=class_id,
                                target=parent_class_name,
                                relation="inherits",
                            )
                        )

            with self.scope.push(class_id, "class"):
                self.generic_visit(node)
        else:
            self.generic_visit(node)

    def visit_function_definition(self, node: tree_sitter.Node) -> None:
        name_node = node.child_by_field_name("name")
        if name_node:
            func_name = self.get_text(name_node)
            parent_id = self.get_current_parent_id()
            parent_type = self.scope.current_type

            if parent_type == "class":
                func_id = f"{parent_id}.{func_name}"
                sym_type = "method"
            else:
                func_id = f"{self.rel_path}::{func_name}"
                sym_type = "function"

            local_bindings = {}

            def extract_type_from_call_or_type(type_or_call_node):
                if type_or_call_node.type == "identifier":
                    return self.get_text(type_or_call_node)
                elif type_or_call_node.type == "attribute":
                    attr_node = type_or_call_node.child_by_field_name("attribute")
                    if attr_node:
                        return self.get_text(attr_node)
                elif type_or_call_node.type == "type":
                    for child in type_or_call_node.children:
                        res = extract_type_from_call_or_type(child)
                        if res:
                            return res
                elif type_or_call_node.type == "call":
                    func_node = type_or_call_node.child_by_field_name("function")
                    if func_node:
                        return extract_type_from_call_or_type(func_node)
                for child in type_or_call_node.children:
                    res = extract_type_from_call_or_type(child)
                    if res:
                        return res
                return None

            def collect_local_bindings(n):
                if n.type == "typed_parameter":
                    var_name = None
                    for child in n.children:
                        if child.type == "identifier":
                            var_name = self.get_text(child)
                            break
                    type_node = n.child_by_field_name("type")
                    if var_name and type_node:
                        t_name = extract_type_from_call_or_type(type_node)
                        if t_name:
                            local_bindings[var_name] = t_name
                elif n.type == "assignment":
                    left = n.child_by_field_name("left") or (
                        n.children[0] if n.children else None
                    )
                    right = n.child_by_field_name("right") or (
                        n.children[2] if len(n.children) > 2 else None
                    )
                    if (
                        left
                        and right
                        and left.type == "identifier"
                        and right.type == "call"
                    ):
                        t_name = extract_type_from_call_or_type(right)
                        var_name = self.get_text(left)
                        if t_name:
                            local_bindings[var_name] = t_name
                elif n.type == "as_pattern":
                    call_node = None
                    target_node = None
                    for child in n.children:
                        if child.type == "call":
                            call_node = child
                        elif child.type == "as_pattern_target":
                            for sub in child.children:
                                if sub.type == "identifier":
                                    target_node = sub
                                    break
                    if call_node and target_node:
                        t_name = extract_type_from_call_or_type(call_node)
                        var_name = self.get_text(target_node)
                        if t_name:
                            local_bindings[var_name] = t_name

                for child in n.children:
                    if child.type != "function_definition":
                        collect_local_bindings(child)

            collect_local_bindings(node)

            start_line, end_line = self.get_line_range(node)
            self.result.nodes.append(
                NodeSchema(
                    id=func_id,
                    label=func_name,
                    type=sym_type,
                    source_file=self.rel_path,
                    line_start=start_line,
                    line_end=end_line,
                    signature=self.parser._get_signature(node, self.source),
                    docstring=self.parser._get_docstring(node, self.source),
                    local_bindings=local_bindings,
                )
            )

            self.result.edges.append(
                EdgeSchema(source=parent_id, target=func_id, relation="contains")
            )

            with self.scope.push(func_id, sym_type):
                self.generic_visit(node)
        else:
            self.generic_visit(node)

    def visit_import_statement(self, node: tree_sitter.Node) -> None:
        file_node_id = self.rel_path
        for child in node.children:
            if child.type == "dotted_name":
                module_name = self.get_text(child)
                self.result.edges.append(
                    EdgeSchema(
                        source=file_node_id,
                        target=module_name,
                        relation="imports",
                        import_map={module_name: module_name},
                    )
                )
            elif child.type == "aliased_import":
                name_node = child.child_by_field_name("name")
                alias_node = child.child_by_field_name("alias")
                if name_node and alias_node:
                    module_name = self.get_text(name_node)
                    alias_name = self.get_text(alias_node)
                    self.result.edges.append(
                        EdgeSchema(
                            source=file_node_id,
                            target=module_name,
                            relation="imports",
                            import_map={alias_name: module_name},
                        )
                    )
        self.generic_visit(node)

    def visit_import_from_statement(self, node: tree_sitter.Node) -> None:
        file_node_id = self.rel_path
        module_node = node.child_by_field_name("module_name")
        module_name = ""
        if module_node:
            module_name = self.get_text(module_node)

        dots = ""
        for child in node.children:
            if child.type == "relative_source":
                dots = self.get_text(child)
                break

        target_module = dots + module_name
        import_map = {}
        import_items = []

        start_collecting = False
        for child in node.children:
            if (module_node and child == module_node) or (
                child.type == "relative_source" and not start_collecting
            ):
                start_collecting = True
                continue
            if start_collecting:
                if child.type == "wildcard_import":
                    import_items.append(child)
                elif child.type in (
                    "dotted_name",
                    "aliased_import",
                    "identifier",
                ):
                    import_items.append(child)
                elif child.type == "import_list":
                    for sub_child in child.children:
                        if sub_child.type in (
                            "dotted_name",
                            "aliased_import",
                            "identifier",
                        ):
                            import_items.append(sub_child)

        for item in import_items:
            if item.type == "wildcard_import":
                import_map["*"] = "*"
            elif item.type in ("dotted_name", "identifier"):
                name = self.get_text(item)
                import_map[name] = name
            elif item.type == "aliased_import":
                name_node = item.child_by_field_name("name")
                alias_node = item.child_by_field_name("alias")
                if name_node and alias_node:
                    name = self.get_text(name_node)
                    alias = self.get_text(alias_node)
                    import_map[alias] = name

        if target_module:
            self.result.edges.append(
                EdgeSchema(
                    source=file_node_id,
                    target=target_module,
                    relation="imports",
                    import_map=import_map,
                )
            )
        self.generic_visit(node)

    def visit_call(self, node: tree_sitter.Node) -> None:
        func_node = node.child_by_field_name("function")
        if func_node:
            callee_name = self.get_text(func_node)
            caller_id = self.get_current_parent_id()
            self.result.edges.append(
                EdgeSchema(source=caller_id, target=callee_name, relation="calls")
            )
        self.generic_visit(node)


class PythonParser(BaseParser):
    def __init__(self):
        self.language = tree_sitter.Language(tree_sitter_python.language())
        self.parser = tree_sitter.Parser(self.language)

    def _get_docstring(self, node, source: bytes) -> str:
        body = node.child_by_field_name("body")
        if not body:
            body = node

        for child in body.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type in ("string", "concatenated_string"):
                        text = source[sub.start_byte : sub.end_byte].decode(
                            "utf-8", errors="replace"
                        )
                        return text.strip("\"'").strip()
            if child.type not in ("comment",):
                break
        return ""

    def _get_signature(self, node, source: bytes) -> str:
        body = node.child_by_field_name("body")
        if body:
            end_byte = body.start_byte
            sig_bytes = source[node.start_byte : end_byte]
            sig = sig_bytes.decode("utf-8", errors="replace").strip()
            if sig.endswith(":"):
                sig = sig[:-1].strip()
            return sig
        return (
            source[node.start_byte : node.end_byte]
            .decode("utf-8", errors="replace")
            .split("\n")[0]
        )

    def parse_file(self, file_path: Path, workspace_dir: Path) -> ExtractionResult:
        try:
            source = file_path.read_bytes()
        except Exception as e:
            logger.error(f"Error reading file {file_path}: {e}")
            return ExtractionResult()

        tree = self.parser.parse(source)
        root = tree.root_node

        rel_path = str(file_path.relative_to(workspace_dir))
        result = ExtractionResult()

        # Add file node representing the module itself
        file_node_id = rel_path
        result.nodes.append(
            NodeSchema(
                id=file_node_id,
                label=file_path.name,
                type="file",
                source_file=rel_path,
                line_start=1,
                line_end=len(source.splitlines()) or 1,
                signature=f"module {file_path.name}",
                docstring=self._get_docstring(root, source),
            )
        )

        visitor = PythonVisitor(source, rel_path, result, self)
        visitor.visit(root)
        return result
