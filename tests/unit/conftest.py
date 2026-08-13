"""tests/unit's pytest conftest.

Just wires in the `container` fixture; the actual harness lives in
conftest_container.py (see that module's docstring) so it stays a plain,
importable, testable module rather than growing inside a conftest.
"""

from conftest_container import container  # noqa: F401  (re-exported fixture)
