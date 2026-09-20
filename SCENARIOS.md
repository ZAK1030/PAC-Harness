# 新场景操作指南

本指南针对当前 `PAC-Harness 0.1.0`。推荐运行 `python -m pac_harness --web`，使用 **新建场景 → 创建并开始对话**。网页会保存场景描述并自动交给 ToUser，详见 [网页工作台指南](WORKBENCH.md)。以下是可替代网页向导的手动流程。

Linux 使用 `python3` 替换 `python`，路径使用 POSIX 格式；网页向导、`--assist`、任务恢复和离线测试均可用。Linux 终端暂不支持 Ctrl+G 全局热键，直接在另一个终端运行 `python3 -m pac_harness --assist --run-dir logs/<实际运行目录>`。

## 1. 确定场景边界

“场景”是可复用的环境、工具和知识集合；“任务”是该场景内的一次目标。例如“文档整理”可以是场景，“整理本周收到的 20 份报告”是任务。换任务不一定需要新场景；换业务系统、操作接口或身份规则时通常需要。

每个场景至少需要四部分：

| 部分 | 由谁提供 | 保存位置 |
|---|---|---|
| 场景需求及验收目标 | 用户描述，ToUser 追问缺项 | `SCENE.md` |
| 与环境交互的实际能力 | 接入者或 ToUser 编写代码，用户提供接口信息 | `adapters/` |
| 持久知识与可选方法 | 用户确认事实，ToUser 归纳并写入文件 | `memories/`、`skills/` |
| 模型与业务连接参数 | 用户配置；密钥使用环境变量 | `config.json` 或本地配置 |

用户不必先提供 Python 实现，但需要说明有哪些真实接口可用。ToUser 无法仅凭“帮我处理业务”的描述就访问一个尚未接入的系统。

## 2. 创建独立的干净项目目录

以下 PowerShell 示例只复制通用模板和测试，不复制运行数据。将路径改为你准备建立的场景目录；目标必须尚不存在，避免覆盖已有项目。

```powershell
$coreRoot = 'C:\Biology\Harness-Core'
$sceneRoot = 'C:\Biology\Harness-Scenes\document-review'
if (Test-Path -LiteralPath $sceneRoot) { throw '目标场景目录已存在，请换一个目录。' }
New-Item -ItemType Directory -Path $sceneRoot -Force | Out-Null
$templateEntries = @(
    'pac_harness', 'examples', 'tests', 'memories', 'prompts', 'skills',
    'config.json', 'pyproject.toml', '.gitignore', 'README.md',
    'ADAPTERS.md', 'SCENARIOS.md', 'EXTRACTION.md', 'VALIDATION.md', 'templates'
)
foreach ($entry in $templateEntries) {
    Copy-Item -LiteralPath (Join-Path $coreRoot $entry) -Destination $sceneRoot -Recurse
}
Copy-Item -LiteralPath (Join-Path $coreRoot 'templates\SCENE.md') -Destination (Join-Path $sceneRoot 'SCENE.md')
Set-Location -LiteralPath $sceneRoot
python -m pac_harness --demo
```

这里假设源目录仍是通用模板，其记忆和技能没有被改成某个具体场景。若已改过，应使用保存的干净版本，不要把旧场景经验带入新场景。

初次 `--demo` 只检查框架能启动，运行的是附带的列表示例。输出 `completed` 不表示你的新业务场景已经接通。复制的验证记录也只是原模板的记录，不是新场景的验收结果。

此方案每个场景拥有一份核心代码副本，便于当前 ToUser 在场景内独立分析和修改。后续修复通用框架时，需要通过 Git 或发布流程显式同步，不会自动传播到所有场景。

## 3. 填写场景描述

编辑新项目根目录的 `SCENE.md`，参考 [模板](templates/SCENE.md)。至少写清：

1. 希望系统完成什么，什么结果才算成功。
2. 系统能看到哪些数据、文件、界面或传感信息。
3. 系统能执行哪些操作，通过文件、API、SDK 还是其他接口完成。
4. 操作失败、部分完成或结果不明时，怎样核对和恢复。
5. 哪些知识是本场景长期有效的，哪些只是这一次任务的要求。

不清楚的项目写“待确认”，不必凭空填写。可先放一份脱敏输入与预期输出到 `references/`，在说明里列出路径。场景描述中不放真实密钥。

## 4. 在运行任务前打开 ToUser

当前已有一个不依赖正在执行任务的入口：

```powershell
python -m pac_harness --assist --task '建立新场景。先阅读 SCENE.md，确认缺失信息，再实现适配器、知识和离线测试。'
```

这个命令打开对话后仍会等待终端输入，`--task` 是背景说明，不会自动发送第一条消息。可以输入：

```text
请按 SCENE.md 建立这个场景。先说明你理解的输入、动作和成功条件，缺少信息就问我。
请把场景能力放在 adapters/document_review.py，补充 tests 中的离线测试。
把确认的长期事实写入 memories/shared.md，需要的方法写入 skills。
不要执行真实业务操作。最后说明我该如何设置 adapter.factory 和 adapter.settings。
```

