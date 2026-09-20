# 场景适配器契约

核心只依赖以下接口。实现示例见 [examples/list_sorting.py](examples/list_sorting.py)。场景代码负责连接外部环境；工厂函数仅构造适配器，不应在构造时产生业务副作用。

| 方法 | 返回值及职责 |
|---|---|
| `observe()` | 返回可 JSON 序列化的当前观察字典；不改变业务状态 |
| `actions()` | 返回动作目录，每项含 `name`、`description`、`parameters` |
| `validate(action, observation)` | 根据最新观察校验动作，不产生业务副作用；不通过抛 `ValueError` |
| `execute(action, request_id)` | 执行一次动作，返回至少含布尔 `ok` 的字典 |
| `reconcile(request_id)` | 只读查询不明结果，不能通过重发动作来查询 |
| `tools()` | 返回按需的只读 `Tool` 列表；没有时返回 `[]` |
| `close()` | 释放本实例的资源 |

适配器名称和配置来自本地受信任配置，不接受模型提供的模块路径。适配器回调是普通 Python，核心不会在操作系统层证明这些回调没有副作用；接入者必须遵守只读工具契约。

## 动作与参数

```json
{
  "name": "update_record",
  "description": "Update one configured record",
  "parameters": {
    "type": "object",
    "properties": {"record_id": {"type": "string"}},
    "required": ["record_id"],
    "additionalProperties": false
  }
}
```

模型输出 `{"name":"update_record","arguments":{"record_id":"123"}}`。`done` 保留给核心，不能由适配器重定义。参数校验支持 JSON Schema 的小子集：`type`、`properties`、`required`、`additionalProperties: false`、`items`、`enum`、`minimum`、`maximum`；不宣称支持完整 JSON Schema。复杂业务约束必须在 `validate` 检查。

核心在模型规划后重新观察，再调用 `validate`。依赖特定快照的动作应携带版本或对象标识，由适配器拒绝过期计划；执行端也应检查版本，防止观察与执行之间的竞争。

## 执行结果与恢复

```json
{"ok": true, "result": {"record_id": "123"}}
```

`ok: false` 且结果已知时，仍交给 Detector 后检并反馈 Planner。已知失败的具体含义由适配器解释。无法确定动作是否完成时，必须返回 `{"ok":false,"outcome_unknown":true}` 或抛出异常，不能伪装为“确定未执行”。

恢复查询有三种返回：

```json
{"status":"completed","result":{"ok":true}}
```

```json
{"status":"not_executed"}
```

```json
{"status":"unknown"}
```

`completed` 的结果明确，转入原动作的后检；`not_executed` 必须能证明未产生副作用，才允许回到规划；`unknown` 保持暂停。不要因为查询暂时失败或当前缓存缺失就返回 `not_executed`。

内置列表适配器将状态变化与操作回执一起原子保存；真实外部系统需要在对应系统内处理幂等和事务边界。运行目录锁只防止同一运行目录的两个控制器，不是外部系统的全局锁。

## 观察与媒体

普通结构化观察可以是任何业务字典。可选图像或文本证据示例：

```json
{
  "revision": 12,
  "data": {"stage": "processing"},
  "artifacts": [
    {"path":"logs/current/screenshot.png","description":"Current UI"},
    {"path":"logs/current/result.json","description":"Current result"}
  ]
}
```

文件路径以配置项目根目录为基准，使用 `/`。`inspect_artifact` 只读取当前观察或当前动作前后观察声明的文件，拒绝路径穿越、链接、未声明文件及超限内容。图像上限 8 MB；文本上限 100 KB。图片需要所选模型支持图像输入。其他数据格式用自定义只读工具返回针对性结果。

证据宜按观察保存为不可变文件；覆盖同一路径会破坏历史可追溯性。提供方须移除不应交给模型的敏感业务信息，不能把路径校验当作完整的数据分类措施。

## 自定义信息工具

```python
from pac_harness import Tool
from pac_harness.tools import parameters

def tools(self):
    return [Tool(
        name="read_record",
        description="Read one record without modifying it",
        parameters=parameters({"record_id": {"type": "string"}}, ("record_id",)),
        handler=self.read_record,
        roles=("planner", "detector"),
    )]
```

handler 默认返回 JSON 数据。需要多模态输入时可返回 `ToolResult(data=摘要字典, content=Chat-Completions内容块列表)`；日志只保存摘要，内容块回送模型。工具执行不消耗动作轮数，但受 Agent 工具轮次预算限制。

## 新场景验收建议

为适配器加入离线测试，覆盖观察解析、动作参数与权限、已知失败、超时后结果查询和幂等性；再用可重复任务验证真实效果。确定性演示与协议测试只能证明框架链路，不证明新业务任务已经成功。
