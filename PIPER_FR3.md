# Piper / Franka Research 3：标定定位与运动接入

两款机械臂已提供实际 SDK 适配实现，接入同一个 `pac_harness.cartesian_motion:create`，共用 `move_calibrated`、`move_relative`、工作空间检查、单动作回执和 Detector 后检。不是只声明一个厂商类；Piper 实际调用 `MotionCtrl_2` / `EndPoseCtrl`，FR3 实际构造 `CartesianMotion` 并等待 `join_motion`。

当前验证范围是模拟厂商 SDK 的离线测试，**尚未在 Piper 或 FR3 实机验收**。原来的 `PiperBackend` / `FrankaFR3Backend` 保留为自定义驱动包装器；下面使用新增的 `PiperSDKBackend` / `FrankaFR3SDKBackend`。

## 1. 安装与连接平台

PAC-Harness、几何计算和离线测试共用 Windows/Linux 源码；这两个内置实机连接入口运行在 Linux。Windows 上运行 ToUser 或离线测试不需要厂商 SDK，不能将测试通过理解为 Windows 原生实机驱动可用。

Piper 使用 `piper-sdk==0.6.2` 的 `C_PiperInterface_V2`，CAN 接口需已按厂商文档配置：

```bash
python -m pip install -e ".[robot-vision,piper]"
```

FR3 使用 `franky-control` 2.x（import 名称为 `franky`），由 libfranka 执行实时轨迹。需准备 Linux 实时内核、实时调度权限、FCI 网络和机器人 Desk 状态；后端保持 `RealtimeConfig.Enforce`，不会关闭实时检查。

```bash
python -m pip install -e ".[robot-vision,franka]"
```

