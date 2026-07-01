# Team Shapes

Use the smallest team that has clear ownership boundaries.

## Default

```text
orchestrator, implementer, collector, trainer
```

Use this when the project has code changes, external evidence gathering, and later training/eval work.

## Small

```text
orchestrator, implementer
```

Use this for ordinary code work where the orchestrator routes tasks and the implementer edits the repo.

## Data Run

```text
orchestrator, implementer, collector
```

Use this when a run or external system needs monitoring but there is no active training/eval owner.

## Train/Eval

```text
orchestrator, implementer, trainer
```

Use this when datasets, training, evaluation, or launch packets matter but live collection is out of scope.

## Naming

Role names must be short TOML-safe identifiers using letters, numbers, `_`, or `-`.

Good examples:

```text
orchestrator, implementer, collector-data, collector-diagnostics, trainer
```

Avoid role names that encode temporary task state. Pause, drain, or retire roles with `tmux-team role ...` instead of renaming the team for every incident.
