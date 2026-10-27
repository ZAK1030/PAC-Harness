# LIBERO 接入与评测

For the six-suite LIBERO-Pro benchmark, use the separate [LIBERO-Pro setup and evaluation guide](LIBERO_PRO.md).

The `analyzer` reviews complete episode logs and result images after each experiment, using the Planner model connection (`gpt-6-astra` / `xhigh` by default). It produces concise English rules; it does not automatically modify actions, controllers or agent capabilities.

Runtime advice is stored in `memories/planner.analyzer.json` and `memories/detector.analyzer.json`. Each English rule states when to apply it, what to do, how to verify the outcome and when to reconsider. Rules are limited to 300 characters each; a request loads at most six distinct rule IDs, totaling at most 1,800 characters. Matching uses adapter, suite and task keywords. Keywords require all matches by default; `keyword_match: any` expresses alternative objects or destinations. Analyzer can explicitly mark an object/operation procedure as transferable across suites (`applicability: adapter`, exported suite `*`); legacy rules keep their exact suite scope. Critical manipulation rules rank before normal/low-priority reminders, then more specific matches and newer entries. Unverified hypotheses remain in the audit archive. Run-specific coordinates and heights must not become reusable constants.

Full reports and intermediate caches remain in the episode's `review/` directory. `maintenance/analyzer/lessons.json` retains evidence and deduplication records. Planner and Detector read only their concise role rules, not these detailed reports. The `experience_loaded` event records loaded rule IDs and file paths. Source quotes remain verbatim; all newly authored summaries, rules and code suggestions use English. Code suggestions are recorded without being applied.

Completed, exhausted and failed episodes can be reviewed. Smoke, preview and interrupted runs skip automatic review. A review failure preserves scores and completed caches.

Configuration: `analyzer.enabled`, `analyzer.chunk_turns` (default 4), `analyzer.synthesis_fan_in` (default 4; range 2-8), and `analyzer.stream` (Responses streaming enabled by default). Large failed review or synthesis groups can be split. The analyzer condenses and validates full-run findings before publishing role rules. Legacy `to_user.post_run` settings remain supported; interactive ToUser is independent.

Compaction preserves complete action/verification clauses. If its model request fails, the full review and caches remain retryable and existing memory stays unchanged; the program does not shorten advice by cutting characters.

Use the normal experiment command. `--no-post-run-review` disables updates for that run; `--post-run-review` enables them. Freeze role rules and disable updates for fixed-policy benchmarks.

Review historical logs without starting the simulator:

```bash
bash scripts/analyze_libero.sh --run-dir logs/libero-20261013-020150-3fdd1dbd
```

The command accepts a batch or episode directory; `scripts/review_libero.sh` remains compatible. Existing full reports can be condensed without reprocessing images. Earlier Chinese rules are regenerated in English by the analyzer. `--force` repeats the entire review. Override model settings with `analyzer.model`; `to_user.enabled` controls only interactive ToUser.

Supply operator observations and review priorities with `--feedback`:

```bash
bash scripts/analyze_libero.sh \
  --run-dir logs/libero-20261013-103722-9ff023af/task-000-episode-000 \
  --feedback 'In agentview, repeated placements appear left of the plate center. Check this against all placement attempts. Evaluate a small image-right correction using current calibrated geometry and bowl-to-TCP offset; verify centering before release. Preserve the supported procedure in concise Planner and Detector rules.'
```

Feedback is saved in the episode's `review/user-feedback.json` and reused when retrying without the flag. New feedback replaces the previous text and invalidates chronological, synthesis and compaction caches; no `--force` is needed. On a batch directory, the same feedback applies to every episode. Feedback directs all review stages but does not count as observed outcome evidence. Untested corrections remain hypotheses, and reusable rules remain English. Specify the camera or coordinate frame for directions; image-right is not a fixed robot axis or displacement. To remove saved feedback, delete `review/user-feedback.json` and rerun.

If either role rule file was deleted, running the same command restores missing files from the current analyzer archive without a model request. Existing role files are preserved. If the archive was deleted too, the completed report's validated concise output can rebuild it and the role files. A cached completion is reused only after checking that both role outputs exist.

To refocus accumulated experience without rerunning the simulator or every visual review:

