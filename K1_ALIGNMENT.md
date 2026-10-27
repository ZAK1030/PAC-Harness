# Robo-Harness K1 / PAC-Harness evaluation alignment

Audited on 2026-10-19 against
[`Robo-Harness/k1@ee46363101fcf3ef87182fb2dbad99a92ce77fc0`](https://github.com/Robo-Harness/k1/tree/ee46363101fcf3ef87182fb2dbad99a92ce77fc0).
This audit aligns the published case selections. It does not claim an exact
reproduction of unpublished K1 evaluation inputs or equal inference budgets.

## Which K1 result is being compared?

| Published experiment | Cases per suite | Total cases | Published result | PAC profile |
|---|---|---:|---|---|
| Full Gemini 3.7 Flash + K1 sweep | Tasks 0-9, stored states 0, 1, 2 | 180 | 139/180 = 77.2% | `k1-180` |
| GPT-6 Astra + K1 subset | Tasks 0-2, stored state 0 | 18 | 16/18 = 88.9% | `k1-18` |
| GPT-6 Astra RGB-only subset | Same 18 cases | 18 | 11/18 = 61.1% | `k1-18` case selection |
| Gemini 3.7 Flash + K1 subset | Same 18 cases; included in full sweep | 18 | 14/18 = 77.8% | `k1-18` |

Source: [K1 results, lines 7-38](https://github.com/Robo-Harness/k1/blob/ee46363101fcf3ef87182fb2dbad99a92ce77fc0/docs/results.md#L7-L38).
The Astra 88.9% must not be labeled as a 180-case result. There is no published
full-180 Astra result in this release. Published aggregate counts do not identify
which individual plate tasks succeeded.

## Plate-task provenance

Our actual BDDL instructions differ from their inherited filenames:

| Suite | Zero-based task ID | Inherited filename | Actual BDDL instruction | In full 180 selection? | In Astra 18 selection? |
|---|---:|---|---|---|---|
| `libero_goal_task` | 1 | `put_the_bowl_on_the_stove.bddl` | Put the plate on the stove | Yes, states 0-2 | Yes, state 0 |
| `libero_goal_task` | 4 | `put_the_bowl_on_top_of_the_cabinet.bddl` | Put the plate on the top of the drawer | Yes, states 0-2 | No |

Task 4's native goal is `(On plate_1 wooden_cabinet_1_top_side)`.
The goal requires the final plate/cabinet relation; it does not prescribe a
particular grasp technique. The classic LIBERO plate-pushing demonstration is
not evidence of a successful plate-lifting grasp.

The downloaded task 4 file was independently fetched from the official dataset
through `hf-mirror.com` at both revisions below. Both remote copies equal our
local file byte for byte:

- Current pinned revision: `c86fc3b8293185a6f373677018ff3e37f8391602`, 2026-07-22.
- Earlier revision: `3a33dc4cfbea7ff5eac6764e9c557500972b6259`, 2025-12-03.
- SHA256: `8ebaeda89603edc8f149c295ca8eaf159e2af6c6640f96819f9d41b354471ef5`.
- [Official task file](https://huggingface.co/datasets/zhouxueyang/LIBERO-Pro/blob/c86fc3b8293185a6f373677018ff3e37f8391602/bddl_files/libero_goal_task/put_the_bowl_on_top_of_the_cabinet.bddl).

K1 uses the same LIBERO-Pro source revision and reads instructions from the
environment, explicitly avoiding stale filename labels. Its published full
selection includes task 4. However, K1 does not release its experiment dataset
revision, input hashes or evaluation manifests. Therefore the evidence supports
inclusion of that task index under the published protocol; it cannot certify
that K1's actual experiment used our exact plate-task bytes.

## Alignment and remaining differences

| Item | K1 public release | Current PAC evaluation | Assessment |
|---|---|---|---|
| LIBERO-Pro source | `eafdb809426b13153aa1e4c42d6601844217dfec` | Same pinned revision | Aligned |
| Task data | User-supplied BDDL/init files; experiment hashes not released | `zhouxueyang/LIBERO-Pro@c86fc3b...`; all 120 local files hash-verified | Exact equality unverified |
| Suites | Object, Goal, Spatial, each `swap` and `task` | Same six suites | Aligned |
| Task/state selection | 180-case sweep and separate 18-case subset | Explicit profiles for both | Aligned with public selection |
| Instruction | Actual environment instruction | Actual environment instruction | Aligned source of instruction |
| Simulator budget | 2400 counted native control steps | 2400 counted control steps by default | Aligned default |
| Images | 384 x 384, external and wrist RGB-D | 384 x 384, external and wrist RGB-D | Aligned modalities/resolution; presentation/tools differ |
| Control rate | 20 Hz | Upstream LIBERO default, 20 Hz | Same configured/default rate |
| Reset | Stored state; seed = state index; 10 open-gripper settling steps; explicit OSC cache refresh | Stored state; seed = base seed + state index, base seed 0 by default; 10 settling steps | Same selection/seed/settling defaults; reset implementations are not identical |
| Score | Native `check_success()` | Native `check_success()` | Aligned criterion |
| Stop after native success | Yes | Yes | Aligned |
| Post-release diagnostic | Separate diagnostic; excluded from main score | Not the main score | Do not mix diagnostic and native results |
| High-level budget | 400 model/tool rounds | 400 Planner turns plus Detector/tool calls | Numerically similar, not an equal API-call/token budget |
| Motion primitives | K1 relative/absolute servo tools; full move up to 120 steps | PAC calibrated/relative tools; full move up to 120 steps | Same maximum default; different methods/tolerances |
| Grasp proposal model | Paper Appendix A.3 specifies a GraspGen Franka Panda checkpoint; 100 samples, up to 24 candidates, at most three shown to the agent | Current PAC Planner selects targets using its observations and calibrated motion tools; no equivalent GraspGen provider is integrated | Additional learned grasp model in K1; disclose this method difference |
| Model | Full 180: Gemini 3.7 Flash; Astra only on 18 subset | GPT-6 Astra, `xhigh` by default | Use subset for the published same-model-name comparison; reasoning/provider settings are not proven identical |
| Prior experience | Development overlap disclosed; exact development history not released | Classic development, 30 Pro adaptation cases, Analyzer/user feedback history | Disclose explicitly; not equal prior knowledge |
| Updates during evaluation | Public release has no equivalent PAC Analyzer protocol | Disabled for named profiles; knowledge hashes checked | Freeze PAC memories; report adaptation separately |

Source links: [environment](https://github.com/Robo-Harness/k1/blob/ee46363101fcf3ef87182fb2dbad99a92ce77fc0/docs/environments.md),
[configuration](https://github.com/Robo-Harness/k1/blob/ee46363101fcf3ef87182fb2dbad99a92ce77fc0/configs/libero-pro.yaml),
[adapter](https://github.com/Robo-Harness/k1/blob/ee46363101fcf3ef87182fb2dbad99a92ce77fc0/src/robo_harness/libero_adapter.py).
The paper's [Appendix A.3](https://arxiv.org/html/2609.29389v1#A1.SS3)
specifies the learned grasp provider; the public code exposes it as an optional
backend. [Appendix E.3](https://arxiv.org/html/2609.29389v1#A5.SS3) shows a
bowl-to-plate trace, not a plate-lifting trace. The paper and project page do
not provide a plate-specific contact procedure or identify individual plate-task
successes. Generic grasp-proposal details are not evidence of a successful
plate grasp.
Configuration overrides change these defaults and must be disclosed.

## Run the aligned case selections

First finish adaptation and freeze prompts, skills and role memories. Do not
run formal evaluation while another process updates those files.

```bash
# Validate only: no environment rollouts or model calls.
bash scripts/run_libero_pro.sh --protocol k1-18 --check
bash scripts/run_libero_pro.sh --protocol k1-180 --check

# Same published 18-case selection as the Astra comparison.
bash scripts/run_libero_pro.sh --protocol k1-18 --output logs/libero-pro-k1-18

# Full 180-case selection; compare against the full Gemini result with the
# model difference and our adaptation history stated explicitly.
bash scripts/run_libero_pro.sh --protocol k1-180 --output logs/libero-pro-k1-180

# Resume using the saved configuration and knowledge hashes.
bash scripts/run_libero_pro.sh --protocol k1-18 --resume logs/libero-pro-k1-18
```

Named profiles reject conflicting suite/task/state selections and online review.
Each run saves the actual task instructions, local benchmark hashes, reference
commit, effective configuration and starting knowledge hashes. Incomplete groups
remain unscored. A completed 18-case selection now gets its own Average cell;
it is never marked as a completed 180-case evaluation.

The 30 adaptation episodes use states outside 0-2, but the same benchmark task
conditions. This is a different-initial-state adaptation split, not unseen-task
generalization. Any GT-informed human feedback is also part of the prior
knowledge history and must be reported.

## Saved audit evidence

Files in [`artifacts/k1-protocol-audit/`](artifacts/k1-protocol-audit/):

- `task-alignment.csv`: all 60 task identities, actual instructions, native
  goals, local input hashes and membership in each published case selection.
- `k1-18.json`, `k1-180.json`: explicit 18/180 episode selections and local hashes.
- `upstream/`: public K1 documents/configuration/source at the reference commit.
- `source.json`: reference repository/commit and Git tree.
- `dataset-history-1.json`: official dataset commit history fetched via mirror.
- `*-goal-task4.bddl`: independently downloaded current/earlier task 4 files.

The generated selections describe our verified local data and K1's public
selection rules. They are not K1's unpublished experiment manifests. Exact
episode-level reproduction requires those original manifests/data hashes.
