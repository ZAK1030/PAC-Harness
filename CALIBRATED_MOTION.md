# 标定目标定位与视觉相对纠偏

两种方法共用 Planner → 单动作 → Detector 后检。几何计算参考 Harness-GPT 的 `grounding.py` 与坐标转换实现；不复制现场相机标定、设备地址、目标物体规则或运动阶段规则。

## 安装和离线验证

在项目虚拟环境中执行（Windows / Linux 通用）：

```bash
python -m pip install -e ".[robot-vision]"
python -m examples.calibrated_motion
python -m unittest discover -s tests -v
```

示例只使用合成图像、已知深度和模拟机器人。依次演示标定接近、相对纠偏和独立后检；不调用模型或真实设备。生成的模拟标定文件写入本次 `logs/calibrated-demo-*/simulation-calibration/`，不覆盖用户文件。它验证执行链，不代表现场标定准确或实际运动成功。

## 模型如何选择动作

观察中包含 `observation_id`、相机名称、原图尺寸、机器人状态和图片 `artifacts`。Planner 可通过 `inspect_artifact` 查看原图，通过 `locate_calibrated_target` 只读工具核对几何计算，再输出动作。

标定接近示例：

```json
{
  "name": "move_calibrated",
  "arguments": {
    "observation_id": "当前观察标识",
    "target": "待操作物体",
    "view": "overview",
    "pixel_uv": [320, 240],
    "approach_offset_m": [0, 0, 0.05]
  }
}
```

`pixel_uv` 使用原始 RGB 像素：左上角为原点，u 向右，v 向下。模型只选择像素和接近偏移；目标三维坐标由实测深度和标定计算。偏移是机器人基座坐标系下的米制向量，受 `max_approach_offset_m` 限制；不能通过添加 `target_xyz_m` 等字段绕过计算。

相对纠偏示例：

```json
{
  "name": "move_relative",
  "arguments": {
    "observation_id": "当前观察标识",
    "target": "待操作物体",
    "delta_m": [0.001, 0, 0]
  }
}
```

`delta_m` 为基座坐标系中的相对位移，受 `fine_step_m` 限制。它不会自动把图像右方换算成基座 +X；方向需要结合当前相机方向和业务知识判断。每个动作都保持姿态不变，仅执行一次平移，然后交给 Detector 后检。

标定接近以 TCP 到物体点的距离判断是否进入精调范围，排除接近偏移。接近向量按 `coarse_step_m` 限制，必要时停在物体的精调半径边界；偏置路径没有进入该半径时，不会因距偏置终点较近而提前停止。进入范围后，粗接近动作返回规划反馈，由 Planner 选择合适的相对纠偏。

## 在当前项目配置真实服务

保留 `config.local.json` 中的模型与 ToUser 配置，将 `adapter` 设置为 `pac_harness.cartesian_motion:create`。其 `settings` 结构如下；所有设备参数和限位都要使用现场实际值。

```json
{
  "motion_enabled": false,
  "max_observation_age_s": 30,
  "arrival_tolerance_m": 0.002,
  "hardware": {
    "pose_reference": "receiver_reported",
    "orientation_unit": "radians",
    "tcp_offset_tool_m": [0, 0, 0.1765]
  },
  "safety": {
    "workspace_reference": "tcp",
    "xyz_min": [-0.5, -0.5, 0.1],
    "xyz_max": [0.5, 0.5, 0.8],
    "coarse_step_m": 0.05,
    "fine_step_m": 0.01,
    "fine_entry_distance_m": 0.05
  },
  "grounding": {
    "views": ["overview"],
    "factory_calibration_path": "calibration/factory.json",
    "extrinsics_path": "calibration/extrinsics.json",
    "sample_radius_px": 4,
    "min_depth_m": 0.05,
    "max_depth_m": 2.5,
    "max_depth_spread_m": 0.02,
    "max_approach_offset_m": 0.25
  },
  "robot": {
    "factory": "pac_harness.rokae_bridge:create",
    "settings": {
      "action_url": "http://127.0.0.1:18002",
      "observation_url": "http://127.0.0.1:18001",
      "joint_min_rad": [-3, -3, -3, -3, -3, -3],
      "joint_max_rad": [3, 3, 3, 3, 3, 3]
    }
  },
  "rgbd": {
    "factory": "pac_harness.rgbd_rpc:create",
    "settings": {
      "observation_url": "http://127.0.0.1:18001",
      "views": ["overview"],
      "max_camera_age_ms": 500
    }
  }
}
```

