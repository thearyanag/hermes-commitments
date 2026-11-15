"""Load the root plugin under its manifest name, independent of checkout name.

Hermes supplies a package namespace at runtime. Tests reproduce that import
context without requiring the repository directory to be named ``commitments``.
This helper never registers the plugin or starts its worker.
"""

import importlib.util
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "commitments", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
