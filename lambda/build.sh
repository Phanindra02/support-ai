#!/bin/bash
# Makes the Lambda's zip: extract_text.py and pypdf, at the zip's top level, where Lambda's
# python3.14 runtime looks for them. boto3 is left out, because the runtime has it.
#   lambda/build.sh            # writes build/lambda.zip
#   lambda/build.sh OUT_DIR    # writes OUT_DIR/lambda.zip
#
# The versions come from uv.lock's lambda group, so the zip has what the tests ran against.
set -euo pipefail

cd "$(dirname "$0")/.."
out="$(mkdir -p "${1:-build}" && cd "${1:-build}" && pwd)"
files="$out/lambda"

rm -rf "$files" "$out/lambda.zip"
mkdir -p "$files"

uv export --locked --quiet --only-group lambda --no-hashes --no-header --no-emit-project \
  --output-file "$out/lambda-requirements.txt"
# Wheels for Lambda's Linux on ARM (Graviton) and Python 3.14, whatever machine builds it.
# pypdf is pure Python, but a dependency with compiled code would need the right wheel.
uv pip install --quiet --requirements "$out/lambda-requirements.txt" --target "$files" \
  --python-version 3.14 --python-platform aarch64-manylinux_2_28 --only-binary :all:
cp lambda/extract_text.py "$files/"

# .lock is uv's own marker for the folder it installed into.
(cd "$files" && zip --quiet --recurse-paths "$out/lambda.zip" . --exclude ".lock" "*/__pycache__/*")
echo "$out/lambda.zip"