```bash
bash scripts/analyze_libero.sh --config config.libero-pro.json \
  --consolidate-memory \
  --feedback 'Prioritize bowl-wall contact before closure, held-object centering before descent, and prompt detection of object or container tipping. Keep untested plate-grasp remedies as hypotheses. Merge covered duplicates while preserving exceptions.'
```

This is a text-evidence review of the archive, not a fresh inspection of images. Analyzer returns at most four focused findings and explicitly chooses priority and transferable applicability. Original outcome quotes are checked against their source events. Only supported findings enter role memory; Analyzer may retire covered duplicate/conflicting lessons by exact catalog key. Retired lessons and their evidence remain in the archive. The report, checked excerpts, original event locations and pre-change memory snapshots are saved under `maintenance/analyzer/consolidation/`. A concurrent archive update aborts publication so it can be retried against the latest memory. For archives spanning multiple adapters, supply `--adapter adapters.libero:create`.

该适配器把官方 LIBERO 仿真接入现有 `Planner → action → Detector` 调度。指标为**任务成功率**，不是离线动作预测准确率。批量评测默认用官方 `check_success()` 作为 Detector，不把模型的完成声明算成成功。若要测试原来的 GPT Planner + GPT Detector 配合，添加 `--detector model`；统计分数仍使用官方仿真谓词。当前支持 `libero_spatial`、`libero_object`、`libero_goal`、`libero_10`、`libero_90`。

## 安装与无模型验收

Linux、Python 3.11（安装脚本需要 `uv`，缺失时先执行 `python -m pip install uv`），需联网获取 LIBERO（含 assets、BDDL、初始状态）；评测无需下载演示 HDF5 数据。安装脚本固定 LIBERO commit `8f1084e3132a39270c3a13ebe37270a43ece2a01`。默认 EGL 无显示渲染，需机器提供 EGL 驱动；Debian/Ubuntu 可执行 `sudo apt-get install libegl1 libgl1 libosmesa6`。CPU 机器可安装 Mesa OSMesa 并设置 `MUJOCO_GL=osmesa`。

```bash
bash scripts/setup_libero.sh
source .venv/bin/activate
python -m pac_harness.libero_eval --smoke
```

`--smoke` 使用官方第 0 个初始状态执行两次零运动动作，通过真实 Harness 后检、保存相机图片、回执与结果。此结果不是模型成绩。

当前工作区已安装环境，直接 `source .venv/bin/activate` 即可；此机器环境与上游资源存放在 `/opt/pac-libero/`，项目 `.venv` 与 `.deps/LIBERO` 指向该目录。换机器请执行安装脚本，不复制虚拟环境。

当前机器可直接运行：

```bash
bash scripts/run_libero.sh --smoke
bash scripts/run_libero.sh --smoke-motion
bash scripts/run_libero.sh --task-ids 0 --episodes 1
```

启动脚本优先使用 `OPENAI_API_KEY`；当前提供的密钥保存在仓库外 `/opt/pac-libero/cubence-api-key`，权限 600。可用 `CUBENCE_API_KEY_FILE` 指定其他文件。密钥不进入 Git、模型配置或评测日志。

只获取双相机观察：

```bash
python -m pac_harness --config config.libero.json --observe-only
```

首次导入自动生成项目隔离的 `maintenance/libero-config/config.yaml`，避免上游交互提示阻塞启动。已有 `LIBERO_CONFIG_PATH` 则沿用用户路径。

## 配置模型并评测

复制 `config.libero.json` 为 `config.libero.local.json`，Planner 和模型 Detector 默认已配置 Cubence `https://api-lb.cubence.ai/v1`、`gpt-6-astra`、Extra High 推理（`reasoning_effort: xhigh`）、`wire_api: responses`。可修改 `models.planner` 的模型名称、服务地址及密钥环境变量名；Responses 请求始终 `store: false`，现有 Chat Completions 配置仍可使用。模型需要支持工具调用、JSON 输出及图像输入；通过 `inspect_artifact` 获取图像。配置里不写密钥。

```bash
export OPENAI_API_KEY='实际密钥'
python -m pac_harness.libero_eval --config config.libero.local.json \
  --suite libero_spatial --task-ids 0 --episodes 1
python -m pac_harness.libero_eval --config config.libero.local.json \
  --suite libero_spatial --episodes 50
```

