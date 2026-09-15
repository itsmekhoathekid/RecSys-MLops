"""Compile vendored kagent e6df917e9fa8 schemas at image build time."""
from pathlib import Path
import importlib.util
from grpc_tools import protoc
import grpc_tools


def main():
    root = Path(__file__).resolve().parent
    source = root / "vendor/kagent/proto"
    output = root / "_protocol"
    output.mkdir(exist_ok=True)
    google = Path(importlib.util.find_spec("google.api").submodule_search_locations[0]).parents[1]
    args = ["protoc", "-I" + str(source), "-I" + str(Path(grpc_tools.__file__).parent / "_proto"),
            "-I" + str(google), "--python_out=" + str(output), "--grpc_python_out=" + str(output)]
    args += [str(source / p) for p in ("a2a.proto", "kagent/api/v1alpha1/common.proto", "kagent/api/v1alpha1/sessions.proto")]
    if protoc.main(args):
        raise RuntimeError("kagent schema compilation failed")


if __name__ == "__main__":
    main()
