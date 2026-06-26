import networkx as nx
from codegraph_gen.schema import ExtractionResult, NodeSchema, EdgeSchema
from codegraph_gen.resolver import TypeResolver, extract_return_type_from_signature
from codegraph_gen.resolver_strategy import LanguageResolverStrategy


def make_node(
    id: str,
    type: str,
    label: str = "",
    source_file: str = "main.py",
    line_start: int = 1,
    line_end: int = 1,
    signature: str = "",
    **kwargs,
) -> NodeSchema:
    return NodeSchema(
        id=id,
        label=label or id.split("::")[-1],
        type=type,
        source_file=source_file,
        line_start=line_start,
        line_end=line_end,
        signature=signature,
        **kwargs,
    )


def test_signature_extraction():
    # Python
    assert (
        extract_return_type_from_signature("def foo() -> MyClass", "python")
        == "MyClass"
    )
    # "-> dict[str, Any]" matches "dict" since "[" is not in the allowed character set [\w::.<>]+
    assert (
        extract_return_type_from_signature("def foo() -> dict[str, Any]", "python")
        == "dict"
    )
    assert (
        extract_return_type_from_signature("def foo() -> List<MyClass>", "python")
        == "MyClass"
    )

    # Kotlin
    assert (
        extract_return_type_from_signature("fun foo(): MyClass", "kotlin") == "MyClass"
    )
    assert (
        extract_return_type_from_signature("fun foo(): List<MyClass>", "kotlin")
        == "MyClass"
    )

    # Go
    assert extract_return_type_from_signature("func foo() MyStruct", "go") == "MyStruct"
    assert (
        extract_return_type_from_signature("func foo() *MyStruct", "go") == "MyStruct"
    )
    assert (
        extract_return_type_from_signature("func foo() (MyStruct, error)", "go") is None
    )  # Original parser behavior
    assert (
        extract_return_type_from_signature("func foo() int", "go") is None
    )  # Builtin types ignored

    # C++
    assert extract_return_type_from_signature("MyClass foo()", "cpp") == "MyClass"
    assert (
        extract_return_type_from_signature("static inline MyClass* foo()", "cpp")
        == "MyClass"
    )
    assert (
        extract_return_type_from_signature("virtual const MyClass& foo()", "cpp")
        == "MyClass"
    )


def test_type_resolver_initialization(tmp_path):
    G = nx.DiGraph()
    G.add_node("main.py", type="file")
    G.add_node("main.py::MyClass", type="class", label="MyClass", source_file="main.py")
    G.add_node(
        "main.py::foo",
        type="function",
        label="foo",
        source_file="main.py",
        signature="def foo() -> MyClass",
    )

    ext = ExtractionResult(
        nodes=[
            make_node(id="main.py", type="file", label="main.py"),
            make_node(
                id="main.py::MyClass",
                type="class",
                label="MyClass",
                source_file="main.py",
            ),
            make_node(
                id="main.py::foo",
                type="function",
                label="foo",
                source_file="main.py",
                signature="def foo() -> MyClass",
            ),
        ],
        edges=[],
    )

    resolver = TypeResolver(G, [ext], tmp_path)

    assert resolver.file_languages["main.py"] == "python"
    assert "main.py" in resolver.scopes
    assert resolver.scopes["main.py"].declared_symbols["MyClass"] == "main.py::MyClass"
    assert resolver.global_symbol_map["MyClass"] == ["main.py::MyClass"]
    assert resolver.return_types["main.py::foo"] == "MyClass"


def test_type_resolver_lookup_strategies(tmp_path):
    G = nx.DiGraph()
    G.add_node("main.py", type="file")
    G.add_node("main.py::MyClass", type="class", label="MyClass", source_file="main.py")
    G.add_node(
        "main.py::MyClass.method",
        type="method",
        label="method",
        source_file="main.py",
        local_bindings={"self": "MyClass", "x": "OtherClass"},
    )
    G.add_node(
        "main.py::OtherClass", type="class", label="OtherClass", source_file="main.py"
    )
    G.add_node(
        "main.py::OtherClass.bar", type="method", label="bar", source_file="main.py"
    )

    ext = ExtractionResult(
        nodes=[
            make_node(id="main.py", type="file", label="main.py"),
            make_node(
                id="main.py::MyClass",
                type="class",
                label="MyClass",
                source_file="main.py",
            ),
            make_node(
                id="main.py::MyClass.method",
                type="method",
                label="method",
                source_file="main.py",
                local_bindings={"self": "MyClass", "x": "OtherClass"},
            ),
            make_node(
                id="main.py::OtherClass",
                type="class",
                label="OtherClass",
                source_file="main.py",
            ),
            make_node(
                id="main.py::OtherClass.bar",
                type="method",
                label="bar",
                source_file="main.py",
            ),
        ],
        edges=[],
    )

    resolver = TypeResolver(G, [ext], tmp_path)

    # 1. Builtin check
    from codegraph_gen.resolver_strategy import get_strategy_by_name

    assert get_strategy_by_name("python").is_builtin("print") is True
    assert get_strategy_by_name("python").is_builtin("MyClass") is False

    # 2. Local binding resolution
    assert (
        resolver.resolve_symbol("main.py::MyClass.method", "x.bar")
        == "main.py::OtherClass.bar"
    )

    # 3. Self reference
    assert (
        resolver.resolve_symbol("main.py::MyClass.method", "self.method")
        == "main.py::MyClass.method"
    )

    # 4. Context fallback
    assert (
        resolver.resolve_symbol("main.py::MyClass.method", "OtherClass")
        == "main.py::OtherClass"
    )


