from codegraph_gen.parser.ocaml import OCamlParser


def test_ocaml_parsing(tmp_path):
    code = """
(* This is a module docstring *)
open List
include String

module MyMod = struct
  (* Helper function docstring *)
  let helper a = a + 1
end

type config = {
  version : string;
}

let main_func cfg =
  let local_val = MyMod.helper 42 in
  local_val
"""
    file_path = tmp_path / "test.ml"
    file_path.write_text(code, encoding="utf-8")

    parser = OCamlParser()
    result = parser.parse_file(file_path, tmp_path)

    # Verify nodes
    nodes = {node.id: node for node in result.nodes}
    assert "test.ml" in nodes
    assert nodes["test.ml"].type == "file"
    assert "This is a module docstring" in nodes["test.ml"].docstring

    assert "test.ml::MyMod" in nodes
    assert nodes["test.ml::MyMod"].type == "class"
    assert nodes["test.ml::MyMod"].signature == "module MyMod"

    assert "test.ml::MyMod.helper" in nodes
    assert nodes["test.ml::MyMod.helper"].type == "function"
    assert nodes["test.ml::MyMod.helper"].signature == "let helper a = a + 1"
    assert "Helper function docstring" in nodes["test.ml::MyMod.helper"].docstring

    assert "test.ml::config" in nodes
    assert nodes["test.ml::config"].type == "struct"
    assert nodes["test.ml::config"].signature == "type config"

    assert "test.ml::main_func" in nodes
    assert nodes["test.ml::main_func"].type == "function"
    assert nodes["test.ml::main_func"].signature == "let main_func cfg"
    assert nodes["test.ml::main_func"].local_bindings == {"local_val": "MyMod.helper"}

    # Verify edges
    contains_edges = [
        (edge.source, edge.target)
        for edge in result.edges
        if edge.relation == "contains"
    ]
    assert ("test.ml", "test.ml::MyMod") in contains_edges
    assert ("test.ml::MyMod", "test.ml::MyMod.helper") in contains_edges
    assert ("test.ml", "test.ml::config") in contains_edges
    assert ("test.ml", "test.ml::main_func") in contains_edges

    import_edges = [
        (edge.source, edge.target, edge.import_map)
        for edge in result.edges
        if edge.relation == "imports"
    ]
    assert ("test.ml", "List", {"*": "*"}) in import_edges
    assert ("test.ml", "String", {"*": "*"}) in import_edges

    call_edges = [
        (edge.source, edge.target) for edge in result.edges if edge.relation == "calls"
    ]
    # main_func calls MyMod.helper
    assert ("test.ml::main_func", "MyMod.helper") in call_edges
