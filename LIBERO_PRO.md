# LIBERO-Pro evaluation

LIBERO-Pro runs through the existing Planner → action → Detector loop, with
native simulator success scoring, calibrated RGB-D motion, relative corrections,
per-turn result images, episode checkpoints and optional Analyzer review.
The classic LIBERO installation and configuration remain separate.

## Setup

The setup reuses the simulator dependencies from the existing `.venv` and creates
a lightweight `.venv-libero-pro` that imports only the Pro version of `libero`.
If classic LIBERO is not installed, run `bash scripts/setup_libero.sh` first.
No demonstrations are required or downloaded.

```bash
cd /mnt/aoss/vla/PAC-Harness
bash scripts/setup_libero_pro.sh
bash scripts/run_libero_pro.sh --check
bash scripts/run_libero_pro.sh --smoke-motion
```

If Hugging Face is unreachable, use:

```bash
HF_ENDPOINT=https://hf-mirror.com bash scripts/setup_libero_pro.sh
```

`--check` validates the pinned task files, initial-state files and requested state
indices without starting episodes or calling a model. `--smoke-motion` runs task 0
in each of the six suites through both calibrated cameras, gripper control and
relative motion, without model calls. Smoke results are never model scores.
Rendering defaults to EGL; the launcher does not require environment activation.

The Pro source checkout is pinned to
`Zxy-MLlab/LIBERO-PRO@eafdb809426b13153aa1e4c42d6601844217dfec`.
The task data is pinned to
`zhouxueyang/LIBERO-Pro@c86fc3b8293185a6f373677018ff3e37f8391602`.
The setup verifies all 120 required files against the pinned repository's blob
hashes and records SHA256 digests in `.deps/libero-pro-data/manifest.json`.
Paths are isolated in `maintenance/libero-pro-config/config.yaml`.

## Start with one task

```bash
bash scripts/run_libero_pro.sh \
  --suite libero_spatial_swap --task-ids 0 --init-state-ids 0 \
  --output logs/libero-pro-trial
```

Planner and model Detector use `config.libero-pro.json`: `gpt-6-astra`, reasoning
effort `xhigh`, Cubence Responses API, and `OPENAI_API_KEY`. The launcher also
supports the existing external key file through `CUBENCE_API_KEY_FILE`.
Keys do not belong in configuration files. Use `--config <file>` to select a
different configuration.

Pro task variants can reuse the original task filename while changing the
instruction and goal predicates. The terminal, Planner and recorded results use
the actual environment instruction, rather than the filename-derived wording.

## Evaluate the full six-column table

```bash
bash scripts/run_libero_pro.sh --output logs/libero-pro-eval
```

For explicit alignment with Robo-Harness K1's published case selections, use:

```bash
# Full sweep: six suites, tasks 0-9, stored states 0, 1, 2 (180 cases).
bash scripts/run_libero_pro.sh --protocol k1-180 --check
bash scripts/run_libero_pro.sh --protocol k1-180 --output logs/libero-pro-k1-180

# Astra comparison subset: six suites, tasks 0-2, stored state 0 (18 cases).
bash scripts/run_libero_pro.sh --protocol k1-18 --check
bash scripts/run_libero_pro.sh --protocol k1-18 --output logs/libero-pro-k1-18
```

Both profiles reject conflicting case selections and online Analyzer updates.
`--check` calls neither the simulator nor a model. Formal evaluation should start
after adaptation has completed and the role memories are frozen. See
[K1 alignment audit](K1_ALIGNMENT.md) for the distinction between published
case-selection alignment and unverified equality of K1's experiment data files.
The published Astra 88.9% is 16/18 on the subset, not a 180-case result.

The default plan is **6 suites × 10 tasks × 3 stored initial states = 180 episodes**.
The state indices are **0, 1, 2**; they are not three random seeds.
`--episodes N` selects states `0..N-1`; `--init-state-ids` selects explicit indices.
`--seed` controls simulator random seeding independently of the stored states.

| Table column | Suite |
|---|---|
| Object / Position | `libero_object_swap` |
| Object / Task | `libero_object_task` |
| Goal / Position | `libero_goal_swap` |
| Goal / Task | `libero_goal_task` |
| Spatial / Position | `libero_spatial_swap` |
| Spatial / Task | `libero_spatial_task` |

The default budgets are 2,400 counted simulator control steps per episode,
400 Planner turns, 384 × 384 images and at most 120 internal control steps per
Cartesian move. Ten initial settling steps precede the counted budget. One
Cartesian action commands its complete target; physical control steps still
consume the simulator budget. Detector runs after the action. `--max-steps` or
configuration changes alter the protocol and are recorded with the run.

