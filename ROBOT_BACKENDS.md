# 通用机械臂后端

PAC-Harness 现在提供统一的基础运动接口，适配器只依赖 `RobotBackend`，不把某一家 SDK 的对象暴露给 Planner 或 Detector。

```python
from pac_harness import FrankaFR3Backend, RobotLimits

limits = RobotLimits(tuple([-3.0] * 7), tuple([3.0] * 7), max_joint_speed=1.0)
backend = FrankaFR3Backend(vendor_driver, limits)
backend.connect()
backend.enable()
state = backend.state()
backend.move_joints([0.0] * 7, speed=0.2)
backend.stop()
backend.close()
```

## 已提供的后端

| 后端 | SDK/连接 | 自由度 | 当前范围 |
|---|---|---:|---|
| `RokaeBackend` | ROKAE SDK，由场景注入 driver | 6 | 统一基础接口 |
| `PiperBackend` | AgileX `piper_sdk`，通常为 CAN | 6 | 统一基础接口 |
| `FrankaFR3Backend` | `libfranka` / `pylibfranka`，FCI | 7 | 统一基础接口 |
| `SimRobotBackend` | 内置模拟器 | 6 或自定义 | 离线测试 |

SDK 不作为 Core 的强制依赖。应用层负责创建 vendor driver，再传给对应后端；这样没有硬件或 SDK 时仍可运行全部离线测试。

## 安全边界

- `connect()` 后仍必须显式 `enable()` 才能运动。
- 关节位置、运动速度和位姿维度在发送前校验；越界请求不会到达 vendor driver。
- `stop()` 只负责调用驱动停止接口，不能替代硬件急停或安全回路。
- 软件限位必须按具体机械臂、工具和工作空间配置，示例限位不能直接用于生产。
- 首次接入每台实机应先只读连接和状态，再低速单轴运动，最后才接入任务流程。

## SDK 映射说明

后端采用少量常见方法名的 duck typing（如 `connect`、`move_joints`、`move_pose`）。不同 SDK 版本的对象方法不一致时，在场景侧写一层很薄的 driver wrapper，把 SDK 的单位、返回值和错误映射成这些方法；不要在通用后端中硬编码设备地址或凭据。

目前后端只提供基础运动、状态和夹爪入口；坐标标定、碰撞策略、轨迹规划、示教回放和具体抓取动作仍属于场景适配器，需要分别验证。


## joint_move：相对增量与部分绝对目标

新增 `pac_harness.joint_motion:create` 适配器，动作由 Planner 提出、运行时执行、Detector 后检。关节数取自后端：六轴支持 J1～J6，七轴支持 J1～J7。参数必须明确角度单位。

仅将 J6 增加 90 度：

```json
{"name": "joint_move", "arguments": {"mode": "relative", "unit": "deg", "joints": {"J6": 90}}}
```

仅将 J6 回到绝对 0 度：

```json
{"name": "joint_move", "arguments": {"mode": "absolute", "unit": "deg", "joints": {"J6": 0}}}
```

也可以同时指定多个关节，或使用 `unit: "rad"`。绝对 0 度不是“最初角度”：要回到原角度，需要明确提供那次记录的 J6 值。未指定的关节始终采用执行前的当前实测值，不恢复之前保存的整组关节角；因此不会因为恢复旧 J1～J5 而撤销中间的 XY 调整。**关节运动本身不保证 TCP 的 XYZ 不变**，这还取决于机械臂和工具几何。

终端输出 `start / target / actual (deg)`；运行回执保存原始弧度、最大误差。发送异常或无法确认到位时返回 `execution_unknown`，不自动重发。`joint-receipts/` 位于当前运行目录，重启后也不会重复执行相同请求。仅明确完成的回执可恢复后检；未知回执保持未知，不能通过“现在恰好在目标位置”推断原指令成功。

### 离线模拟配置

保留自己的模型配置，只替换 `adapter`：

```json
{
  "adapter": {
    "factory": "pac_harness.joint_motion:create",
    "settings": {
      "motion_enabled": true,
      "robot": {
        "factory": "pac_harness.joint_motion:simulated_robot",
        "settings": {"dof": 6}
      }
    }
  }
}
```

```bash
python -m pac_harness --config config.local.json --observe-only
python -m pac_harness --config config.local.json --task "仅将 J6 相对增加 10 度，核对实测关节角后结束"
```

第二条命令使用配置的真实模型，但上述配置的机器人是模拟器。`--demo` 仍专用于列表排序示例。

### 已有 ROKAE 接收器的真实执行链

新增 `pac_harness.rokae_bridge:create`，直接使用现有桥接服务的 `POST /joints`、`GET /health` 和观察服务的 `POST /call`，无需导入 Harness-GPT。将上例 `robot.factory` 改为该工厂，并把 `robot.settings` 设置为以下字段：

| 字段 | 含义 |
|---|---|
| `action_url` | 已启动的 ROKAE 动作接收器地址 |
| `observation_url` | 已启动的观察 RPC 服务地址 |
| `joint_min_rad` / `joint_max_rad` | 实机六个关节的软件上下限，每项为弧度；必须按设备填写 |

观察服务使用 MessagePack 时需 `python -m pip install msgpack`。先设 `motion_enabled: false`，使用 `--observe-only` 检查反馈；完成现场准备后再显式设为 `true`。服务必须是支持关节命令的当前版本，并由现场操作员启动、使能及 ARM；本客户端不会自动启动服务、上电、ARM 或解除停止。

当前支持由本机启动器监管、`heartbeat_required: false` 的接收器；不接管其他控制器的心跳生命周期。若接收器要求心跳或 FK 工作空间校验，客户端拒绝运动，需先完善相应集成，不要为了运行示例而关闭既有保护。

发送前核对当前关节，拒绝与计算目标时的起始值不一致的请求。执行后检查接收器实例、请求编号、发送标识、计数增量、完成回执和实测角度。误差使用原始关节角，不将相差 360 度视为已经到位。速度和运动超时由接收器配置控制。

Piper/FR3 仍需实际厂商驱动映射；通用 `DriverBackend` 只有在驱动明确返回 `ok: true`、`execution_outcome: "completed"` 且实测到位时，才可供这个关节适配器认定完成。旧的普通 SDK 返回值不能充当完成回执。

该适配器只提供关节运动和机器人状态。已有相机/XY/夹爪业务适配器应组合其动作和观察，不要直接替换后丢掉原功能。真实执行链已通过假的接收器与模拟器离线验证，尚未连接实机验收。


## 标定定位与笛卡尔移动

新增通用 RGB-D 标定计算和笛卡尔动作适配器，提供 `move_calibrated`、`move_relative`。ROKAE HTTP 后端通过已有接收器 `/action` 发送单个受限平移，并核对回执及实际位姿。相机配准、TCP 参考点、配置和离线示例见 [CALIBRATED_MOTION.md](CALIBRATED_MOTION.md)。厂商 `move_pose()` 的普通返回值不能替代完成回执；自定义驱动需报告真实完成状态。
