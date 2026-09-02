# C4 model — source for the `agnes-c4-*.svg` figures

A [C4](https://c4model.com/) model of Agnes in
[Structurizr DSL](https://structurizr.com/dsl). **This folder is the source of
truth**; [`docs/diagrams/agnes-c4-*.svg`](../diagrams/) is its rendered output,
embedded from [`ARCHITECTURE.md`](../../ARCHITECTURE.md).

Same format as the Keboola platform's own C4 model, so both are queryable and
diffable the same way.

## Regenerating

```bash
scripts/dev/render_c4.sh
```

DSL → PlantUML → SVG, with the real Structurizr parser as the gate. Both steps
run in containers, so the repo needs no Java toolchain. Edit the DSL, rerun,
commit both.

`scripts/validate_c4.py` is a fast structural lint (dangling references, broken
`!include` paths, redefinitions, brace balance) for CI, which has no container
runtime. It is **not** the compiler — passing it does not mean the model renders.

To browse the model interactively instead:

```bash
docker run -it --rm -p 8080:8080 \
    -v "$PWD/docs/c4:/usr/local/structurizr" structurizr/structurizr local
```

## Layout

```
docs/c4/
  workspace.dsl              root; includes everything below
  model/model.dsl            people, the system, the workstation, L1 + L2 relationships
  l2/containers.dsl          containers, with their components declared inline
  l3/external-systems.dsl    everything Agnes talks to but does not run
  l3/api.dsl                 component relationships inside role: api
  l3/gateway.dsl             component relationships inside role: gateway
  l3/worker.dsl              component relationships inside role: worker
  views/views.dsl            the five views, and the styles
```

Adding a container touches `l2/containers.dsl` and nothing else. Adding a
component view means one new `l3/<container>.dsl` plus one view.

## Rules this model follows

**A view's key is its filename.** The view `agnes-c4-context` renders to
`docs/diagrams/agnes-c4-context.svg`. Renaming a view renames its figure, and
the `ARCHITECTURE.md` embed must move with it —
`tests/test_c4_model.py` fails if it does not.

**Implied relationships are off** (`!impliedRelationships false`). Structurizr
would otherwise roll a component's relationship up to its container and its
system, carrying the component's wording with it — so L1 would describe
BigQuery in a sentence written for L3. Each level states its own relationships
in the words that level's reader needs, which is the point of having levels.

**Cloud vendors are excluded from L2**, following the platform model: every
container depends on the cloud it runs in, so the same boxes on every diagram
hide the relationships that actually differ. They appear at L1, and granularly
at L3 where a specific component uses them.

**The model is idealized.** It shows every data source, identity provider and
model provider Agnes supports; any one instance connects a subset. Same
trade-off, and same consequence for impact analysis, as the platform model.

## What is not here

No level 4 (code). The C4 model advises against maintaining one, and the
repository already is that diagram.
[`docs/architecture.md`](../architecture.md) is the module-level reference.

The three layered figures in `ARCHITECTURE.md` are not part of this model. They
are compositions — deliberately arranged to be read in one pass — rather than
views selected from a graph, and are hand-laid-out by
`scripts/dev/gen_architecture_diagrams.py`.