These task/state selections follow the published Robo-Harness K1 six-column
evaluation setup. A matched protocol also requires checking model, action space,
available observations, budgets, termination and prior experience when making
paper comparisons; this adapter alone does not establish identical conditions.
References: [K1 results](https://github.com/Robo-Harness/k1/blob/main/docs/results.md),
[environment documentation](https://github.com/Robo-Harness/k1/blob/main/docs/environments.md),
[released configuration](https://github.com/Robo-Harness/k1/blob/main/configs/libero-pro.yaml).

## Results and recovery

The batch directory contains:

- `protocol.json`: suite/task/state plan, actual instructions, pinned input
  hashes and starting knowledge hashes.
- `config.json`: the effective model and runtime configuration.
- `summary.json`: native successes, completed/expected counts and episode results.
- `table.md` and `table.csv`: the six columns in the order above and Average.
- `<suite>/task-NNN-episode-NNN/`: existing Harness logs, simulator checkpoints,
  per-episode results and one `frames/turn-NNN.png` per dispatched Planner turn.

A suite score is filled only when all requested episodes in that suite finish.
Unrun or interrupted cells display `—`. Average is filled only for a completed
180-case sweep or 18-case K1 subset. Model completion claims do not replace the official
`check_success()` predicate. Smoke runs are explicitly unscored.

```bash
# Preserve completed episodes and resume unfinished ones.
bash scripts/run_libero_pro.sh --resume logs/libero-pro-eval

# Regenerate the table without simulator or model calls.
bash scripts/run_libero_pro.sh --summarize logs/libero-pro-eval
```

Resume uses the saved configuration and rejects conflicting suite/state/budget
options. The existing episode recovery rules apply when a dispatched action's
outcome cannot be reconciled.

## Analyzer and prior experience

Analyzer updates are **disabled by default** for a fixed-policy evaluation.
The starting `prompts/`, `memories/` and `skills/` files are hashed; keep those
files unchanged throughout the batch. A fixed-policy resume checks their hashes,
and suite transitions check for changes. Legacy experience retains its exact
suite scope. Analyzer may explicitly publish transferable object/operation rules
after reviewing their evidence; classic LIBERO rules are not automatically retagged.

To study adaptation as a separate protocol, opt in explicitly:

```bash
bash scripts/run_libero_pro.sh --post-run-review \
  --output logs/libero-pro-adaptation
```

For manual review after evaluation, pass a suite or episode directory:

```bash
bash scripts/analyze_libero.sh --config config.libero-pro.json \
  --run-dir logs/libero-pro-eval/libero_spatial_swap
```

Reviewing the logs does not rerun the simulator. Only Analyzer publishes new
concise rules to the existing Planner and Detector role memory files.

## Adapt on 30 separate episodes before evaluation

```bash
bash scripts/adapt_libero_pro.sh --output logs/libero-pro-adapt-30
```

The curriculum randomly selects five distinct tasks per suite and one stored
state from index 3 onward for each selected task. It excludes the evaluation
states 0, 1 and 2, producing 30 distinct task/state combinations. This is
adaptation on different initial states of the benchmark tasks; it is not an
unseen-task split. The saved sampling seed makes the selection reproducible.
Use `--sampling-seed N` to specify it, or `--plan-only` to save a selection
without running episodes.

Each episode uses the existing GPT Planner and Detector. Analyzer then reviews
its logs and images and publishes supported rules before the next episode starts.
The six suites alternate over five rounds. Analyzer failures are retried up to
three times with completed caches preserved; persistent failures stop the
curriculum before the next episode. Failed manipulation attempts can still
provide review evidence. No ground-truth demonstrations are supplied.

The directory contains `selection.md`, `plan.json`, `status.json`, separate
`episode-NNN.log` files, and per-suite episode batches. `knowledge-initial/` and
`knowledge-history/` preserve the starting and per-episode knowledge. A
`knowledge-final/` snapshot is written once all 30 reviews have completed.
Adaptation successes are separate from the held-out 180-episode result table.

```bash
# Continue the saved selection; preserve completed experiments and reviews.
bash scripts/adapt_libero_pro.sh --resume logs/libero-pro-adapt-30

# After all 30 reviews finish, evaluate with updates disabled by default.
bash scripts/run_libero_pro.sh --output logs/libero-pro-eval-after-adaptation
```

Do not run the fixed-policy evaluation concurrently with adaptation: its
knowledge files must remain unchanged for the duration of the evaluation.
