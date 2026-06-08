import logging
from pathlib import Path
import tree_sitter
import tree_sitter_javascript
import tree_sitter_typescript
from codegraph.parser.base import BaseParser, ExtractionResult, NodeSchema, EdgeSchema

logger = logging.getLogger(__name__)

class JavaScriptParser(BaseParser):
    def __init__(self):
        # Cache parsers for javascript, typescript and tsx
        self.js_lang = tree_sitter.Language(tree_sitter_javascript.language())
        self.ts_lang = tree_sitter.Language(tree_sitter_typescript.language_typescript())
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
            comment_text = source[prev.start_byte:prev.end_byte].decode("utf-8", errors="replace")
            # Strip comment markers
            clean_text = comment_text.strip().lstrip("/*").rstrip("*/").lstrip("*").strip()
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
            # Trim trailing open curly brace
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
        result.nodes.append(NodeSchema(
            id=file_node_id,
            label=file_path.name,
            type="file",
            source_file=rel_path,
            line_start=1,
            line_end=len(source.splitlines()) or 1,
            signature=f"module {file_path.name}",
            docstring=self._get_docstring(root, source)
        ))

        scope_stack = [(file_node_id, "file")]

        def get_current_parent_id():
            return scope_stack[-1][0] if scope_stack else file_node_id

        def walk(node):
            nonlocal result
            
            node_type = node.type
            pushed_scope = False
            
            if node_type in ("class_declaration", "interface_declaration"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    class_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                    parent_id = get_current_parent_id()
                    
                    class_id = f"{rel_path}::{class_name}"
                    sym_type = "class" if node_type == "class_declaration" else "interface"
                    
                    result.nodes.append(NodeSchema(
                        id=class_id,
                        label=class_name,
                        type=sym_type,
                        source_file=rel_path,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        signature=self._get_signature(node, source),
                        docstring=self._get_docstring(node, source)
                    ))
                    
                    result.edges.append(EdgeSchema(
                        source=parent_id,
                        target=class_id,
                        relation="contains"
                    ))
                    
                    # Inheritance: heritage / extends clause
                    # Extends/implements logic can be added here if needed by walking children
                    for child in node.children:
                        if child.type in ("class_heritage", "interface_heritage"):
                            # extends Expression
                            for sub in child.children:
                                if sub.type in ("identifier", "nested_identifier"):
                                    parent_class_name = source[sub.start_byte:sub.end_byte].decode("utf-8", errors="replace")
                                    result.edges.append(EdgeSchema(
                                        source=class_id,
                                        target=parent_class_name,
                                        relation="inherits"
                                    ))

                    scope_stack.append((class_id, sym_type))
                    pushed_scope = True

            elif node_type in ("function_declaration", "method_definition"):
                name_node = node.child_by_field_name("name")
                if name_node:
                    func_name = source[name_node.start_byte:name_node.end_byte].decode("utf-8", errors="replace")
                    parent_id = get_current_parent_id()
                    parent_type = scope_stack[-1][1] if scope_stack else "file"
                    
                    if parent_type in ("class", "interface"):
                        func_id = f"{parent_id}.{func_name}"
                        sym_type = "method"
                    else:
                        func_id = f"{rel_path}::{func_name}"
                        sym_type = "function"
                        
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
                        relation="contains"
                    ))
                    
                    scope_stack.append((func_id, sym_type))
                    pushed_scope = True

            elif node_type == "import_statement":
                # import { x } from './y'
                # Find source file string
                source_node = node.child_by_field_name("source")
                if source_node:
                    import_path = source[source_node.start_byte:source_node.end_byte].decode("utf-8", errors="replace")
                    import_path = import_path.strip("\"'")
                    result.edges.append(EdgeSchema(
                        source=file_node_id,
                        target=import_path,
                        relation="imports"
                    ))

            elif node_type in ("call_expression", "new_expression"):
                func_node = node.child_by_field_name("function")
                if func_node:
                    callee_name = source[func_node.start_byte:func_node.end_byte].decode("utf-8", errors="replace")
                    caller_id = get_current_parent_id()
                    
                    result.edges.append(EdgeSchema(
                        source=caller_id,
                        target=callee_name,
                        relation="calls"
                    ))

            # Recurse children
            for child in node.children:
                walk(child)
                
            if pushed_scope:
                scope_stack.pop()

        walk(root)
        return result