def test_fixpoint_type_propagation(tmp_path):
    G = nx.DiGraph()
    # file setup
    G.add_node("a.py", type="file")
    G.add_node("b.py", type="file")

    # a.py functions
    G.add_node(
        "a.py::get_helper",
        type="function",
        label="get_helper",
        source_file="a.py",
        signature="def get_helper() -> HelperClass",
    )
    G.add_node(
        "a.py::HelperClass", type="class", label="HelperClass", source_file="a.py"
    )
    G.add_node(
        "a.py::HelperClass.do_work", type="method", label="do_work", source_file="a.py"
    )

    # b.py function with chain:
    # x = get_helper() (local variable x has bound_name "get_helper")
    # x.do_work()
    G.add_node(
        "b.py::client",
        type="function",
        label="client",
        source_file="b.py",
        local_bindings={"x": "get_helper"},
    )

    ext_a = ExtractionResult(
        nodes=[
            make_node(id="a.py", type="file", label="a.py"),
            make_node(
                id="a.py::get_helper",
                type="function",
                label="get_helper",
                source_file="a.py",
                signature="def get_helper() -> HelperClass",
            ),
            make_node(
                id="a.py::HelperClass",
                type="class",
                label="HelperClass",
                source_file="a.py",
            ),
            make_node(
                id="a.py::HelperClass.do_work",
                type="method",
                label="do_work",
                source_file="a.py",
            ),
        ],
        edges=[],
    )
    ext_b = ExtractionResult(
        nodes=[
            make_node(id="b.py", type="file", label="b.py"),
            make_node(
                id="b.py::client",
                type="function",
                label="client",
                source_file="b.py",
                local_bindings={"x": "get_helper"},
            ),
        ],
        edges=[EdgeSchema(source="b.py::client", target="x.do_work", relation="calls")],
    )

    resolver = TypeResolver(G, [ext_a, ext_b], tmp_path)

    # Initially, local binding for 'x' in 'b.py::client' is 'get_helper' (unresolved function type)
    assert G.nodes["b.py::client"]["local_bindings"]["x"] == "get_helper"

    # Propagating types should resolve 'x' to 'a.py::HelperClass'
    resolver.propagate_types()
    assert G.nodes["b.py::client"]["local_bindings"]["x"] == "a.py::HelperClass"

    # Resolving edges should resolve the call edge from 'b.py::client' to 'a.py::HelperClass.do_work'
    resolver.resolve_all_edges()
    assert G.has_edge("b.py::client", "a.py::HelperClass.do_work")


def test_language_strategies():
    from codegraph_gen.resolver_strategy import (
        get_strategy_for_file,
        get_strategy_by_name,
    )

    # test strategy lookup by file extension
    strategy_py = get_strategy_for_file("foo.py")
    assert strategy_py.name == "python"

    strategy_go = get_strategy_for_file("main.go")
    assert strategy_go.name == "go"

    strategy_rs = get_strategy_for_file("lib.rs")
    assert strategy_rs.name == "rust"

    strategy_ml = get_strategy_for_file("main.ml")
    assert strategy_ml.name == "ocaml"

    # test lookup by name
    assert get_strategy_by_name("Swift").name == "swift"
    assert get_strategy_by_name("cpp").name == "cpp"
    assert get_strategy_by_name("ocaml").name == "ocaml"

    # test builtins
    assert strategy_py.is_builtin("print") is True
    assert strategy_py.is_builtin("nonexistent") is False
    assert strategy_go.is_builtin("panic") is True

    # test package sibling scope (Go and Swift have it, Python doesn't)
    assert strategy_go.has_package_sibling_scope() is True
    assert get_strategy_for_file("foo.swift").has_package_sibling_scope() is True
    assert strategy_py.has_package_sibling_scope() is False

    # test import path candidates
    assert strategy_py.get_import_path_candidates("foo.bar") == [
        "foo/bar",
        "foo/bar.py",
        "foo/bar/__init__.py",
    ]
    assert strategy_rs.get_import_path_candidates("foo::bar") == [
        "foo/bar",
        "foo/bar.rs",
        "foo/bar/mod.rs",
    ]

    # test path target detection (C/C++ strategy handles .h/.hpp ends as path targets)
    strategy_cpp = get_strategy_by_name("cpp")
    assert strategy_py.is_path_target("myheader.h") is False
    assert strategy_cpp.is_path_target("myheader.h") is True