各 episode 使用对应官方初始状态，固定任务顺序 0，默认先稳定 10 步，之后开始计步。启动时终端打印任务指令、套件、任务编号、episode 与步数上限；结束时打印 `sim_steps=已用/上限` 和停止原因。默认 horizon：spatial 220 / object 280 / goal 300 / 10 520 / 90 400；可用 `--max-steps 1000` 或 adapter.settings.max_steps 覆盖（改变评测协议）。这里的 step 是一次 `env.step()` 控制周期，不是一次 Planner 动作；一次 Cartesian 动作可能消耗多个 step。达到上限且未成功时停止原因是 `episode_exhausted`；Harness 的 `run.max_turns` 是另一个规划轮数预算。超过初始状态数量直接报错，不循环复用。summary.json 保存总体成功率、任务指令、每任务成功次数、episode 状态、步数和模型名称；每个 episode 另存 Harness 事件、检查点、仿真状态及按规划轮次编号的结果 PNG。未完成或预算耗尽也记入分母。启动/环境故障会终止评测，不伪装成模型失败。

## 动作与恢复

默认启用 `10d86c3` 的两种移动方式，配置在 `adapter.settings.cartesian`。无需实体相机标定板或实体机械臂 SDK：从 MuJoCo 读取双相机内参、固定相机到机器人基座的外参、腕部相机到 TCP 的刚性变换。RGB 和原生同光心深度同步翻转；深度缓冲转为光轴方向的米制深度。每个 episode 的 `calibration/factory.json` 与 `calibration/extrinsics.json` 保存参数，`frames/` 每个执行轮次只保存一张动作完成后的固定相机 PNG，命名 `turn-001.png`、`turn-002.png`，对应终端 `[1] Planner`、`[2] Planner`；`frames.json` 记录对应动作、回执 ID、仿真步数及成功状态。重复观察和重试不增加结果图；被校验拒绝的计划不产生结果图，`done` 轮次保存当前场景。双相机推理图使用 `.observations/` 缓存，只保留最近两个仿真状态（最多四张），同一状态重复读取复用图片，以支持 Detector 前后对比和断点恢复。较早的观察缓存路径会被清理，历史结果请使用 `frames/` 和 `frames.json`。模型仍通过 `inspect_artifact` 查看图像，策略观察不包含物体位置或分割真值。

| 接口 | 用途 | 坐标与约束 |
| --- | --- | --- |
| `move_calibrated` | 选择图中的目标像素，自动投影到三维，一次动作下发完整目标位姿 | `observation_id`、`target`、`view`、`pixel_uv: [u,v]`；可选基座系 `approach_offset_m`。LIBERO 默认 `calibrated_direct: true`，不受 5 cm 粗移动截断；到达后用新图像进行 relative 微调 |
| `move_relative` | 基于当前观察作精细修正 | `observation_id`、`target`、`delta_m: [dx,dy,dz]`；基座系、米制，默认向量长度最多 1 cm |
| `locate_calibrated_target` | 只读计算选中像素的三维位置与标定证据 | 与粗移动相同的参数，不执行运动 |
| `robot_action` | 原有增量动作，控制夹爪与旋转 | 最多 `chunk_size` 个 7 维归一化向量，平移/旋转使用 **世界坐标系** |

`view` 为 `agentview` 或 `robot0_eye_in_hand`；像素以已保存 PNG 左上角为原点，u 向右、v 向下。机器人 `robot.pose` 为 **机器人基座坐标系的 grip_site TCP**，XYZ 单位米、旋转为 extrinsic xyz 欧拉角（弧度）。不要把相机图中左右方向直接当成基座方向。旧版原始 `robot0_eef_pos` 是世界坐标，两者不可混用。移动后旧图像不能再次用于移动，必须选择当前 `observation_id`。

粗移动示例（替换成当前观察 ID 和实际目标像素）：

```json
{"name":"move_calibrated","arguments":{"observation_id":"sim-…","target":"black bowl","view":"agentview","pixel_uv":[120,150],"approach_offset_m":[0,0,0.10]}}
```

