import logging
from pathlib import Path
import tree_sitter
import tree_sitter_rust
from codegraph_gen.parser.base import BaseParser, ExtractionResult, NodeSchema, EdgeSchema

logger = logging.getLogger(__name__)


class RustParser(BaseParser):
    def __init__(self):
        self.language = tree_sitter.Language(tree_sitter_rust.language())
        self.parser = tree_sitter.Parser(self.language)

    def _get_docstring(self, node, source: bytes) -> str:
        """Finds comments immediately preceding the node."""
        docstring = ""
        prev = node.prev_sibling
        comments = []
        while prev and prev.type in ("line_comment", "block_comment"):
            comment_text = source[prev.start_byte : prev.end_byte].decode(
                "utf-8", errors="replace"
            )
            # Strip comment markers (/// or //)
            clean_text = comment_text.strip().lstrip("/").strip()
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
                signature=f"mod {file_path.stem}",
                docstring=self._get_docstring(root, source),
            )
        )

        def get_impl_type(impl_node) -> str | None:
            type_node = impl_node.child_by_field_name("type")
            if type_node:
                raw_type = source[type_node.start_byte : type_node.end_byte].decode(
                    "utf-8", errors="replace"
                )
                return raw_type.strip()
            return None

        def walk(node, current_impl_type=None):
            nonlocal result

            if node.type == "ERROR" or (hasattr(node, "is_error") and node.is_error):
                logger.debug(f"Skipping syntax error node in Rust AST: {node}")
                return

            node_type = node.type
            pushed_impl = None

            if node_type in ("struct_item", "enum_item", "trait_item"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    item_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    item_id = f"{rel_path}::{item_name}"

                    sym_type = "struct"
                    if node_type == "enum_item":
                        sym_type = "enum"
                    elif node_type == "trait_item":
                        sym_type = "interface"  # map trait to interface for consistency

                    result.nodes.append(
                        NodeSchema(
                            id=item_id,
                            label=item_name,
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
                            source=file_node_id, target=item_id, relation="contains"
                        )
                    )

            elif node_type == "impl_item":
                impl_type = get_impl_type(node)
                if impl_type:
                    pushed_impl = impl_type

                    # Ensure struct node is created if it hasn't been yet (impls can define methods for external/internal types)
                    type_id = f"{rel_path}::{impl_type}"

                    # We might also link impl to trait if it's trait implementation
                    trait_node = node.child_by_field_name("trait")
                    if trait_node:
                        trait_name = source[
                            trait_node.start_byte : trait_node.end_byte
                        ].decode("utf-8", errors="replace")
                        result.edges.append(
                            EdgeSchema(
                                source=type_id, target=trait_name, relation="implements"
                            )
                        )

            elif node_type == "function_item":
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")

                    if current_impl_type:
                        parent_id = f"{rel_path}::{current_impl_type}"
                        func_id = f"{parent_id}.{func_name}"
                        sym_type = "method"
                        relation = "contains"
                    else:
                        parent_id = file_node_id
                        func_id = f"{rel_path}::{func_name}"
                        sym_type = "function"
                        relation = "contains"

                    local_bindings = {}

                    def extract_rust_type(type_node) -> str | None:
                        if type_node.type == "type_identifier":
                            return source[
                                type_node.start_byte : type_node.end_byte
                            ].decode("utf-8", errors="replace")
                        elif type_node.type in (
                            "pointer_type",
                            "reference_type",
                            "sliced_type",
                            "array_type",
                        ):
                            for child in type_node.children:
                                if child.type not in ("&", "*", "mut", "const"):
                                    res = extract_rust_type(child)
                                    if res:
                                        return res
                        elif type_node.type == "generic_type":
                            type_id_node = type_node.child_by_field_name("type")
                            if type_id_node:
                                return extract_rust_type(type_id_node)
                        return None

                    def collect_local_bindings(n):
                        if n.type == "parameter":
                            pattern_node = n.child_by_field_name("pattern")
                            type_node = n.child_by_field_name("type")
                            if pattern_node and type_node:
                                var_name = None
                                if pattern_node.type == "identifier":
                                    var_name = source[
                                        pattern_node.start_byte : pattern_node.end_byte
                                    ].decode("utf-8", errors="replace")
                                elif pattern_node.type == "mut_pattern":
                                    inner = pattern_node.child_by_field_name("pattern")
                                    if inner and inner.type == "identifier":
                                        var_name = source[
                                            inner.start_byte : inner.end_byte
                                        ].decode("utf-8", errors="replace")
                                if var_name:
                                    t_name = extract_rust_type(type_node)
                                    if t_name:
                                        local_bindings[var_name] = t_name

                        elif n.type == "let_declaration":
                            pattern_node = n.child_by_field_name("pattern")
                            type_node = n.child_by_field_name("type")
                            value_node = n.child_by_field_name("value")

                            var_name = None
                            if pattern_node:
                                if pattern_node.type == "identifier":
                                    var_name = source[
                                        pattern_node.start_byte : pattern_node.end_byte
                                    ].decode("utf-8", errors="replace")
                                elif pattern_node.type == "mut_pattern":
                                    inner = pattern_node.child_by_field_name("pattern")
                                    if inner and inner.type == "identifier":
                                        var_name = source[
                                            inner.start_byte : inner.end_byte
                                        ].decode("utf-8", errors="replace")

                            if var_name:
                                type_name = None
                                if type_node:
                                    type_name = extract_rust_type(type_node)
                                elif value_node:
                                    if value_node.type == "call_expression":
                                        func = value_node.child_by_field_name(
                                            "function"
                                        )
                                        if func and func.type == "scoped_identifier":
                                            path_node = func.child_by_field_name("path")
                                            if path_node:
                                                type_name = source[
                                                    path_node.start_byte : path_node.end_byte
                                                ].decode("utf-8", errors="replace")
                                    elif value_node.type == "struct_expression":
                                        name_node = value_node.child_by_field_name(
                                            "name"
                                        )
                                        if name_node:
                                            type_name = extract_rust_type(name_node)
                                    elif value_node.type == "match_expression":
                                        subject_node = value_node.child_by_field_name(
                                            "value"
                                        )
                                        if not subject_node:
                                            for child in value_node.children:
                                                if child.type in ("match_block", "{"):
                                                    break
                                                if child.type != "match":
                                                    subject_node = child
                                                    break
                                        if subject_node:
                                            sub_ids = []

                                            def collect_ids(sub_n):
                                                if sub_n.type == "identifier":
                                                    id_str = source[
                                                        sub_n.start_byte : sub_n.end_byte
                                                    ].decode("utf-8", errors="replace")
                                                    sub_ids.append(id_str)
                                                for c in sub_n.children:
                                                    collect_ids(c)

                                            collect_ids(subject_node)
                                            for sub_id in sub_ids:
                                                if sub_id in local_bindings:
                                                    type_name = local_bindings[sub_id]
                                                    break
                                if type_name:
                                    local_bindings[var_name] = type_name

                        for child in n.children:
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
                        EdgeSchema(source=parent_id, target=func_id, relation=relation)
                    )

            elif node_type == "use_declaration":

                def parse_use_item(n, prefix=""):
                    if n.type == "use_path":
                        parts = []
                        use_list_node = None
                        as_clause_node = None

                        for child in n.children:
                            if child.type == "use_list":
                                use_list_node = child
                            elif child.type == "use_as_clause":
                                as_clause_node = child
                            elif child.type in (
                                "identifier",
                                "scoped_identifier",
                                "use_path",
                            ):
                                parts.append(
                                    source[child.start_byte : child.end_byte].decode(
                                        "utf-8", errors="replace"
                                    )
                                )

                        current_path = "::".join(parts)
                        full_path = (
                            f"{prefix}::{current_path}" if prefix else current_path
                        )

                        if use_list_node:
                            for sub in use_list_node.children:
                                if sub.type in (
                                    "use_path",
                                    "identifier",
                                    "scoped_identifier",
                                    "use_as_clause",
                                ):
                                    parse_use_item(sub, full_path)
                        elif as_clause_node:
                            path_node = as_clause_node.child_by_field_name("path")
                            alias_node = as_clause_node.child_by_field_name("alias")
                            if path_node and alias_node:
                                sub_path = source[
                                    path_node.start_byte : path_node.end_byte
                                ].decode("utf-8", errors="replace")
                                alias_name = source[
                                    alias_node.start_byte : alias_node.end_byte
                                ].decode("utf-8", errors="replace")
                                item_path = (
                                    f"{full_path}::{sub_path}"
                                    if full_path
                                    else sub_path
                                )
                                last_symbol = item_path.split("::")[-1]
                                result.edges.append(
                                    EdgeSchema(
                                        source=file_node_id,
                                        target=item_path,
                                        relation="imports",
                                        import_map={alias_name: last_symbol},
                                    )
                                )
                        else:
                            last_symbol = full_path.split("::")[-1]
                            result.edges.append(
                                EdgeSchema(
                                    source=file_node_id,
                                    target=full_path,
                                    relation="imports",
                                    import_map={last_symbol: last_symbol},
                                )
                            )

                    elif n.type == "use_as_clause":
                        path_node = n.child_by_field_name("path")
                        alias_node = n.child_by_field_name("alias")
                        if path_node and alias_node:
                            path_name = source[
                                path_node.start_byte : path_node.end_byte
                            ].decode("utf-8", errors="replace")
                            alias_name = source[
                                alias_node.start_byte : alias_node.end_byte
                            ].decode("utf-8", errors="replace")
                            full_path = (
                                f"{prefix}::{path_name}" if prefix else path_name
                            )
                            last_symbol = full_path.split("::")[-1]
                            result.edges.append(
                                EdgeSchema(
                                    source=file_node_id,
                                    target=full_path,
                                    relation="imports",
                                    import_map={alias_name: last_symbol},
                                )
                            )
                    elif n.type in ("identifier", "scoped_identifier"):
                        name = source[n.start_byte : n.end_byte].decode(
                            "utf-8", errors="replace"
                        )
                        full_path = f"{prefix}::{name}" if prefix else name
                        last_symbol = full_path.split("::")[-1]
                        result.edges.append(
                            EdgeSchema(
                                source=file_node_id,
                                target=full_path,
                                relation="imports",
                                import_map={last_symbol: last_symbol},
                            )
                        )
                    elif n.type == "self_literal":
                        full_path = prefix
                        last_symbol = full_path.split("::")[-1] if full_path else "self"
                        result.edges.append(
                            EdgeSchema(
                                source=file_node_id,
                                target=full_path,
                                relation="imports",
                                import_map={last_symbol: last_symbol},
                            )
                        )

                for child in node.children:
                    if child.type in (
                        "use_path",
                        "use_list",
                        "identifier",
                        "scoped_identifier",
                        "use_as_clause",
                    ):
                        parse_use_item(child)

            elif node_type in ("call_expression", "method_call_expression"):
                callee_name = None
                if node_type == "call_expression":
                    func_node = node.child_by_field_name("function")
                    if func_node:
                        callee_name = source[
                            func_node.start_byte : func_node.end_byte
                        ].decode("utf-8", errors="replace")
                else:
                    value_node = node.child_by_field_name("value")
                    name_node = node.child_by_field_name("name")
                    if value_node and name_node:
                        receiver = (
                            source[value_node.start_byte : value_node.end_byte]
                            .decode("utf-8", errors="replace")
                            .strip()
                        )
                        method = (
                            source[name_node.start_byte : name_node.end_byte]
                            .decode("utf-8", errors="replace")
                            .strip()
                        )
                        callee_name = f"{receiver}.{method}"

                if callee_name:
                    # Find enclosing caller function/method ID
                    caller_id = file_node_id
                    curr = node.parent
                    while curr:
                        if curr.type == "function_item":
                            c_name_node = curr.child_by_field_name("name")
                            if c_name_node:
                                c_name = source[
                                    c_name_node.start_byte : c_name_node.end_byte
                                ].decode("utf-8", errors="replace")
                                # Check if inside an impl block
                                impl_node = curr.parent
                                while impl_node and impl_node.type != "impl_item":
                                    impl_node = impl_node.parent
                                if impl_node:
                                    r_type = get_impl_type(impl_node)
                                    if r_type:
                                        caller_id = f"{rel_path}::{r_type}.{c_name}"
                                    else:
                                        caller_id = f"{rel_path}::{c_name}"
                                else:
                                    caller_id = f"{rel_path}::{c_name}"
                            break
                        curr = curr.parent

                    result.edges.append(
                        EdgeSchema(
                            source=caller_id, target=callee_name, relation="calls"
                        )
                    )

            # Recurse children
            impl_context = pushed_impl if pushed_impl else current_impl_type
            for child in node.children:
                walk(child, impl_context)

        walk(root)
        return result
