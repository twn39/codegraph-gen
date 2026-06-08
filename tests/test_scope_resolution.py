import tempfile
from pathlib import Path

from codegraph.parser.python import PythonParser
from codegraph.parser.go import GoParser
from codegraph.builder import build_graph


def test_name_isolation_and_aliases():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # module_a.py defines foo
        module_a = workspace / "module_a.py"
        module_a.write_text("def foo():\n    pass")

        # module_b.py defines foo
        module_b = workspace / "module_b.py"
        module_b.write_text("def foo():\n    pass")

        # caller.py imports foo from module_a and alias_foo from module_b
        caller = workspace / "caller.py"
        caller.write_text("""
from module_a import foo
from module_b import foo as alias_foo

def run():
    foo()
    alias_foo()
""")

        py_parser = PythonParser()
        res_a = py_parser.parse_file(module_a, workspace)
        res_b = py_parser.parse_file(module_b, workspace)
        res_caller = py_parser.parse_file(caller, workspace)

        G = build_graph([res_a, res_b, res_caller], workspace)

        # Verify foo calls resolved correctly
        assert G.has_edge("caller.py::run", "module_a.py::foo")

        # Verify alias_foo resolved to module_b's foo
        assert G.has_edge("caller.py::run", "module_b.py::foo")


def test_wildcard_imports():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        helper = workspace / "helper.py"
        helper.write_text("def helper_func():\n    pass")

        caller = workspace / "caller.py"
        caller.write_text("""
from helper import *

def run():
    helper_func()
""")

        py_parser = PythonParser()
        res_helper = py_parser.parse_file(helper, workspace)
        res_caller = py_parser.parse_file(caller, workspace)

        G = build_graph([res_helper, res_caller], workspace)

        assert G.has_edge("caller.py::run", "helper.py::helper_func")


def test_builtin_filtering():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # Define a user class/function called "len" elsewhere in user.py
        user = workspace / "user.py"
        user.write_text("def len(x):\n    return 42")

        caller = workspace / "caller.py"
        caller.write_text("""
def run():
    x = [1, 2]
    len(x)  # This is a builtin, should not connect to user.py::len
""")

        py_parser = PythonParser()
        res_user = py_parser.parse_file(user, workspace)
        res_caller = py_parser.parse_file(caller, workspace)

        G = build_graph([res_user, res_caller], workspace)

        # The call to len() should not connect to user.py::len
        assert not G.has_edge("caller.py::run", "user.py::len")


def test_go_package_scope():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # Sibling Go files in mypackage/
        mypackage_dir = workspace / "mypackage"
        mypackage_dir.mkdir()

        file_a = mypackage_dir / "file_a.go"
        file_a.write_text("""
package mypackage
func Caller() {
    MyFunc()
}
""")

        file_b = mypackage_dir / "file_b.go"
        file_b.write_text("""
package mypackage
func MyFunc() {}
""")

        # Other Go package and file with the same function name
        otherpackage_dir = workspace / "other"
        otherpackage_dir.mkdir()
        file_c = otherpackage_dir / "file_c.go"
        file_c.write_text("""
package other
func MyFunc() {}
""")

        go_parser = GoParser()
        res_a = go_parser.parse_file(file_a, workspace)
        res_b = go_parser.parse_file(file_b, workspace)
        res_c = go_parser.parse_file(file_c, workspace)

        G = build_graph([res_a, res_b, res_c], workspace)

        # Caller in file_a should connect to MyFunc in file_b (same directory/package)
        assert G.has_edge("mypackage/file_a.go::Caller", "mypackage/file_b.go::MyFunc")

        # Caller in file_a should NOT connect to MyFunc in file_c (other package)
        assert not G.has_edge("mypackage/file_a.go::Caller", "other/file_c.go::MyFunc")
