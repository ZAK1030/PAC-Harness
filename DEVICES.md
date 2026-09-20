# 设备接入指南

配置模型只解决推理与对话，设备通过 `adapter.factory` 指定的场景适配器接入。请在当前项目目录运行下面的命令，Linux 可使用 `python3`。

## 1. 普通 RGB 摄像头

安装可选依赖（只在实际读取相机的环境安装）：

```bash
python -m pip install opencv-python
```

在项目 `config.json` 或 `config.local.json` 中，只替换 `adapter` 部分，保留已有 `models`、密钥文件引用和 ToUser 配置：

```json
{
  "adapter": {
    "factory": "examples.camera_observation:create",
    "settings": {
      "cameras": {
        "wrist": {
          "factory": "pac_harness.devices:opencv_camera",
          "settings": {"source": 0}
        },
        "overview": {
          "factory": "pac_harness.devices:opencv_camera",
          "settings": {"source": 1}
        }
      }
    }
  }
}
```

只接一台时删除另一项。增加相机时增加一个不同名称的条目，例如 `side`；名称不是设备编号。USB 编号可能因重新插拔而改变，先确认图像确实来自对应相机。Linux 可填写 `/dev/video0`；SDK 相机应使用下面的自定义工厂。

```bash
python -m pac_harness --config config.local.json --observe-only
```

此命令不调用模型或 `execute`；会实际打开相机，保存图片到 `logs/<run>/frames/`，输出 `artifacts` 路径，然后关闭相机。检查图像、方向、视野和曝光，再使用支持图像输入的模型：

```bash
python -m pac_harness --config config.local.json --task "检查各相机画面，描述桌面物体"
```

示例仅观察，没有机械臂动作。模型通过 `inspect_artifact` 按需读取实际图片；只在提示词写“有摄像头”不会产生视觉输入。图片最多 8 MB；多相机依次读取，不保证同步。`received_at_ns` 是接收时间，不是曝光时间；实时控制还需 SDK 时间戳、帧龄检查与同步策略。某些驱动读取可能阻塞，应在正式场景中配置厂商超时或独立采集进程。

相机打不开时检查系统权限、设备占用、驱动、编号；在 Windows 先关闭占用相机的程序，在 Linux 核对视频设备权限。普通 RGB 图不能代替深度数据或手眼标定。

实现依据：[OpenCV 官方视频采集说明](https://docs.opencv.org/4.x/dd/d43/tutorial_py_video_display.html)。

## 2. 新型号或深度相机

在 `adapters/my_camera.py` 实现工厂 `create(*, root, config)`，返回具备这两个方法的对象：

```python
class Camera:
    def read_png(self) -> bytes:
        # 调用厂商 SDK，检查帧有效性，将 RGB 帧编码成 PNG bytes。
        raise NotImplementedError("请实现该型号 SDK 的真实采集")

    def close(self):
        # 关闭流并释放设备。
        pass

def create(*, root, config):
    return Camera()
```

将该相机的 `factory` 改为 `adapters.my_camera:create`，`settings` 放序列号等非敏感连接参数。这个骨架必须实现后才能使用。深度、内参、外参、时间戳应由业务适配器提供结构化观察或只读工具，不能假定通用 RGB 组件会完成标定。

已有业务适配器不应改成只读示例：在其初始化中创建 `CameraSet(root=root, run_directory=run_directory, cameras=config['cameras'])`，在 `observe()` 中把 `self.cameras.observe()['artifacts']` 合并进原观察，在 `close()` 中调用 `self.cameras.close()`。保留现有动作与回执逻辑。

## 3. 机械臂

`RokaeBackend`、`PiperBackend`、`FrankaFR3Backend` 当前是通用接口外壳，**尚不是已验证的厂商 SDK 驱动**。不能只填写品牌或 IP 就控制设备。需要在 `adapters/my_robot.py` 实现可信设备工厂，做 SDK 单位、坐标系、错误码、动作完成回执和超时映射，再由业务适配器调用。

在业务 `adapter.settings` 内增加配置（只读相机示例不会使用该项）：

```json
"robot": {
  "factory": "adapters.my_robot:create",
  "settings": {"connection": "待填写", "motion_enabled": false}
}
```

业务适配器中使用 `load_device(config['robot'], root=root)`（来自 `pac_harness.devices`）加载该工厂。它只调用工厂；连接、读取、使能、执行和关闭均须由实现明确管理。`motion_enabled` 是工厂需要检查的配置约定，不是通用加载器自动实施的限制。工厂和 `observe()` 不得使能或运动；动作只能由 `execute()` 发送。不要将 SDK 返回函数视为动作完成，要查询真实结果；结果不明时 `reconcile()` 返回未知，避免重复发送。

交给 ToUser 时提供：品牌型号、SDK 版本与本地文档、连接方式、关节数和单位、位姿参考点/TCP、工具参数、软件限位、夹爪接口、停止与故障恢复规则。先验证只读状态和离线错误路径，再人工开展低速实机验收。现有通用后端的 `ok: True` 包装不能替代设备完成回执。

## 4. 在终端向 ToUser 说明需求

在当前项目目录配置好 ToUser 后，直接描述设备、任务目标、接口资料和成功条件：

```bash
python -m pac_harness --assist --message "桌面分拣需要手腕和俯视两台相机、一台机械臂。请先核对设备接口与缺失信息，再实现相机观察、机械臂适配器及离线测试；把确认知识写入 memories 和 skills。不要执行设备动作。"
```

`--message` 自动发送首条消息，后续继续终端对话；`--task` 仅提供背景。较长资料可以先写入项目中的 Markdown 文件，再要求 ToUser 阅读。资料不会自动注入 Planner/Detector；确认的长期知识需要保存到记忆和技能。ToUser 不负责实机验收，需要你提供 SDK 与接口资料。
