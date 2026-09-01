#!/usr/bin/env bash
# Render the C4 figures from their source model.
#
#     scripts/dev/render_c4.sh
#
# docs/c4/ is the source of truth; docs/diagrams/agnes-c4-*.svg is its output.
# Edit the DSL, run this, commit both. The DSL is what a reviewer, a validator
# and an LLM read; the SVG is only what a human looks at.
#
# The pipeline is DSL -> PlantUML -> SVG, with the Structurizr parser as the
# gate: it rejects dangling references, duplicate relationships, and views
# scoped to elements that do not exist. Both steps run in containers so the
# repo needs no Java toolchain.
#
# A view's key IS its filename: the view `agnes-c4-context` lands at
# docs/diagrams/agnes-c4-context.svg.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
C4_DIR="$REPO_ROOT/docs/c4"
OUT_DIR="$REPO_ROOT/docs/diagrams"
WORK="$C4_DIR/.render"

STRUCTURIZR_IMAGE="docker.io/structurizr/structurizr:latest"
PLANTUML_IMAGE="docker.io/plantuml/plantuml:latest"

if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    RUNTIME=docker
elif command -v podman >/dev/null 2>&1; then
    RUNTIME=podman
else
    echo "error: needs docker or podman (the Structurizr parser and PlantUML run in containers)" >&2
    exit 1
fi

rm -rf "$WORK"
mkdir -p "$WORK"

echo "==> parsing docs/c4/workspace.dsl"
"$RUNTIME" run --rm -v "$C4_DIR:/usr/local/structurizr:z" "$STRUCTURIZR_IMAGE" \
    export -workspace /usr/local/structurizr/workspace.dsl \
           -format plantuml/structurizr -output /usr/local/structurizr/.render

# Structurizr emits a "<view>-key" legend diagram beside each view. The legend
# repeats the styles block, which the model already documents in prose; keeping
# it would double the figure count for no reader.
rm -f "$WORK"/*-key.puml

echo "==> rendering SVG"
"$RUNTIME" run --rm -v "$WORK:/data:z" "$PLANTUML_IMAGE" -tsvg -o /data '/data/*.puml'

shopt -s nullglob
rendered=0
for svg in "$WORK"/structurizr-*.svg; do
    name="$(basename "$svg")"
    name="${name#structurizr-}"
    mv "$svg" "$OUT_DIR/$name"
    echo "    docs/diagrams/$name"
    rendered=$((rendered + 1))
done

if [ "$rendered" -eq 0 ]; then
    echo "error: the parser produced no diagrams — see the output above" >&2
    exit 1
fi

rm -rf "$WORK"
echo "==> $rendered figures rendered from docs/c4/"
