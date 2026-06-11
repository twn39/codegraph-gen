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


def test_common_builtin_methods_filtering():
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # Define a user class with append/resume/len/is_empty/new methods in user.py
        user = workspace / "user.py"
        user.write_text("""
class PacketBuffer:
    def append(self, x):
        pass
    def resume(self):
        pass
    def len(self):
        pass
    def is_empty(self):
        pass
    def new(self):
        pass
""")

        caller = workspace / "caller.py"
        caller.write_text("""
def run():
    cues = []
    cues.append(1)  # This is a builtin method call, should not connect to user.py::PacketBuffer.append
    task.resume()   # This is a common network task method, should not connect to user.py::PacketBuffer.resume
    cues.len()
    cues.is_empty()
    other.new()
""")

        py_parser = PythonParser()
        res_user = py_parser.parse_file(user, workspace)
        res_caller = py_parser.parse_file(caller, workspace)

        G = build_graph([res_user, res_caller], workspace)

        # The call to cues.append() should not connect to user.py::PacketBuffer.append
        assert not G.has_edge("caller.py::run", "user.py::PacketBuffer.append")
        # The call to task.resume() should not connect to user.py::PacketBuffer.resume
        assert not G.has_edge("caller.py::run", "user.py::PacketBuffer.resume")
        # The calls to len, is_empty, and new should not connect
        assert not G.has_edge("caller.py::run", "user.py::PacketBuffer.len")
        assert not G.has_edge("caller.py::run", "user.py::PacketBuffer.is_empty")
        assert not G.has_edge("caller.py::run", "user.py::PacketBuffer.new")


def test_swift_local_scope_type_binding():
    from codegraph.parser.swift import SwiftParser
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # 1. Define class AudioDecoderActor with decode method
        decoder_file = workspace / "AudioDecoderActor.swift"
        decoder_file.write_text("""
class AudioDecoderActor {
    func decode(_ data: Data) {}
}
""")

        # 2. Define class EdgeTTSPlayer with play method
        player_file = workspace / "EdgeTTSPlayer.swift"
        player_file.write_text("""
class EdgeTTSPlayer {
    func play() {}
}
""")

        # 3. Define Caller with parameters and local variable bindings
        caller_file = workspace / "Caller.swift"
        caller_file.write_text("""
class Caller {
    func run(with player: EdgeTTSPlayer) {
        let decoder = AudioDecoderActor()
        decoder.decode(Data())
        player.play()
    }
}
""")

        swift_parser = SwiftParser()
        res_decoder = swift_parser.parse_file(decoder_file, workspace)
        res_player = swift_parser.parse_file(player_file, workspace)
        res_caller = swift_parser.parse_file(caller_file, workspace)

        # Verify NodeSchema actually got local_bindings populated
        caller_run_node = next((n for n in res_caller.nodes if n.label == "run"), None)
        assert caller_run_node is not None
        assert caller_run_node.local_bindings == {
            "player": "EdgeTTSPlayer",
            "decoder": "AudioDecoderActor"
        }

        # Build graph and check edges
        G = build_graph([res_decoder, res_player, res_caller], workspace)

        # Verify correct exact connections were made
        assert G.has_edge("Caller.swift::Caller.run", "AudioDecoderActor.swift::AudioDecoderActor.decode")
        assert G.has_edge("Caller.swift::Caller.run", "EdgeTTSPlayer.swift::EdgeTTSPlayer.play")


def test_python_local_scope_type_binding():
    from codegraph.parser.python import PythonParser
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # 1. Define class HeifContext with read_from_file method
        context_file = workspace / "context.py"
        context_file.write_text("""
class HeifContext:
    def read_from_file(self, path):
        pass
""")

        # 2. Define class HeifImage with add_plane method
        image_file = workspace / "image.py"
        image_file.write_text("""
class HeifImage:
    def add_plane(self, channel, w, h, b):
        pass
""")

        # 3. Define Caller with parameters, with-statement and variable assignments
        caller_file = workspace / "caller.py"
        caller_file.write_text("""
def run(img_param: HeifImage):
    ctx = HeifContext()
    ctx.read_from_file("test.heic")
    img_param.add_plane(1, 100, 100, 8)
    
    with HeifContext() as ctx_mgr:
        ctx_mgr.read_from_file("mgr.heic")
""")

        py_parser = PythonParser()
        res_context = py_parser.parse_file(context_file, workspace)
        res_image = py_parser.parse_file(image_file, workspace)
        res_caller = py_parser.parse_file(caller_file, workspace)

        # Verify NodeSchema actually got local_bindings populated
        caller_run_node = next((n for n in res_caller.nodes if n.label == "run"), None)
        assert caller_run_node is not None
        assert caller_run_node.local_bindings == {
            "img_param": "HeifImage",
            "ctx": "HeifContext",
            "ctx_mgr": "HeifContext"
        }

        # Build graph and check edges
        G = build_graph([res_context, res_image, res_caller], workspace)

        # Verify correct exact connections were made
        assert G.has_edge("caller.py::run", "context.py::HeifContext.read_from_file")
        assert G.has_edge("caller.py::run", "image.py::HeifImage.add_plane")


def test_external_type_method_fallback_bypass():
    from codegraph.parser.rust import RustParser
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir).resolve()

        # 1. Define a user struct with a custom method (e.g. read)
        user_file = workspace / "user.rs"
        user_file.write_text("""
struct CustomBuffer;
impl CustomBuffer {
    fn read(&self) {}
}
""")

        # 2. Define a caller that calls read on an external type (e.g. standard BufReader)
        caller_file = workspace / "caller.rs"
        caller_file.write_text("""
fn run() {
    let reader: BufReader = get_reader();
    reader.read(); // BufReader is external, so reader.read() should not connect to CustomBuffer::read
}
""")

        rust_parser = RustParser()
        res_user = rust_parser.parse_file(user_file, workspace)
        res_caller = rust_parser.parse_file(caller_file, workspace)

        G = build_graph([res_user, res_caller], workspace)

        # Verify CustomBuffer::read was extracted
        assert "user.rs::CustomBuffer.read" in G.nodes

        # Verify the call to reader.read() did NOT resolve to CustomBuffer::read via global fallback
        assert not G.has_edge("caller.rs::run", "user.rs::CustomBuffer.read")



