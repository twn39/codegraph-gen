import logging
from pathlib import Path
import tree_sitter
import tree_sitter_python
from codegraph_gen.parser.base import BaseParser, ExtractionResult, NodeSchema, EdgeSchema

logger = logging.getLogger(__name__)


class PythonParser(BaseParser):
    def __init__(self):
        self.language = tree_sitter.Language(tree_sitter_python.language())
        self.parser = tree_sitter.Parser(self.language)

    def _get_docstring(self, node, source: bytes) -> str:
        """Extracts the docstring from class/function/module body."""
        body = node.child_by_field_name("body")
        if not body:
            # For modules, the root node is the body container
            body = node

        for child in body.children:
            if child.type == "expression_statement":
                for sub in child.children:
                    if sub.type in ("string", "concatenated_string"):
                        text = source[sub.start_byte : sub.end_byte].decode(
                            "utf-8", errors="replace"
                        )
                        # Strip quotes
                        return text.strip("\"'").strip()
            # Docstring must be the first statement
            if child.type not in ("comment",):
                break
        return ""

    def _get_signature(self, node, source: bytes) -> str:
        """Extracts class/function signature (e.g. def hello(a, b))."""
        # Take the text from start of definition up to the colon / block
        body = node.child_by_field_name("body")
        if body:
            end_byte = body.start_byte
            # Trim trailing whitespace and colons
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

        # 1. Add file node representing the module itself
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

        # Scope helper to manage parent IDs during recursive walk
        # stack of (node_id, node_type)
        scope_stack = [(file_node_id, "file")]

        def get_current_parent_id():
            return scope_stack[-1][0] if scope_stack else file_node_id

        def walk(node):
            nonlocal result

            if node.type == "ERROR" or (hasattr(node, "is_error") and node.is_error):
                logger.debug(f"Skipping syntax error node in Python AST: {node}")
                return

            node_type = node.type
            pushed_scope = False

            if node_type == "class_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    parent_id = get_current_parent_id()

                    # Compute qualified ID
                    class_id = f"{rel_path}::{class_name}"

                    # Add node
                    result.nodes.append(
                        NodeSchema(
                            id=class_id,
                            label=class_name,
                            type="class",
                            source_file=rel_path,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            signature=self._get_signature(node, source),
                            docstring=self._get_docstring(node, source),
                        )
                    )

                    # Add containment edge
                    result.edges.append(
                        EdgeSchema(
                            source=parent_id, target=class_id, relation="contains"
                        )
                    )

                    # Check inheritance
                    superclasses = node.child_by_field_name("superclasses")
                    if superclasses:
                        # Extract inherited class names
                        for child in superclasses.children:
                            if child.type in ("identifier", "attribute"):
                                parent_class_name = source[
                                    child.start_byte : child.end_byte
                                ].decode("utf-8", errors="replace")
                                # We record inheritance edge; builder will resolve the full ID later
                                result.edges.append(
                                    EdgeSchema(
                                        source=class_id,
                                        target=parent_class_name,
                                        relation="inherits",
                                    )
                                )

                    scope_stack.append((class_id, "class"))
                    pushed_scope = True

            elif node_type == "function_definition":
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    parent_id = get_current_parent_id()
                    parent_type = scope_stack[-1][1] if scope_stack else "file"

                    # Compute ID: if inside a class, prepend class name.
                    if parent_type == "class":
                        func_id = f"{parent_id}.{func_name}"
                        sym_type = "method"
                    else:
                        func_id = f"{rel_path}::{func_name}"
                        sym_type = "function"

                    local_bindings = {}

                    def extract_type_from_call_or_type(type_or_call_node):
                        if type_or_call_node.type == "identifier":
                            return source[
                                type_or_call_node.start_byte : type_or_call_node.end_byte
                            ].decode("utf-8", errors="replace")
                        elif type_or_call_node.type == "attribute":
                            attr_node = type_or_call_node.child_by_field_name(
                                "attribute"
                            )
                            if attr_node:
                                return source[
                                    attr_node.start_byte : attr_node.end_byte
                                ].decode("utf-8", errors="replace")
                        elif type_or_call_node.type == "type":
                            for child in type_or_call_node.children:
                                res = extract_type_from_call_or_type(child)
                                if res:
                                    return res
                        elif type_or_call_node.type == "call":
                            func_node = type_or_call_node.child_by_field_name(
                                "function"
                            )
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
                                    var_name = source[
                                        child.start_byte : child.end_byte
                                    ].decode("utf-8", errors="replace")
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
                                var_name = source[
                                    left.start_byte : left.end_byte
                                ].decode("utf-8", errors="replace")
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
                                var_name = source[
                                    target_node.start_byte : target_node.end_byte
                                ].decode("utf-8", errors="replace")
                                if t_name:
                                    local_bindings[var_name] = t_name

                        for child in n.children:
                            if child.type != "function_definition":
                                collect_local_bindings(child)

                    collect_local_bindings(node)

                    result.nodes.append(
                        NodeSchema(
                            id=func_id,
                            label=func_name,
                            type=sym_type,
                            source_file=rel_path,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            signature=self._get_signature(node, source),
                            docstring=self._get_docstring(node, source),
                            local_bindings=local_bindings,
                        )
                    )

                    result.edges.append(
                        EdgeSchema(
                            source=parent_id, target=func_id, relation="contains"
                        )
                    )

                    scope_stack.append((func_id, sym_type))
                    pushed_scope = True

            elif node_type in ("import_statement", "import_from_statement"):
                if node_type == "import_statement":
                    for child in node.children:
                        if child.type == "dotted_name":
                            module_name = source[
                                child.start_byte : child.end_byte
                            ].decode("utf-8", errors="replace")
                            result.edges.append(
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
                                module_name = source[
                                    name_node.start_byte : name_node.end_byte
                                ].decode("utf-8", errors="replace")
                                alias_name = source[
                                    alias_node.start_byte : alias_node.end_byte
                                ].decode("utf-8", errors="replace")
                                result.edges.append(
                                    EdgeSchema(
                                        source=file_node_id,
                                        target=module_name,
                                        relation="imports",
                                        import_map={alias_name: module_name},
                                    )
                                )
                elif node_type == "import_from_statement":
                    module_node = node.child_by_field_name("module_name")
                    module_name = ""
                    if module_node:
                        module_name = source[
                            module_node.start_byte : module_node.end_byte
                        ].decode("utf-8", errors="replace")

                    dots = ""
                    for child in node.children:
                        if child.type == "relative_source":
                            dots = source[child.start_byte : child.end_byte].decode(
                                "utf-8", errors="replace"
                            )
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
                            name = source[item.start_byte : item.end_byte].decode(
                                "utf-8", errors="replace"
                            )
                            import_map[name] = name
                        elif item.type == "aliased_import":
                            name_node = item.child_by_field_name("name")
                            alias_node = item.child_by_field_name("alias")
                            if name_node and alias_node:
                                name = source[
                                    name_node.start_byte : name_node.end_byte
                                ].decode("utf-8", errors="replace")
                                alias = source[
                                    alias_node.start_byte : alias_node.end_byte
                                ].decode("utf-8", errors="replace")
                                import_map[alias] = name

                    if target_module:
                        result.edges.append(
                            EdgeSchema(
                                source=file_node_id,
                                target=target_module,
                                relation="imports",
                                import_map=import_map,
                            )
                        )

            elif node_type == "call":
                # Function/method call extraction
                func_node = node.child_by_field_name("function")
                if func_node:
                    callee_name = source[
                        func_node.start_byte : func_node.end_byte
                    ].decode("utf-8", errors="replace")
                    # Source of the call is the current function/method, or the file if at top level
                    caller_id = get_current_parent_id()

                    # We record a calls edge; builder will resolve the full ID later
                    result.edges.append(
                        EdgeSchema(
                            source=caller_id, target=callee_name, relation="calls"
                        )
                    )

            # Recurse children
            for child in node.children:
                walk(child)

            if pushed_scope:
                scope_stack.pop()

        walk(root)
        return result
