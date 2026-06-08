import logging
from pathlib import Path
import tree_sitter
import tree_sitter_go
from codegraph.parser.base import BaseParser, ExtractionResult, NodeSchema, EdgeSchema

logger = logging.getLogger(__name__)


class GoParser(BaseParser):
    def __init__(self):
        self.language = tree_sitter.Language(tree_sitter_go.language())
        self.parser = tree_sitter.Parser(self.language)

    def _get_docstring(self, node, source: bytes) -> str:
        """Finds comments immediately preceding the node."""
        docstring = ""
        prev = node.prev_sibling
        comments = []
        while prev and prev.type in ("comment", "line_comment"):
            comment_text = source[prev.start_byte : prev.end_byte].decode(
                "utf-8", errors="replace"
            )
            # Strip comment markers (//)
            clean_text = comment_text.strip().lstrip("//").strip()
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
                signature=f"package {file_path.parent.name or 'main'}",
                docstring=self._get_docstring(root, source),
            )
        )

        def get_receiver_type(method_node) -> str | None:
            receiver = method_node.child_by_field_name("receiver")
            if receiver:
                # Find parameter_declaration in receiver
                for child in receiver.children:
                    if child.type == "parameter_declaration":
                        type_node = child.child_by_field_name("type")
                        if type_node:
                            # Might be *Type, so strip '*'
                            raw_type = source[
                                type_node.start_byte : type_node.end_byte
                            ].decode("utf-8", errors="replace")
                            return raw_type.strip()
            return None

        def walk(node):
            nonlocal result

            if node.type == "ERROR" or (hasattr(node, "is_error") and node.is_error):
                logger.debug(f"Skipping syntax error node in Go AST: {node}")
                return

            node_type = node.type

            if node_type == "type_declaration":
                for child in node.children:
                    if child.type == "type_spec":
                        name_node = child.child_by_field_name("name")
                        if name_node:
                            type_name = source[
                                name_node.start_byte : name_node.end_byte
                            ].decode("utf-8", errors="replace")
                            type_id = f"{rel_path}::{type_name}"

                            sym_type = "struct"
                            for tc in child.children:
                                if tc.type == "interface_type":
                                    sym_type = "interface"
                                    break

                            result.nodes.append(
                                NodeSchema(
                                    id=type_id,
                                    label=type_name,
                                    type=sym_type,
                                    source_file=rel_path,
                                    line_start=child.start_point[0] + 1,
                                    line_end=child.end_point[0] + 1,
                                    signature=f"type {type_name} {sym_type}",
                                    docstring=self._get_docstring(node, source),
                                )
                            )

                            result.edges.append(
                                EdgeSchema(
                                    source=file_node_id,
                                    target=type_id,
                                    relation="contains",
                                )
                            )

            elif node_type == "function_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    func_id = f"{rel_path}::{func_name}"

                    result.nodes.append(
                        NodeSchema(
                            id=func_id,
                            label=func_name,
                            type="function",
                            source_file=rel_path,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            signature=self._get_signature(node, source),
                            docstring=self._get_docstring(node, source),
                        )
                    )

                    result.edges.append(
                        EdgeSchema(
                            source=file_node_id, target=func_id, relation="contains"
                        )
                    )

            elif node_type == "method_declaration":
                name_node = node.child_by_field_name("name")
                if name_node:
                    method_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    receiver_type = get_receiver_type(node)

                    if receiver_type:
                        parent_id = f"{rel_path}::{receiver_type}"
                        method_id = f"{parent_id}.{method_name}"
                        relation = "contains"
                    else:
                        parent_id = file_node_id
                        method_id = f"{rel_path}::{method_name}"
                        relation = "contains"

                    result.nodes.append(
                        NodeSchema(
                            id=method_id,
                            label=method_name,
                            type="method",
                            source_file=rel_path,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            signature=self._get_signature(node, source),
                            docstring=self._get_docstring(node, source),
                        )
                    )

                    result.edges.append(
                        EdgeSchema(
                            source=parent_id, target=method_id, relation=relation
                        )
                    )

            elif node_type == "import_spec":
                path_node = node.child_by_field_name("path")
                if path_node:
                    import_path = source[
                        path_node.start_byte : path_node.end_byte
                    ].decode("utf-8", errors="replace")
                    import_path = import_path.strip("\"'")

                    pkg_name = import_path.split("/")[-1]
                    import_map = {}

                    name_node = node.child_by_field_name("name")
                    if name_node:
                        local_name = source[
                            name_node.start_byte : name_node.end_byte
                        ].decode("utf-8", errors="replace")
                        if local_name == ".":
                            import_map["*"] = "*"
                        else:
                            import_map[local_name] = pkg_name
                    else:
                        import_map[pkg_name] = pkg_name

                    result.edges.append(
                        EdgeSchema(
                            source=file_node_id,
                            target=import_path,
                            relation="imports",
                            import_map=import_map,
                        )
                    )

            elif node_type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node:
                    callee_name = source[
                        func_node.start_byte : func_node.end_byte
                    ].decode("utf-8", errors="replace")
                    caller_id = file_node_id
                    curr = node.parent
                    while curr:
                        if curr.type in ("function_declaration", "method_declaration"):
                            c_name_node = curr.child_by_field_name("name")
                            if c_name_node:
                                c_name = source[
                                    c_name_node.start_byte : c_name_node.end_byte
                                ].decode("utf-8", errors="replace")
                                if curr.type == "method_declaration":
                                    r_type = get_receiver_type(curr)
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

            for child in node.children:
                walk(child)

        walk(root)
        return result
