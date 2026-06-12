import pytest
from codegraph_gen.parser.base import ScopeTracker


def test_scope_tracker_basic():
    scope = ScopeTracker("main.py", "file")
    assert scope.current_id == "main.py"
    assert scope.current_type == "file"
    assert len(scope.stack) == 1

    scope.push("MyClass", "class")
    assert scope.current_id == "MyClass"
    assert scope.current_type == "class"
    assert len(scope.stack) == 2

    popped = scope.pop()
    assert popped == ("MyClass", "class")
    assert scope.current_id == "main.py"
    assert scope.current_type == "file"

    # Root scope cannot be popped
    with pytest.raises(IndexError):
        scope.pop()


def test_scope_tracker_context_manager():
    scope = ScopeTracker("main.py", "file")

    with scope.push("MyClass", "class"):
        assert scope.current_id == "MyClass"
        assert scope.current_type == "class"
        assert len(scope.stack) == 2

        with scope.push("method", "method"):
            assert scope.current_id == "method"
            assert scope.current_type == "method"
            assert len(scope.stack) == 3

        assert scope.current_id == "MyClass"
        assert scope.current_type == "class"

    assert scope.current_id == "main.py"
    assert scope.current_type == "file"
    assert len(scope.stack) == 1


def test_scope_tracker_exception_safety():
    scope = ScopeTracker("main.py", "file")

    try:
        with scope.push("MyClass", "class"):
            assert scope.current_id == "MyClass"
            raise ValueError("Something went wrong during visiting")
    except ValueError:
        pass

    # Verify that the stack was correctly popped despite the exception
    assert scope.current_id == "main.py"
    assert scope.current_type == "file"
    assert len(scope.stack) == 1


def test_scope_tracker_find_parent():
    scope = ScopeTracker("main.py", "file")
    scope.push("MyNamespace", "namespace")
    scope.push("MyClass", "class")
    scope.push("method", "method")

    assert scope.find_parent_by_type("class") == "MyClass"
    assert scope.find_parent_by_type("namespace") == "MyNamespace"
    assert scope.find_parent_by_type("file") == "main.py"
    assert scope.find_parent_by_type("unknown") is None
