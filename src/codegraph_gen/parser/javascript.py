import logging
from pathlib import Path
import tree_sitter
import tree_sitter_javascript
import tree_sitter_typescript
from codegraph_gen.parser.base import (
    BaseParser,
    ExtractionResult,
    NodeSchema,
    EdgeSchema,
)

logger = logging.getLogger(__name__)


class JavaScriptParser(BaseParser):
    def __init__(self):
        # Cache parsers for javascript, typescript and tsx
        self.js_lang = tree_sitter.Language(tree_sitter_javascript.language())
        self.ts_lang = tree_sitter.Language(
            tree_sitter_typescript.language_typescript()
        )
        self.tsx_lang = tree_sitter.Language(tree_sitter_typescript.language_tsx())

        self.js_parser = tree_sitter.Parser(self.js_lang)
        self.ts_parser = tree_sitter.Parser(self.ts_lang)
        self.tsx_parser = tree_sitter.Parser(self.tsx_lang)

    def _get_docstring(self, node, source: bytes) -> str:
        """Finds comments immediately preceding the node."""
        # Tree-sitter doesn't always attach comments to nodes, but we can look for
        # sibling nodes of type 'comment' that end right before this node starts.
        docstring = ""
        prev = node.prev_sibling
        comments = []
        while prev and prev.type in ("comment", "line_comment", "block_comment"):
            comment_text = source[prev.start_byte : prev.end_byte].decode(
                "utf-8", errors="replace"
            )
            # Strip comment markers
            clean_text = (
                comment_text.strip().lstrip("/*").rstrip("*/").lstrip("*").strip()
            )
            comments.append(clean_text)
            prev = prev.prev_sibling

        if comments:
            docstring = "\n".join(reversed(comments))
        return docstring

    def _get_signature(self, node, source: bytes) -> str:
        body = node.child_by_field_name("body")
        if body:
            end_byte = body.start_byte
            sig_bytes = source[node.start_byte : end_byte]
            sig = sig_bytes.decode("utf-8", errors="replace").strip()
            # Trim trailing open curly brace
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

        ext = file_path.suffix.lower()
        if ext == ".tsx":
            parser = self.tsx_parser
        elif ext in (".ts", ".cts", ".mts"):
            parser = self.ts_parser
        else:
            parser = self.js_parser

        tree = parser.parse(source)
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
                signature=f"module {file_path.name}",
                docstring=self._get_docstring(root, source),
            )
        )

        scope_stack = [(file_node_id, "file")]

        def get_current_parent_id():
            return scope_stack[-1][0] if scope_stack else file_node_id

        def walk(node):
            nonlocal result

            if node.type == "ERROR" or (hasattr(node, "is_error") and node.is_error):
                logger.debug(f"Skipping syntax error node in JS/TS AST: {node}")
                return

            node_type = node.type
            pushed_scope = False

            if node_type in ("class_declaration", "interface_declaration"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    parent_id = get_current_parent_id()

                    class_id = f"{rel_path}::{class_name}"
                    sym_type = (
                        "class" if node_type == "class_declaration" else "interface"
                    )

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

                    # Inheritance: heritage / extends clause
                    for child in node.children:
                        if child.type in ("class_heritage", "interface_heritage"):
                            # extends Expression
                            for sub in child.children:
                                if sub.type in ("identifier", "nested_identifier"):
                                    parent_class_name = source[
                                        sub.start_byte : sub.end_byte
                                    ].decode("utf-8", errors="replace")
                                    result.edges.append(
                                        EdgeSchema(
                                            source=class_id,
                                            target=parent_class_name,
                                            relation="inherits",
                                        )
                                    )

                    scope_stack.append((class_id, sym_type))
                    pushed_scope = True

            elif node_type in ("function_declaration", "method_definition"):
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

                    def extract_type_from_ts_node(ts_node):
                        if ts_node.type == "type_identifier":
                            return source[ts_node.start_byte : ts_node.end_byte].decode(
                                "utf-8", errors="replace"
                            )
                        elif ts_node.type == "property_identifier":
                            return source[ts_node.start_byte : ts_node.end_byte].decode(
                                "utf-8", errors="replace"
                            )
                        elif ts_node.type == "nested_type_identifier":
                            for child in reversed(ts_node.children):
                                if child.type in ("type_identifier", "identifier"):
                                    return extract_type_from_ts_node(child)
                        elif ts_node.type == "generic_type":
                            type_node = ts_node.child_by_field_name("name") or (
                                ts_node.children[0] if ts_node.children else None
                            )
                            if type_node:
                                return extract_type_from_ts_node(type_node)
                        elif ts_node.type == "new_expression":
                            constructor_node = ts_node.child_by_field_name(
                                "constructor"
                            )
                            if constructor_node:
                                if constructor_node.type == "identifier":
                                    return source[
                                        constructor_node.start_byte : constructor_node.end_byte
                                    ].decode("utf-8", errors="replace")
                                elif constructor_node.type == "member_expression":
                                    prop = constructor_node.child_by_field_name(
                                        "property"
                                    )
                                    if prop:
                                        return source[
                                            prop.start_byte : prop.end_byte
                                        ].decode("utf-8", errors="replace")
                        elif ts_node.type == "type_annotation":
                            for child in ts_node.children:
                                res = extract_type_from_ts_node(child)
                                if res:
                                    return res
                        for child in ts_node.children:
                            res = extract_type_from_ts_node(child)
                            if res:
                                return res
                        return None

                    def collect_local_bindings(n):
                        if n.type in ("required_parameter", "optional_parameter"):
                            pattern = n.child_by_field_name("pattern")
                            type_node = n.child_by_field_name("type")
                            if pattern and pattern.type == "identifier" and type_node:
                                var_name = source[
                                    pattern.start_byte : pattern.end_byte
                                ].decode("utf-8", errors="replace")
                                t_name = extract_type_from_ts_node(type_node)
                                if t_name:
                                    local_bindings[var_name] = t_name
                        elif n.type == "variable_declarator":
                            name_node = n.child_by_field_name("name")
                            value_node = n.child_by_field_name("value")
                            type_node = n.child_by_field_name("type")
                            if name_node and name_node.type == "identifier":
                                var_name = source[
                                    name_node.start_byte : name_node.end_byte
                                ].decode("utf-8", errors="replace")
                                if type_node:
                                    t_name = extract_type_from_ts_node(type_node)
                                    if t_name:
                                        local_bindings[var_name] = t_name
                                elif value_node and value_node.type == "new_expression":
                                    t_name = extract_type_from_ts_node(value_node)
                                    if t_name:
                                        local_bindings[var_name] = t_name

                        for child in n.children:
                            if child.type not in (
                                "function_declaration",
                                "method_definition",
                                "class_declaration",
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

            elif node_type == "import_statement":
                source_node = node.child_by_field_name("source")
                if source_node:
                    import_path = source[
                        source_node.start_byte : source_node.end_byte
                    ].decode("utf-8", errors="replace")
                    import_path = import_path.strip("\"'")

                    import_map = {}
                    clause_node = None
                    for child in node.children:
                        if child.type == "import_clause":
                            clause_node = child
                            break

                    if clause_node:
                        for c in clause_node.children:
                            if c.type == "identifier":
                                name = source[c.start_byte : c.end_byte].decode(
                                    "utf-8", errors="replace"
                                )
                                import_map[name] = "default"
                            elif c.type == "namespace_import":
                                for sub in c.children:
                                    if sub.type == "identifier":
                                        name = source[
                                            sub.start_byte : sub.end_byte
                                        ].decode("utf-8", errors="replace")
                                        import_map[name] = "*"
                                        break
                            elif c.type == "named_imports":
                                for spec in c.children:
                                    if spec.type == "import_specifier":
                                        name_node = spec.child_by_field_name("name")
                                        alias_node = spec.child_by_field_name("alias")
                                        if name_node and alias_node:
                                            name = source[
                                                name_node.start_byte : name_node.end_byte
                                            ].decode("utf-8", errors="replace")
                                            alias = source[
                                                alias_node.start_byte : alias_node.end_byte
                                            ].decode("utf-8", errors="replace")
                                            import_map[alias] = name
                                        elif name_node:
                                            name = source[
                                                name_node.start_byte : name_node.end_byte
                                            ].decode("utf-8", errors="replace")
                                            import_map[name] = name

                    result.edges.append(
                        EdgeSchema(
                            source=file_node_id,
                            target=import_path,
                            relation="imports",
                            import_map=import_map,
                        )
                    )

            elif node_type in ("call_expression", "new_expression"):
                func_node = node.child_by_field_name("function")
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