ToUser 可以先追问接口细节，再编辑文件。只回复“记录了指导”不代表完成接入；应能看到实际生成的适配器、测试及知识文件。

当前 `SCENE.md` 是供 ToUser 阅读的需求文件，不会被两个任务 Agent 自动加载。Planner/Detector 的持久知识来自 `memories/`、`prompts/` 和按需技能。没有指定既有运行目录的独立 `--assist` 不会创建可恢复的业务任务检查点，纯会话指导也不会自动成为下一次任务的知识；建场景时应要求 ToUser 将结论写入文件。

当前 ToUser 成功应用一次修改后会结束对话。若接入还需要下一轮，重新使用 `--assist`，让它继续阅读已保存的 `SCENE.md` 和生成文件。这个入口适合建立场景，但还不是有持久阶段管理的建场景向导。

## 5. 核对生成文件并设置配置

一个准备运行的场景大致如下：

```text
document-review/
  SCENE.md
  config.json
  pac_harness/
  adapters/
    __init__.py
    document_review.py
  prompts/
    planner.md
    detector.md
  memories/
    shared.md
    planner.md
    detector.md
  skills/
  tests/
  references/
  logs/
  maintenance/
```

适配器工厂形式为：

```python
def create(*, root, run_directory, config):
    return DocumentReviewEnvironment(root, run_directory, config)
```

`DocumentReviewEnvironment` 需要实际实现 [适配器契约](ADAPTERS.md)，不能只保留类名占位。`adapters/__init__.py` 用于明确建立本场景的 Python 包。

ToUser 当前不能应用配置修改。它应把推荐的具体配置写在回答或说明文件里，由你更新配置。例如只替换 `config.json` 的 `adapter` 部分，保留原有 `models`、`run`、`to_user`：

```json
{
  "factory": "adapters.document_review:create",
  "settings": {
    "input_directory": "data/inbox",
    "output_directory": "data/reviewed"
  }
}
```

其中两个目录参数只是示例，必须与适配器实际读取的参数一致。相对业务路径应由适配器明确基于传入的 `root` 解析，并检查操作范围，不能假定核心自动限制所有 Python 文件访问。

随后设置 `models.planner` 和 `models.detector` 的有效模型、地址和密钥环境变量。也可以使用 `config.local.json`，运行时加 `--config config.local.json`；但这个本地配置不会被复制给 ToUser，它默认只看到脱敏后的 `config.json` 以及已有任务上下文，需要另行提供与排障相关的非敏感设置。

## 6. 验证后运行

在该场景目录中：

```powershell
python -m unittest discover -s tests -v
python -m pac_harness --task '整理本周报告并生成核对清单' --preview
python -m pac_harness --task '整理本周报告并生成核对清单'
```

测试应包含新场景适配器的正常效果、输入异常、已知失败和结果查询，而不只是原模板测试。`--preview` 会调用观察接口和模型、校验计划，但不调用 `execute`；因此适配器构造和只读方法也必须遵守无业务副作用的约定。准备好实际业务连接后再运行最后一条。

改用自定义适配器后不要加 `--demo`，它仅支持附带的列表示例。若需要新场景的无模型演示，应像现有测试一样为该适配器提供确定性的测试 Agent。

## 7. 切换与隔离

可以分别进入两个项目目录启动，也可从 Core 目录明确指定根目录：

```powershell
python -m pac_harness --root C:\Biology\Harness-Scenes\document-review --task '整理报告'
python -m pac_harness --root C:\Biology\Harness-Scenes\inventory-check --task '核对库存'
```

第二个命令以另一个场景已按相同步骤建好为前提。推荐直接在各自场景目录启动，确保用的是该场景固定的核心代码版本；从 Core 目录通过 `--root` 启动会使用当前进程导入的 Core 版本，而不自动切换为目标目录的核心副本。

使用不同根目录时，知识、任务状态及审计不会由框架自动互相加载。但它们仍可能使用同一 Python 环境、Codex 配置、账号或外部业务资源。按需为场景建立虚拟环境、专用账号和服务端资源锁；不要把 `--root` 当作操作系统沙箱。

仅替换 `--task` 或在同一根目录中换一个配置文件，不构成场景知识隔离。当前恢复检查也没有记录 Git commit、全部提示词哈希或依赖版本，版本可追溯性还需补充。

## 8. 用 Git 保存场景演进

建议 Core 和各场景分别建仓库。先保持每个场景的源码副本独立，记录引入的 Core 版本；以后再迁移为固定版本的共享依赖。Git 不要求上传到 GitHub，完全可以只保留本地仓库。

应纳入版本：适配器、测试、`SCENE.md`、不含凭据的配置模板、角色提示词、记忆和技能。运行日志、大型业务数据、密钥、虚拟环境及本地连接配置应按项目加入 `.gitignore`。Git 版本回退只改变文件，不会撤销已经发生的外部操作，也不等于恢复任务检查点。

初次初始化可在安装 Git 后执行：

```powershell
git init
git status --short
```

确认文件范围后再暂存和提交。当前 ToUser 保存差异和备份，但不会自动提交 Git、建分支或执行 Git 回滚；这些属于后续可集成的版本管理能力。
