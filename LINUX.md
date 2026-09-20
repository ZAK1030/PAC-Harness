# Linux 支持

## 已支持

- Python 3.11+ 下运行 Harness、列表演示、任务恢复和文件锁。
- ToUser 的 `codex exec` 对话、结构化 JSON 响应、隔离暂存目录、差异审计和离线测试。
- 本地 Web 工作台：`python -m pac_harness --web`。
- Piper/FR3 通用后端的 Linux Python driver 注入方式；厂商 SDK 仍由场景自行安装。

```bash
cd /path/to/PAC-Harness
python3 -m pac_harness --demo
python3 -m unittest discover -s tests -v
python3 -m pac_harness --assist --task '分析当前项目的问题'
python3 -m pac_harness --web --port 8765
```

## ToUser 验证差异

Linux 没有使用 Windows 专用的 Codex sandbox profile。验证器只执行固定的 Python `unittest` 命令，工作目录是 ToUser 的隔离暂存目录，网络和业务入口不会由该命令主动开启。生产 Linux 主机仍应使用独立虚拟环境、容器或系统级沙箱来限制不可信依赖。

## 终端协助

Linux 当前不安装全局键盘钩子，也不会把终端输入切换为 raw 模式，避免 ToUser 后台监听吞掉普通命令行输入。因此运行中的任务不能用 Ctrl+G 自动打断；可在另一个终端执行 `--assist --run-dir logs/<run>`，或打开 Web 工作台。这样是明确的降级行为，不影响任务恢复和项目锁。

## 厂商 SDK

Piper 的 CAN 设备、FR3 的 FCI/libfranka 以及 ROKAE 驱动需要按厂商文档安装。Core 不会自动安装、扫描或连接设备；先执行只读状态检查，再在低速和软件限位下做单轴验收。
