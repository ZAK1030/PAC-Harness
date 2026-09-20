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