FR3 的 libfranka/wheel 必须与现场 Robot Server 版本兼容。安装默认 wheel 不是兼容性证明；选择方法见 [franky 安装说明](https://timschneider42.github.io/franky/getting-started/installation.html)。Piper SDK API 和 CAN 准备见 [AgileX SDK](https://github.com/agilexrobotics/piper_sdk)。

## 2. 替换机器人配置

保留 [CALIBRATED_MOTION.md](CALIBRATED_MOTION.md) 中的 `adapter.factory`、`grounding`、`hardware`、`safety` 和自己的 RGB-D 采集工厂，将 `adapter.settings.robot` 换成下面之一。示例地址、限位、碰撞阈值仅展示字段，必须替换为设备实际值。

### Piper

```json
{
  "factory": "pac_harness.piper_backend:create",
  "settings": {
    "can_name": "can0",
    "command_enabled": false,
    "pose_reference": "flange",
    "joint_min_rad": [-3, -3, -3, -3, -3, -3],
    "joint_max_rad": [3, 3, 3, 3, 3, 3],
    "xyz_min_m": [-0.5, -0.5, 0.1],
    "xyz_max_m": [0.5, 0.5, 0.8],
    "speed_percent": 10,
    "max_cartesian_step_m": 0.05,
    "max_feedback_age_s": 0.5,
    "move_timeout_s": 15,
    "arrival_tolerance_m": 0.001,
    "orientation_tolerance_rad": 0.001,
    "settle_time_s": 0.2
  }
}
```

Piper 的 `pose_reference` **必须对应现场 SDK 反馈/指令实际代表的点**，明确填写 `flange` 或 `tcp`，不能因为字段叫 end_pose 就假定是工具尖端。`hardware.pose_reference` 建议设为 `receiver_reported`，RGB-D `Observation.state` 同样声明该参考点；TCP/法兰转换按真实 `tcp_offset_tool_m` 执行。

上层使用米和弧度；后端将 XYZ 转为 SDK 的 0.001 mm 整数，将 Euler/关节角转为 0.001° 整数。笛卡尔移动使用 **MOVE L**，保持当前姿态；不会用 MOVE P 的关节路径替代线性接近。速度使用厂商百分比，不把 m/s 猜成百分比。

`connect()` 使用 `ConnectPort(piper_init=False)`，不调用上电、使能、主从配置、恢复或夹爪动作。机器人使能由现场准备，或由应用明确调用 `backend.enable()`；后者只发送一次 `EnableArm(7)` 再等待反馈，连接/工厂不会调用它。

必须由一个控制程序独占运动指令。Piper CAN 没有 ROKAE HTTP 那样的请求 ID；完成依据是更新的状态/关节/位姿/驱动反馈、CAN/MOVE L 模式、到位状态、实测误差及持续稳定时间。小于到位容差的命令也要求观测到实际位移，不能借用上次的到位状态。时间戳是 SDK 聚合反馈时间，不是每个 CAN 分片的独立同步证明；现场需要验证完整反馈链。超时/故障后尝试发送快速停止，保持结果未知，绝不自动发送恢复。

### Franka Research 3

```json
{
  "factory": "pac_harness.franka_backend:create",
  "settings": {
    "hostname": "192.168.0.2",
    "command_enabled": false,
    "pose_reference": "tcp",
    "joint_min_rad": [-3, -3, -3, -3, -3, -3, -3],
    "joint_max_rad": [3, 3, 3, 3, 3, 3, 3],
    "xyz_min_m": [-0.5, -0.5, 0.1],
    "xyz_max_m": [0.5, 0.5, 0.8],
    "relative_dynamics_factor": 0.05,
    "collision_torque_threshold_nm": 10,
    "collision_force_threshold_n": 15,
    "max_cartesian_step_m": 0.05,
    "move_timeout_s": 15,
    "arrival_tolerance_m": 0.001,
    "orientation_tolerance_rad": 0.001,
    "settle_time_s": 0.2
  }
}
```

读取**实测** `O_T_EE` 和七个 `q`，不使用期望位姿 `O_T_EE_d`。列优先 4×4 矩阵转换为基座米制 XYZ / extrinsic xyz 弧度；绝对目标通过 quaternion 转换传入 `Affine` / `CartesianMotion`，参考系明确为机器人基座。`O_T_EE` 是机器人配置的末端坐标系，需与现场 TCP、手眼标定和工具配置一致；此后端固定报告 `tcp`。

一条动作只创建一个 motion，异步开始后用 `join_motion(timeout=...)` 等待并传播控制异常，再检查机器人 Idle、实际位置和递增的机器人时间。超时或异常尝试 `stop()` 并有界等待，不重新提交 motion。动力学比例只在机器人全局设置一次，避免全局与 motion 上重复缩放。该比例不是绝对 m/s 速度值。

**franky 构造 Robot 时会安装碰撞阈值。** 因此配置必须明确提供现场认可的力矩/力阈值，不能宣称连接完全不写控制器参数；本后端不恢复错误、不解除停止、不启动运动，也不修改 EE 工具变换。`backend.enable()` 仅开放软件命令权限，不能替代 Desk/FCI/硬件准备。

## 3. 相机、标定和两个运动开关

RGB-D 工厂依然实现 `capture(robot_state) -> Observation` 与 `close()`，返回新相机图像、原始米制深度、真实采集时间、对应当前机械臂的实测位姿及参考点。详见 [标定输入协议](CALIBRATED_MOTION.md#在当前项目配置真实服务)。

已有 `rgbd_rpc:create` 可以继续使用，**前提是服务返回同一台 Piper/FR3 的实测 robot_state**。原服务若仍读取 ROKAE，必须修改观察服务或使用自己的 RGB-D 工厂；不能直接把 ROKAE 位姿改标签后传给新机械臂。相机到基座/工具的外参要针对实际安装重新确认，不能复用另一台机器人的基座标定。抓图期间机械臂应保持静止，随臂相机尤其需要采集时刻对应的位姿。

机器人 `xyz_min_m/max` 在 `robot.settings.pose_reference` 下定义；上层 `safety.xyz_min/max` 在 `safety.workspace_reference` 下定义。两层都检查实际起点和目标，参考点不同时按 TCP 偏置理解，不要直接复制同一组数值。SDK 层限制单次平移 `max_cartesian_step_m`，上层另区分粗步长/精调步长。

先保持上层 `adapter.settings.motion_enabled: false` 和机器人 `command_enabled: false`：

```bash
python -m pac_harness --config config.local.json --observe-only
```

验证现场状态、单位、相机标定、参考点和停止后，将两处显式设置为 `true`，再运行现有任务入口：

```bash
python -m pac_harness --config config.local.json --task "根据当前 RGB-D 像素标定接近目标，每次移动后核验实测位置，进入精调范围后使用相对纠偏"
```

这些后端也可作为 `pac_harness.joint_motion:create` 的 robot 工厂：Piper 六轴、FR3 七轴，部分关节目标保留其他当前角度。提供的软件关节限位检查目标及读取到的反馈，不提供 FK 轨迹/碰撞检查；实际控制器与现场保护仍需独立验证。夹爪不在这次接入范围内，调用返回明确的未实现错误。

## 4. 回执、恢复和离线验证

成功回执保存后端名称、当地生成的 command_id、实际位姿/关节和完成依据；该 command_id 是软件记录标识，不冒充厂商确认的请求编号。上层 `cartesian-receipts/` / `joint-receipts/` 在调用 SDK 前落盘，相同请求重启后不重发。SDK 实例遇到发送、等待或反馈异常会锁定未知状态并关闭命令权限；重新 enable 不会清除未知状态，需人工检查后创建新连接/运行。

```bash
python -m unittest discover -s tests -p test_native_robot_motion.py -v
python -m unittest discover -s tests -v
```

测试使用与厂商 API 形状一致的替身，覆盖单位转换、基座绝对目标、七轴关节、实测/期望位姿区别、不到位、冻结反馈、超时停止、重复请求和两款后端的标定→纠偏完整链。它不验证 CAN/FCI 通信质量、机械精度、现场碰撞或真实 SDK 二进制兼容性。

接口依据：[Piper V2 实现](https://github.com/agilexrobotics/piper_sdk/blob/master/piper_sdk/interface/piper_interface_v2.py)、[franky 运动接口](https://timschneider42.github.io/franky/guide/executing-motions.html)、[franky 几何定义](https://timschneider42.github.io/franky/guide/geometry.html)。
