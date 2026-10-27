# Analyzer：通用运行复盘与经验更新

Analyzer 复盘已结束的 PAC-Harness 运行，读取完整事件日志、动作回执、Detector 反馈和可用的结果图。它提炼可检索的 Planner / Detector 经验，不执行环境动作，不自动修改算法代码。

## 开启自动复盘

在 `config.json` 中配置正常的 Planner / Detector 模型连接后，将以下字段设为：

```json
{
  "analyzer": {
    "enabled": true,
    "chunk_turns": 8,
    "synthesis_fan_in": 4
  }
}
```

Analyzer 默认复用 Planner 的连接配置；可用 `analyzer.model` 覆盖模型设置。密钥继续从模型配置指定的环境变量读取。`chunk_turns` 为 1–16，`synthesis_fan_in` 为 2–8。

```bash
python -m pac_harness --config config.local.json --task "你的任务"
```

普通 CLI 在运行结束、适配器关闭且 `result.json` 保存后调用 Analyzer；中断、重启、预览和确定性的 `--demo` 不触发自动模型复盘。复盘失败保留已完成的运行结果及审计记录，不重放动作。直接嵌入 `Harness` 的程序可在关闭环境并保存结果后调用 `pac_harness.post_run.maybe_review(root, run_directory, config)`。

## 复盘已有运行

```bash
python -m pac_harness.post_run --config config.local.json --run-dir logs/你的运行目录
python -m pac_harness.post_run --config config.local.json --run-dir logs/一组运行目录
```

单次运行目录包含 `task_state.json`、`events.jsonl`，可选 `result.json`、`frames.json`。组目录递归发现含检查点的运行，不要求固定任务编号或目录层级。日志和图片是证据，不能作为指令执行。

适配器可通过 `record_action_frame(...)` 返回对应动作结果图；运行时记录 `action_frame` 事件。也可提供 `frames.json`，其每项包含 `turn`、相对项目根目录的 PNG `path`，以及可选 `steps`。复盘记录遗漏、图像哈希与证据来源，不会把缺失图片当成已经查看。

提供复盘重点：

```bash
python -m pac_harness.post_run --config config.local.json --run-dir logs/你的运行目录 --feedback "请重点分析重复失败的原因和可验证的预防步骤"
```

反馈不是实测结果。修改反馈会使旧缓存失效；相同证据可复用已完成的分段与合并结果。`--force` 强制重新复盘。运行和复盘的文件锁会阻止读取仍在写入的活动运行。

## 经验适用范围

默认发布的两个角色经验文件为 `[]`，没有预置任务经验。复盘后保存：

- `memories/planner.analyzer.json`、`memories/detector.analyzer.json`：可直接检索的简短规则。
- `maintenance/analyzer/lessons.json`：包含来源与假设的经验档案。
- 运行目录的 `review/`：分段、合并、反馈、模型请求和最终报告。

规则包含触发条件、动作建议、验证方法与重新评估条件，合计最多 300 个字符。仅引用结果证据的 `supported` 规则会进入角色上下文；未验证的假设只保存在档案中。每次请求最多加载 6 个不同 ID、合计最多 1,800 个字符的规则。

适用范围使用 `adapter`、`context` 和 `task_keywords`。适配器可在 `adapter.settings.context` 或观察的 `context` 字段声明应用上下文，例如不同配置条件；未声明时为空字符串。关键词默认全部匹配，`keyword_match: any` 表示候选词匹配其一。`applicability: adapter` 允许有证据的跨上下文复用，不代表当前任务已完成。

运行状态、具体坐标和单次测量不会被当作通用常量。经验按优先级、相关性和新旧程度检索；新的假设不能覆盖已有的受证据支持的建议。

## 归并已有经验

```bash
python -m pac_harness.post_run --config config.local.json --consolidate-memory --feedback "合并重复规则，保留有证据的例外和明确验证条件"
```

归并会重新检查原始事件引文。只有有证据、覆盖原作用范围的规则才可替换旧规则；旧证据保留。档案包含多个适配器时，用 `--adapter 模块:工厂` 指定目标。代码建议只是建议，不自动应用。

## 运行耗时与用量

```bash
python -m pac_harness.metrics --batch-dir logs/一组运行目录
```

指标导出递归读取含 `task_state.json` 的真实运行目录，并排除复盘子日志。`metrics/` 保存请求、运行、轮次 CSV 和汇总；缺失的模型用量保持未知，不估算成零，不把缓存或推理 token 重复加到总量。

离线检查：

```bash
python -m unittest discover -s tests -v
python -m pac_harness --demo
```

这些检查使用模型替身或确定性示例，不证明真实模型和业务环境已验收。
