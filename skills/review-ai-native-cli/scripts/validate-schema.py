"""validate-schema.py <schemas-dir> <schema> <capture.json> [dotted.path]

Validates a captured envelope, or the part of it at a dotted path, against the
target tool's own vendored JSON Schemas (contract/schemas, draft 2020-12), for
the semantic rules check-envelope.js does not cover (VER-002). Examples:

    python -I validate-schema.py contract/schemas envelope  out/ctx.json
    python -I validate-schema.py contract/schemas context   out/ctx.json data
    python -I validate-schema.py contract/schemas preview   out/dry.json data.preview
    python -I validate-schema.py contract/schemas receipt   out/confirm.json data.receipt

<schema> is the name inside the schema's $id (urn:ai-native-cli-spec:<schema>:2.0).
Needs: python -m pip install "jsonschema>=4.18" referencing
"""

import json
import pathlib
import sys

try:
    from jsonschema import Draft202012Validator
    from referencing import Registry, Resource
except ImportError:
    sys.exit('validate-schema.py needs: python -m pip install "jsonschema>=4.18" referencing')

if len(sys.argv) < 4:
    sys.exit(__doc__)
docs = [json.loads(p.read_text("utf-8")) for p in pathlib.Path(sys.argv[1]).glob("*.schema.json")]
registry = Registry().with_resources((s["$id"], Resource.from_contents(s)) for s in docs)
matches = [s for s in docs if s["$id"].split(":")[2] == sys.argv[2]]
if not matches:
    sys.exit(f"no schema named {sys.argv[2]} in {sys.argv[1]}")
value = json.loads(pathlib.Path(sys.argv[3]).read_text("utf-8"))
for key in sys.argv[4].split(".") if len(sys.argv) > 4 else []:
    value = value[key]
errors = sorted(Draft202012Validator(matches[0], registry=registry).iter_errors(value), key=str)
for e in errors:
    print("  -", "/".join(map(str, e.absolute_path)) or "(root)", e.message[:200])
print("PASS" if not errors else "FAIL", sys.argv[2], sys.argv[3])
sys.exit(1 if errors else 0)
