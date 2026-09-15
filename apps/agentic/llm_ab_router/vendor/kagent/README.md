# Pinned protocol sources

Unmodified protocol definitions from kagent commit `e6df917e9fa8`:
https://github.com/kagent-dev/kagent/tree/e6df917e9fa8/proto

`LICENSE` is copied from the same commit. `compile_protocol.py` generates Python
bindings during the image build. The application uses only GetSession/ListTasks
to retrieve child evidence with the root invocation's authenticated identity.
The generated API includes other methods, but the collector never invokes them.