class DummyCustomStrategy(LanguageResolverStrategy):
    name = "dummy"
    file_extensions = {".dummy"}
    import_search_suffixes = [".dummy"]

    def compute_transfer_type(
        self, resolved_target_type: str, resolved_target_id: str
    ) -> str | None:
        if resolved_target_type == "special_type":
            return "special_resolved_type"
        return None

    def extend_resolver_chain(self, default_chain: list) -> list:
        def custom_step(ctx):
            if ctx.callee_name == "magic_word":
                return "magic_resolved_id"
            return None

        return default_chain + [custom_step]


def test_worklist_solver_incremental_and_cyclic(tmp_path):
    import networkx as nx

    # Test incremental update cascading:
    # We have a chain of local bindings in 'main.py::foo':
    # x -> y (depends on y)
    # y -> z (depends on z)
    # z -> 'main.py::SomeClass' (directly resolvable to class)
    #
    # Also, we have a cycle of local bindings in 'main.py::bar':
    # a -> b
    # b -> a
    G = nx.DiGraph()
    G.add_node("main.py", type="file")
    G.add_node(
        "main.py::SomeClass", type="class", label="SomeClass", source_file="main.py"
    )
    G.add_node(
        "main.py::foo",
        type="function",
        label="foo",
        source_file="main.py",
        local_bindings={"x": "y", "y": "z", "z": "SomeClass"},
    )
    G.add_node(
        "main.py::bar",
        type="function",
        label="bar",
        source_file="main.py",
        local_bindings={"a": "b", "b": "a"},
    )

    ext = ExtractionResult(
        nodes=[
            make_node(id="main.py", type="file", label="main.py"),
            make_node(
                id="main.py::SomeClass",
                type="class",
                label="SomeClass",
                source_file="main.py",
            ),
            make_node(
                id="main.py::foo",
                type="function",
                label="foo",
                source_file="main.py",
                local_bindings={"x": "y", "y": "z", "z": "SomeClass"},
            ),
            make_node(
                id="main.py::bar",
                type="function",
                label="bar",
                source_file="main.py",
                local_bindings={"a": "b", "b": "a"},
            ),
        ],
        edges=[],
    )

    resolver = TypeResolver(G, [ext], tmp_path)
    resolver.propagate_types()

    # The chain x -> y -> z -> SomeClass should fully resolve to 'main.py::SomeClass'
    assert G.nodes["main.py::foo"]["local_bindings"]["z"] == "main.py::SomeClass"
    assert G.nodes["main.py::foo"]["local_bindings"]["y"] == "main.py::SomeClass"
    assert G.nodes["main.py::foo"]["local_bindings"]["x"] == "main.py::SomeClass"

    # The cycle a -> b -> a should terminate gracefully and remain unmodified
    assert G.nodes["main.py::bar"]["local_bindings"]["a"] == "b"
    assert G.nodes["main.py::bar"]["local_bindings"]["b"] == "a"


def test_custom_strategy_hooks(tmp_path):
    import networkx as nx

    dummy_strategy = DummyCustomStrategy()

    G = nx.DiGraph()
    G.add_node("test.dummy", type="file")
    G.add_node(
        "test.dummy::SpecialNode",
        type="special_type",
        label="SpecialNode",
        source_file="test.dummy",
    )
    G.add_node(
        "test.dummy::TargetNode",
        type="class",
        label="special_resolved_type",
        source_file="test.dummy",
    )
    G.add_node(
        "test.dummy::client",
        type="function",
        label="client",
        source_file="test.dummy",
        local_bindings={"v": "SpecialNode"},
    )

    ext = ExtractionResult(
        nodes=[
            make_node(id="test.dummy", type="file", label="test.dummy"),
            make_node(
                id="test.dummy::SpecialNode",
                type="special_type",
                label="SpecialNode",
                source_file="test.dummy",
            ),
            make_node(
                id="test.dummy::TargetNode",
                type="class",
                label="special_resolved_type",
                source_file="test.dummy",
            ),
            make_node(
                id="test.dummy::client",
                type="function",
                label="client",
                source_file="test.dummy",
                local_bindings={"v": "SpecialNode"},
            ),
        ],
        edges=[],
    )

    resolver = TypeResolver(G, [ext], tmp_path)
    # Manually associate our dummy file with our custom dummy strategy
    resolver.file_strategies["test.dummy"] = dummy_strategy

    # 1. Verify custom resolver step chain extension
    resolved_magic = resolver.resolve_symbol("test.dummy::client", "magic_word")
    assert resolved_magic == "magic_resolved_id"

    # 2. Verify custom transfer function hook in type propagation
    resolver.propagate_types()
    assert (
        G.nodes["test.dummy::client"]["local_bindings"]["v"] == "test.dummy::TargetNode"
    )
