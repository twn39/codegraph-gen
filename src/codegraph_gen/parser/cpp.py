import logging
from pathlib import Path
import tree_sitter
from codegraph_gen.parser.base import (
    BaseParser,
    ExtractionResult,
    NodeSchema,
    EdgeSchema,
)

logger = logging.getLogger(__name__)


class CCppParser(BaseParser):
    def __init__(self, lang_module):
        self.language = tree_sitter.Language(lang_module.language())
        self.parser = tree_sitter.Parser(self.language)

    def _get_declarator_name(self, node, source: bytes) -> str:
        if not node:
            return ""
        if node.type in ("identifier", "field_identifier", "destructor_name"):
            return source[node.start_byte : node.end_byte].decode(
                "utf-8", errors="replace"
            )
        elif node.type in ("qualified_identifier", "operator_name"):
            return source[node.start_byte : node.end_byte].decode(
                "utf-8", errors="replace"
            )
        elif node.type in (
            "pointer_declarator",
            "reference_declarator",
            "parenthesized_declarator",
            "array_declarator",
        ):
            decl = node.child_by_field_name("declarator")
            if decl:
                return self._get_declarator_name(decl, source)
        elif node.type == "function_declarator":
            decl = node.child_by_field_name("declarator")
            if decl:
                return self._get_declarator_name(decl, source)
        # Search all children for identifier/qualified_identifier/etc.
        for child in node.children:
            name = self._get_declarator_name(child, source)
            if name:
                return name
        return ""

    def _get_docstring(self, node, source: bytes) -> str:
        docstring = ""
        prev = node.prev_sibling
        comments = []
        while prev and prev.type in ("comment", "line_comment", "block_comment"):
            comment_text = source[prev.start_byte : prev.end_byte].decode(
                "utf-8", errors="replace"
            )
            # Strip comment markers (//, /*, */, ///)
            clean_text = comment_text.strip().lstrip("/").strip()
            if clean_text.endswith("*/"):
                clean_text = clean_text[:-2].strip()
            if clean_text.startswith("/*"):
                clean_text = clean_text[2:].strip()
            comments.append(clean_text)
            prev = prev.prev_sibling

        if comments:
            docstring = "\n".join(reversed(comments))
        return docstring

    def _get_signature(self, node, source: bytes) -> str:
        body = node.child_by_field_name("body")
        if body:
            end_byte = body.start_byte
            sig = (
                source[node.start_byte : end_byte]
                .decode("utf-8", errors="replace")
                .strip()
            )
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
        defined_ids = set()

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
                signature=f"file {file_path.name}",
                docstring=self._get_docstring(root, source),
            )
        )
        defined_ids.add(file_node_id)

        scope_stack = [(file_node_id, "file")]

        def get_current_parent_id():
            return scope_stack[-1][0] if scope_stack else file_node_id

        def walk(node):
            nonlocal result

            if node.type == "ERROR" or (hasattr(node, "is_error") and node.is_error):
                logger.debug(f"Skipping syntax error node in C/C++ AST: {node}")
                return

            node_type = node.type
            pushed_scope = False

            if node_type in (
                "class_specifier",
                "struct_specifier",
                "union_specifier",
                "enum_specifier",
                "namespace_definition",
            ):
                if node_type != "namespace_definition":
                    body_node = node.child_by_field_name("body")
                    if not body_node:
                        for child in node.children:
                            walk(child)
                        return

                name_node = node.child_by_field_name("name")
                name = ""
                if name_node:
                    name = (
                        source[name_node.start_byte : name_node.end_byte]
                        .decode("utf-8", errors="replace")
                        .strip()
                    )

                if not name:
                    # Anonymous specifier
                    for child in node.children:
                        walk(child)
                    return

                parent_id = get_current_parent_id()
                if "::" in name:
                    symbol_id = f"{rel_path}::{name}"
                else:
                    parent_parts = parent_id.split("::", 1)
                    if len(parent_parts) > 1:
                        symbol_id = f"{rel_path}::{parent_parts[1]}.{name}"
                    else:
                        symbol_id = f"{rel_path}::{name}"

                sym_type = "class"
                if node_type == "struct_specifier":
                    sym_type = "struct"
                elif node_type == "union_specifier":
                    sym_type = "union"
                elif node_type == "enum_specifier":
                    sym_type = "enum"
                elif node_type == "namespace_definition":
                    sym_type = "namespace"

                result.nodes.append(
                    NodeSchema(
                        id=symbol_id,
                        label=name,
                        type=sym_type,
                        source_file=rel_path,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=self._get_signature(node, source),
                        docstring=self._get_docstring(node, source),
                    )
                )
                defined_ids.add(symbol_id)

                result.edges.append(
                    EdgeSchema(source=parent_id, target=symbol_id, relation="contains")
                )

                # Handle base classes inheritance
                for child in node.children:
                    if child.type == "base_class_clause":

                        def extract_base_types(n):
                            if n.type in (
                                "type_identifier",
                                "qualified_identifier",
                                "template_type",
                            ):
                                return (
                                    source[n.start_byte : n.end_byte]
                                    .decode("utf-8", errors="replace")
                                    .strip()
                                )
                            for c in n.children:
                                bt = extract_base_types(c)
                                if bt:
                                    return bt
                            return None

                        for sub in child.children:
                            base_name = extract_base_types(sub)
                            if base_name:
                                result.edges.append(
                                    EdgeSchema(
                                        source=symbol_id,
                                        target=base_name,
                                        relation="inherits",
                                    )
                                )

                scope_stack.append((symbol_id, sym_type))
                pushed_scope = True

            elif node_type == "function_definition":
                declarator = node.child_by_field_name("declarator")
                func_name = self._get_declarator_name(declarator, source)

                if func_name:
                    parent_id = get_current_parent_id()
                    parent_type = scope_stack[-1][1] if scope_stack else "file"

                    if "::" in func_name:
                        class_part, method_part = func_name.rsplit("::", 1)
                        class_id = f"{rel_path}::{class_part.replace('::', '.')}"
                        method_id = f"{class_id}.{method_part}"
                        sym_type = "method"
                        func_label = method_part

                        actual_parent = (
                            class_id if class_id in defined_ids else file_node_id
                        )
                        result.edges.append(
                            EdgeSchema(
                                source=actual_parent,
                                target=method_id,
                                relation="contains",
                            )
                        )
                    elif parent_type in ("class", "struct", "union", "namespace"):
                        method_id = f"{parent_id}.{func_name}"
                        sym_type = (
                            "method" if parent_type != "namespace" else "function"
                        )
                        func_label = func_name
                        result.edges.append(
                            EdgeSchema(
                                source=parent_id, target=method_id, relation="contains"
                            )
                        )
                    else:
                        method_id = f"{rel_path}::{func_name}"
                        sym_type = "function"
                        func_label = func_name
                        result.edges.append(
                            EdgeSchema(
                                source=parent_id, target=method_id, relation="contains"
                            )
                        )

                    result.nodes.append(
                        NodeSchema(
                            id=method_id,
                            label=func_label,
                            type=sym_type,
                            source_file=rel_path,
                            line_start=node.start_point[0] + 1,
                            line_end=node.end_point[0] + 1,
                            signature=self._get_signature(node, source),
                            docstring=self._get_docstring(node, source),
                        )
                    )
                    defined_ids.add(method_id)

                    scope_stack.append((method_id, sym_type))
                    pushed_scope = True

            elif node_type == "preproc_include":
                path_node = node.child_by_field_name("path")
                if not path_node:
                    for child in node.children:
                        if child.type in ("string_literal", "system_lib_string"):
                            path_node = child
                            break
                if path_node:
                    include_path = (
                        source[path_node.start_byte : path_node.end_byte]
                        .decode("utf-8", errors="replace")
                        .strip('"<>')
                    )
                    result.edges.append(
                        EdgeSchema(
                            source=file_node_id, target=include_path, relation="imports"
                        )
                    )

            elif node_type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node:
                    callee_name = (
                        source[func_node.start_byte : func_node.end_byte]
                        .decode("utf-8", errors="replace")
                        .strip()
                    )
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


class CParser(CCppParser):
    def __init__(self):
        import tree_sitter_c

        super().__init__(tree_sitter_c)


class CppParser(CCppParser):
    def __init__(self):
        import tree_sitter_cpp

        super().__init__(tree_sitter_cpp)