粗、精细动作保持当前夹爪指令和 TCP 朝向。闭合夹爪可使用 `robot_action` 的 `[0,0,0,0,0,0,1]`；打开为 `[0,0,0,0,0,0,-1]`。Cartesian 目标通过 OSC 的绝对姿态模式执行，结束或异常时恢复原有增量模式，不瞬移物体或机械臂。一次 `move_calibrated` 直接发送完整三维目标加偏移，默认在动作内部最多执行 40 个控制步以检查实际到达；不能保证远距离运动在一个底层 `env.step()` 完成。位置到达容差 2 mm、旋转容差 0.02 rad；每个内部控制步计入任务 horizon。直达回执 `motion_mode: coarse_direct` 包含完整目标/实际姿态、计划距离、误差、步数和停止原因；到达末端位置不代表 LIBERO 任务成功。若出现 `move_step_limit`，可调整 `cartesian.move_max_steps`；不会越过 episode horizon。已测得的未到达允许重新规划，异常结果不明时禁止重放。工作区只限制起点和终点，不进行障碍路径规划；Planner 应先选择目标上方偏移，再用 relative 下降微调。设置 `cartesian.calibrated_direct: false` 可恢复旧的每次最多 `safety.coarse_step_m`（默认 5 cm）及 fine entry 限制。

`--smoke-motion` 不调用模型，通过真实 Harness 执行固定相机粗移动、夹爪闭合、腕部相机粗移动和精细上移。`summary.json` 的 `motion_checks.passed` 检查三次移动实际到达和夹爪保持；退出码非零代表验收失败。这是接口验收，不是任务成功率。关闭新接口可设置 `adapter.settings.calibrated_motion: false`，保留仅 `robot_action` 模式。

几何与执行回归测试：

```bash
.venv/bin/python -m unittest discover -s tests -q
PAC_LIBERO_SIM_TESTS=1 MUJOCO_GL=egl .venv/bin/python -m unittest discover -s tests -p test_libero_motion.py -v
```

后者额外用 MuJoCo 独立射线验证双相机像素深度投影，以及机械臂移动后的腕部外参和旧观察拒绝。标定模式关闭离屏 MSAA 多重采样，使深度对应像素中心；相机内参也使用像素中心约定，避免斜面上毫米级的采样偏差。这会改变图像抗锯齿外观，评测日志记录 `calibrated_motion` 设置。

完成动作后将 MuJoCo state 与回执一起保存；发送前保存 pending 标记，结果不明时禁止重放。可通过普通 Harness CLI `--resume` 恢复单个运行。

Resume an interrupted batch with its saved task order and evaluation settings:

```bash
bash scripts/run_libero.sh --resume logs/libero-20261013-162046-b37bbb19
```

Completed result files are preserved. Interrupted episodes resume from the saved simulator and Harness state; unstarted episodes begin normally. Stop the old process before resuming. Changes to suite, task selection, seed, detector mode, episode count or horizon are rejected. The current Planner model must match the batch model.

LIBERO terminal success is authoritative for episode finalization. The last executed action still receives a Detector postcheck; the runtime then records the environment terminal result and exits without another Planner or repeated `done` submissions. Detector uncertainty about release or support is retained separately and never rewritten as verified. This also applies when resuming an already successful episode. Agents for environments without a terminal-success hook retain their existing completion protocol.

模型响应故障分层记录在 `events.jsonl`：`model_error`/`model_retry` 包含 HTTP 状态、Content-Type、请求 ID、响应长度与 SHA256、解析阶段和脱敏内容片段。认证信息及图像 base64 不写入诊断。HTTP 空响应、HTML、暂时性网络错误及 429/5xx 在模型请求内部最多额外重试两次，退避 1/2 秒（`models.<role>.max_transport_retries`）。Responses 请求明确 `stream: false`；若服务仍返回 SSE，只接受完整终态，拒绝从半截文本构造动作。完整 JSON 的单层 Markdown 包装可直接解包；空或错误的模型 JSON 会在同一规划/检测轮内向模型反馈具体错误，保留已读取证据，最多纠正两次（`max_format_repairs`），并继续经过原有动作和报告校验。诊断或纠正请求不会执行机器人动作；超过内部预算后才进入 Harness 的 `run.max_failures` 重试。持续服务故障仍会停止，`summary.json` 的停止记录现在包含具体错误原因。

当前接入的是现有通用模型 Planner；OpenVLA、π0 等 VLA checkpoint 需要实现相应策略接口与训练时一致的预处理/反归一化，不能仅把 checkpoint 名写进 Chat Completions 配置。不同模型、horizon、图像预处理和 Detector 设定下的成绩不能直接与论文数字比较。
