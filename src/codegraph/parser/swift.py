import logging
from pathlib import Path
import tree_sitter
import tree_sitter_swift
from codegraph.parser.base import BaseParser, ExtractionResult, NodeSchema, EdgeSchema

logger = logging.getLogger(__name__)


class SwiftParser(BaseParser):
    def __init__(self):
        self.language = tree_sitter.Language(tree_sitter_swift.language())
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
            # Strip comment markers (///, //, /*)
            clean_text = comment_text.strip().lstrip("/").strip()
            comments.append(clean_text)
            prev = prev.prev_sibling

        if comments:
            docstring = "\n".join(reversed(comments))
        return docstring

    def _get_signature(self, node, source: bytes) -> str:
        # For Swift, we find body child or child starting with '{'
        body = None
        for child in node.children:
            if child.type in (
                "class_body",
                "struct_body",
                "protocol_body",
                "enum_body",
                "function_body",
                "brace_item_list",
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
                signature=f"module {file_path.stem}",
                docstring=self._get_docstring(root, source),
            )
        )

        scope_stack = [(file_node_id, "file")]

        def get_current_parent_id():
            return scope_stack[-1][0] if scope_stack else file_node_id

        def walk(node):
            nonlocal result

            if node.type == "ERROR" or (hasattr(node, "is_error") and node.is_error):
                logger.debug(f"Skipping syntax error node in Swift AST: {node}")
                return

            node_type = node.type
            pushed_scope = False

            if node_type in (
                "class_declaration",
                "struct_declaration",
                "protocol_declaration",
                "enum_declaration",
            ):
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = source[
                        name_node.start_byte : name_node.end_byte
                    ].decode("utf-8", errors="replace")
                    parent_id = get_current_parent_id()
                    class_id = f"{rel_path}::{class_name}"

                    sym_type = "class"
                    if node_type == "struct_declaration":
                        sym_type = "struct"
                    elif node_type == "protocol_declaration":
                        sym_type = "interface"
                    elif node_type == "enum_declaration":
                        sym_type = "enum"

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

                    # Protocol conformances or subclassing (inheritance) can be found in children
                    # Swift uses type_inheritance_clause
                    for child in node.children:
                        if child.type == "type_inheritance_clause":
                            for sub in child.children:
                                if sub.type == "type_identifier":
                                    parent_name = source[
                                        sub.start_byte : sub.end_byte
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

            elif node_type in (
                "function_declaration",
                "init_declaration",
                "deinit_declaration",
            ):
                func_name = None
                if node_type == "function_declaration":
                    name_node = node.child_by_field_name("name")
                    if name_node:
                        func_name = source[
                            name_node.start_byte : name_node.end_byte
                        ].decode("utf-8", errors="replace")
                elif node_type == "init_declaration":
                    func_name = "init"
                elif node_type == "deinit_declaration":
                    func_name = "deinit"

                if func_name:
                    parent_id = get_current_parent_id()
                    parent_type = scope_stack[-1][1] if scope_stack else "file"

                    if parent_type in ("class", "struct", "interface", "enum"):
                        func_id = f"{parent_id}.{func_name}"
                        sym_type = "method"
                    else:
                        func_id = f"{rel_path}::{func_name}"
                        sym_type = "function"

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
                        )
                    )

                    result.edges.append(
                        EdgeSchema(
                            source=parent_id, target=func_id, relation="contains"
                        )
                    )

                    scope_stack.append((func_id, sym_type))
                    pushed_scope = True

            elif node_type == "import_declaration":
                # import UIKit or import class Module.Class
                # Find path/identifier children
                path_parts = []
                for child in node.children:
                    if child.type in ("simple_identifier", "navigation_expression"):
                        path_parts.append(
                            source[child.start_byte : child.end_byte].decode(
                                "utf-8", errors="replace"
                            )
                        )
                if path_parts:
                    import_path = ".".join(path_parts)
                    result.edges.append(
                        EdgeSchema(
                            source=file_node_id, target=import_path, relation="imports"
                        )
                    )

            elif node_type == "call_expression":
                # Swift call expression contains function name and arguments
                # Find the child that represents the function
                func_node = None
                for child in node.children:
                    # It could be simple_identifier, navigation_expression, etc.
                    if child.type in ("simple_identifier", "navigation_expression"):
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

            # Recurse children
            for child in node.children:
                walk(child)

            if pushed_scope:
                scope_stack.pop()

        walk(root)
        return result
