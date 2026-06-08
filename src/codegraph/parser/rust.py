import logging
from pathlib import Path
import tree_sitter
import tree_sitter_rust
from codegraph.parser.base import BaseParser, ExtractionResult, NodeSchema, EdgeSchema

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
            comment_text = source[prev.start_byte:prev.end_byte].decode("utf-8", errors="replace")
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
            sig_bytes = source[node.start_byte:end_byte]
            sig = sig_bytes.decode("utf-8", errors="replace").strip()
            if sig.endswith("{"):
                sig = sig[:-1].strip()
            return sig
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace").split("\n")[0]

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
        result.nodes.append(NodeSchema(
            id=file_node_id,
            label=file_path.name,
            type="file",
            source_file=rel_path,
            line_start=1,
            line_end=len(source.splitlines()) or 1,
            signature=f"mod {file_path.stem}",
            docstring=self._get_docstring(root, source)
        ))

        def get_impl_type(impl_node) -> str | None:
            type_node = impl_node.child_by_field_name("type")
            if type_node:
                raw_type = source[type_node.start_byte:type_node.end_byte].decode("utf-8", errors="replace")
                return raw_type.strip()
            return None

        def walk(node, current_impl_type=None):
            nonlocal result
            node_type = node.type
            pushed_impl = None

            if node_type in ("struct_item", "enum_item", "trait_item"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    item_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                    item_id = f"{rel_path}::{item_name}"
                    
                    sym_type = "struct"
                    if node_type == "enum_item":
                        sym_type = "enum"
                    elif node_type == "trait_item":
                        sym_type = "interface" # map trait to interface for consistency
                        
                    result.nodes.append(NodeSchema(
                        id=item_id,
                        label=item_name,
                        type=sym_type,
                        source_file=rel_path,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=self._get_signature(node, source),
                        docstring=self._get_docstring(node, source)
                    ))
                    
                    result.edges.append(EdgeSchema(
                        source=file_node_id,
                        target=item_id,
                        relation="contains"
                    ))

            elif node_type == "impl_item":
                impl_type = get_impl_type(node)
                if impl_type:
                    pushed_impl = impl_type
                    
                    # Ensure struct node is created if it hasn't been yet (impls can define methods for external/internal types)
                    type_id = f"{rel_path}::{impl_type}"
                    
                    # We might also link impl to trait if it's trait implementation
                    trait_node = node.child_by_field_name("trait")
                    if trait_node:
                        trait_name = source[trait_node.start_byte:trait_node.end_byte].decode("utf-8", errors="replace")
                        result.edges.append(EdgeSchema(
                            source=type_id,
                            target=trait_name,
                            relation="implements"
                        ))

            elif node_type == "function_item":
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                    
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
                        
                    result.nodes.append(NodeSchema(
                        id=func_id,
                        label=func_name,
                        type=sym_type,
                        source_file=rel_path,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=self._get_signature(node, source),
                        docstring=self._get_docstring(node, source)
                    ))
                    
                    result.edges.append(EdgeSchema(
                        source=parent_id,
                        target=func_id,
                        relation=relation
                    ))

            elif node_type == "use_declaration":
                # use std::collections::HashMap;
                # extract path
                for child in node.children:
                    if child.type in ("use_path", "use_list", "identifier", "scoped_identifier"):
                        use_path = source[child.start_byte:child.end_byte].decode("utf-8", errors="replace")
                        result.edges.append(EdgeSchema(
                            source=file_node_id,
                            target=use_path,
                            relation="imports"
                        ))

            elif node_type == "call_expression":
                func_node = node.child_by_field_name("function")
                if func_node:
                    callee_name = source[func_node.start_byte:func_node.end_byte].decode("utf-8", errors="replace")
                    
                    # Find enclosing caller function/method ID
                    caller_id = file_node_id
                    curr = node.parent
                    while curr:
                        if curr.type == "function_item":
                            c_name_node = curr.child_by_field_name("name")
                            if c_name_node:
                                c_name = source[c_name_node.start_byte:c_name_node.end_byte].decode("utf-8", errors="replace")
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
                        
                    result.edges.append(EdgeSchema(
                        source=caller_id,
                        target=callee_name,
                        relation="calls"
                    ))

            # Recurse children
            impl_context = pushed_impl if pushed_impl else current_impl_type
            for child in node.children:
                walk(child, impl_context)

        walk(root)
        return result