上述数值仅说明字段结构，不是你的实机限位、端口或 TCP 标定。`views` 必须与服务返回和两个标定文件一致；原服务使用 `scene/detail/wrist` 时填写对应名称即可。先运行：

```bash
python -m pac_harness --config config.local.json --observe-only
```

核对设备、图像、单位与标定后再显式打开 `motion_enabled` 并启动模型任务。ROS/厂商 SDK 相机可提供自己的工厂；对象需实现 `capture(robot_state) -> pac_harness.rgbd.Observation` 与 `close()`。RGB 为原生 uint8 RGB，深度为未配准浮点米制数组；提供稳定唯一标识、机器人状态、服务器时间戳和本地单调采集时间。这个工厂不执行机器人动作。

现有 RPC 实现读取 `hardware.read_observation` 的 JPEG、zlib uint16 深度、`depth_scale_mm`、原始尺寸、相机时间戳、帧龄和机器人状态。显式按比例转换为米；冻结帧、过期帧和缺少时间戳会被拒绝。旧的不带这些元数据的服务需要升级或提供自定义采集工厂。

## 标定文件格式

`factory.json` 的 `cameras.<名称>` 包含：

- `rgb_intrinsics` 和 `depth_intrinsics`：各自 `width`、`height`、`K_row_major_3x3`。
- `distortion`：内参项中可提供八项 `[k1,k2,k3,k4,k5,k6,p1,p2]`，同时声明 `distortion_order` 和支持的 `distortion_model_id`（3 或 4）；无畸变可省略。
- `depth_to_rgb_extrinsic`：`direction: "T_rgb_camera_from_depth_camera"` 和米制 `T_row_major_4x4`。
- `mirrored: false`、`depth_aligned_to_rgb: false`，明确描述未经镜像、尚未配准的原始数据。

`extrinsics.json` 的 `cameras.<名称>` 包含 `calibrated: true` 及以下两者之一：

- 固定相机：`T_robot_base_from_camera`。
- 随机械臂运动的相机：`T_tool_from_camera`，此处 tool 是配置 TCP；计算时结合当前 TCP 位姿。

矩阵均为 4×4 行优先刚体变换，旋转必须正交且行列式为 +1，平移为米。工具仅用 XYZ 偏置，不支持 TCP 相对法兰存在额外旋转的配置。位姿姿态使用弧度和 `Rz(yaw) @ Ry(pitch) @ Rx(roll)`。

既有 Harness-GPT 标定文件采用此结构时可直接作为输入，但应复制自己确认过的文件并核对相机名称、TCP 与流分辨率。不会自动读取邻接项目的现场文件。图片裁剪、镜像、旋转或未经确认的缩放会被拒绝；深度缩放必须声明最近邻插值。不要把已经配准的深度再次作为原始深度输入。

## 执行和恢复边界

ROKAE 后端沿用现有接收器的 `/action` MessagePack 协议，8×6 数据块只有首行包含平移，其余为零；不会发送多步轨迹。发送前检查接收器状态、模式/步长限制、坐标参考点和当前位姿；完成后核对接收器实例、请求编号、计数、完成回执、目标及实测位姿。速度和超时服从接收器配置。

服务由现场操作员启动、上电并 ARM；客户端不启动设备、不自动使能或解除停止。当前支持本机启动器监管且无需心跳的模式，不接管其他控制器的心跳。其他机器人后端可实现 `move_cartesian_checked(candidate)`，或提供返回真实完成回执的 `move_pose()`；普通 SDK 返回不代表已到位。

每个动作在运行目录写入 `cartesian-receipts/`，包含原图标识、标定文件哈希、深度采样质量、几何目标、实际步长、实测结果与工作空间检查。原地更新的标定文件重新读取；失败或未知结果不会自动重发，恢复保持未知。进程重启丢失内存图像快照时必须重新观察和规划，不能沿用无法校验的像素目标。

定位结果只代表所选像素的几何位置，不证明语义对象身份、夹爪间隙或无碰撞。实际设备接入仍需现场验收；该分支未连接真实模型或执行实机运动。
