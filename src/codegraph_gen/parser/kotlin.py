import logging
from pathlib import Path
import tree_sitter
import tree_sitter_kotlin
from codegraph_gen.parser.base import (
    BaseParser,
    ExtractionResult,
    NodeSchema,
    EdgeSchema,
)

logger = logging.getLogger(__name__)


class KotlinParser(BaseParser):
    def __init__(self):
        self.language = tree_sitter.Language(tree_sitter_kotlin.language())
        self.parser = tree_sitter.Parser(self.language)

    def _get_docstring(self, node, source: bytes) -> str:
        """Finds comments immediately preceding the node."""
        docstring = ""
        prev = node.prev_sibling
        comments = []
        while prev and prev.type in ("comment", "line_comment", "block_comment"):
            comment_text = source[prev.start_byte : prev.end_byte].decode(
                "utf-8", errors="replace"
            )
            # Strip comment markers (//, /*, /**, *)
            clean_text = (
                comment_text.strip()
                .lstrip("/*")
                .rstrip("*/")
                .lstrip("*")
                .lstrip("/")
                .strip()
            )
            comments.append(clean_text)
            prev = prev.prev_sibling

        if comments:
            docstring = "\n".join(reversed(comments))
        return docstring

    def _get_signature(self, node, source: bytes) -> str:
        body = None
        for child in node.children:
            if child.type in (
                "class_body",
                "function_body",
                "block",
            ):
                body = child
                break
        if body:
            end_byte = body.start_byte
            sig_bytes = source[node.start_byte : end_byte]
            sig = sig_bytes.decode("utf-8", errors="replace").strip()
            if sig.endswith("{"):
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

        # Add file node
        file_node_id = rel_path
        result.nodes.append(
            NodeSchema(
                id=file_node_id,
                label=file_path.name,
                type="file",
                source_file=rel_path,
                line_start=1,
                line_end=len(source.splitlines()) or 1,
                signature=f"package {file_path.stem}",
                docstring=self._get_docstring(root, source),
            )
        )

        scope_stack = [(file_node_id, "file")]

        def get_current_parent_id():
            return scope_stack[-1][0] if scope_stack else file_node_id

        def walk(node):
            nonlocal result

            if node.type == "ERROR" or (hasattr(node, "is_error") and node.is_error):
                logger.debug(f"Skipping syntax error node in Kotlin AST: {node}")
                return

            node_type = node.type
            pushed_scope = False

            if node_type in ("class_declaration", "object_declaration"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    parent_id = get_current_parent_id()
                    class_id = f"{rel_path}::{class_name}"

                    if node_type == "class_declaration":
                        is_interface = any(c.type == "interface" for c in node.children)
                        sym_type = "interface" if is_interface else "class"
                    else:
                        sym_type = "class"  # Map object declaration to class

                    result.nodes.append(
                        NodeSchema(
                            id=class_id,
                            label=class_name,
                            type=sym_type,
                            source_file=rel_path,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            signature=self._get_signature(node, source),
                            docstring=self._get_docstring(node, source),
                        )
                    )

                    result.edges.append(
                        EdgeSchema(
                            source=parent_id, target=class_id, relation="contains"
                        )
                    )

                    # Check inheritance / delegation specifiers
                    for child in node.children:
                        if child.type == "delegation_specifiers":
                            for spec in child.children:
                                if spec.type == "delegation_specifier":

                                    def find_user_type(n):
                                        if n.type == "user_type":
                                            return n
                                        for c in n.children:
                                            res = find_user_type(c)
                                            if res:
                                                return res
                                        return None

                                    user_type_node = find_user_type(spec)
                                    if user_type_node:
                                        id_node = next(
                                            (
                                                c
                                                for c in user_type_node.children
                                                if c.type == "identifier"
                                            ),
                                            None,
                                        )
                                        if id_node:
                                            parent_name = source[
                                                id_node.start_byte : id_node.end_byte
                                            ].decode("utf-8", errors="replace")
                                            result.edges.append(
                                                EdgeSchema(
                                                    source=class_id,
                                                    target=parent_name,
                                                    relation="inherits",
                                                )
                                            )

                    scope_stack.append((class_id, sym_type))
                    pushed_scope = True

            elif node_type == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    parent_id = get_current_parent_id()
                    parent_type = scope_stack[-1][1] if scope_stack else "file"

                    if parent_type in ("class", "interface"):
                        func_id = f"{parent_id}.{func_name}"
                        sym_type = "method"
                    else:
                        func_id = f"{rel_path}::{func_name}"
                        sym_type = "function"

                    local_bindings = {}

                    def extract_type_from_kt_node(kt_node):
                        if kt_node.type == "user_type":
                            id_node = next(
                                (c for c in kt_node.children if c.type == "identifier"),
                                None,
                            )
                            if id_node:
                                return source[
                                    id_node.start_byte : id_node.end_byte
                                ].decode("utf-8", errors="replace")
                        elif kt_node.type == "call_expression":
                            callee = kt_node.child_by_field_name("constructor") or next(
                                (c for c in kt_node.children if c.type == "identifier"),
                                None,
                            )
                            if callee:
                                return source[
                                    callee.start_byte : callee.end_byte
                                ].decode("utf-8", errors="replace")
                        for child in kt_node.children:
                            res = extract_type_from_kt_node(child)
                            if res:
                                return res
                        return None

                    def collect_local_bindings(n):
                        if n.type == "parameter":
                            id_node = next(
                                (c for c in n.children if c.type == "identifier"), None
                            )
                            type_node = next(
                                (c for c in n.children if c.type == "user_type"), None
                            )
                            if id_node and type_node:
                                var_name = source[
                                    id_node.start_byte : id_node.end_byte
                                ].decode("utf-8", errors="replace")
                                t_name = extract_type_from_kt_node(type_node)
                                if t_name:
                                    local_bindings[var_name] = t_name
                        elif n.type == "property_declaration":
                            var_decl = next(
                                (
                                    c
                                    for c in n.children
                                    if c.type == "variable_declaration"
                                ),
                                None,
                            )
                            val_expr = next(
                                (c for c in n.children if c.type == "call_expression"),
                                None,
                            )
                            if var_decl:
                                id_node = next(
                                    (
                                        c
                                        for c in var_decl.children
                                        if c.type == "identifier"
                                    ),
                                    None,
                                )
                                type_node = next(
                                    (
                                        c
                                        for c in var_decl.children
                                        if c.type == "user_type"
                                    ),
                                    None,
                                )
                                if id_node:
                                    var_name = source[
                                        id_node.start_byte : id_node.end_byte
                                    ].decode("utf-8", errors="replace")
                                    if type_node:
                                        t_name = extract_type_from_kt_node(type_node)
                                        if t_name:
                                            local_bindings[var_name] = t_name
                                    elif val_expr:
                                        t_name = extract_type_from_kt_node(val_expr)
                                        if t_name:
                                            local_bindings[var_name] = t_name

                        for child in n.children:
                            if child.type not in (
                                "function_declaration",
                                "class_declaration",
                                "object_declaration",
                            ):
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

            elif node_type == "import":
                qual_id_node = next(
                    (c for c in node.children if c.type == "qualified_identifier"), None
                )
                if qual_id_node:
                    target = source[
                        qual_id_node.start_byte : qual_id_node.end_byte
                    ].decode("utf-8", errors="replace")
                    is_wildcard = any(c.type == "*" for c in node.children)
                    alias = None

                    as_idx = next(
                        (i for i, c in enumerate(node.children) if c.type == "as"), -1
                    )
                    if as_idx != -1 and as_idx + 1 < len(node.children):
                        alias_node = node.children[as_idx + 1]
                        if alias_node.type == "identifier":
                            alias = source[
                                alias_node.start_byte : alias_node.end_byte
                            ].decode("utf-8", errors="replace")

                    if is_wildcard:
                        import_map = {"*": "*"}
                    elif alias:
                        last_part = target.split(".")[-1]
                        import_map = {alias: last_part}
                    else:
                        last_part = target.split(".")[-1]
                        import_map = {last_part: last_part}

                    result.edges.append(
                        EdgeSchema(
                            source=file_node_id,
                            target=target,
                            relation="imports",
                            import_map=import_map,
                        )
                    )

            elif node_type == "call_expression":
                func_node = None
                for child in node.children:
                    if child.type in ("identifier", "navigation_expression"):
                        func_node = child
                        break
                if func_node:
                    callee_name = source[
                        func_node.start_byte : func_node.end_byte
                    ].decode("utf-8", errors="replace")
                    caller_id = get_current_parent_id()
                    result.edges.append(
                        EdgeSchema(
                            source=caller_id, target=callee_name, relation="calls"
                        )
                    )

            for child in node.children:
                walk(child)

            if pushed_scope:
                scope_stack.pop()

        walk(root)
        return result
