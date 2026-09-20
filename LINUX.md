# Linux 使用与验证

环境安装步骤见 [README 环境准备](README.md#环境准备先完成这一步)。Linux 与 Windows 共用同一套源码，无需独立版本。要求 Python 3.11+。核心、ToUser Web 工作台、记忆/Skill、任务恢复和文件锁共用跨平台代码。硬件 SDK 映射仍需按各厂商与设备版本验收。

```bash
cd /path/to/PAC-Harness
python3 -m unittest discover -s tests -v
python3 -m pac_harness --demo
python3 -m pac_harness --assist
python3 -m pac_harness --web
```

## Ctrl+G 与终端

Linux 前台交互 TTY（包括 SSH 终端）支持 Ctrl+G 请求协助，在动作边界进入 ToUser；等待输入时再次按 Ctrl+G 返回任务，无需回车。支持 UTF-8 中文、退格、Ctrl+D 结束输入和 Ctrl+C 中断。终端暂时关闭行缓冲与回显，保留信号处理；暂停、正常退出和异常路径恢复设置。SIGKILL 无法执行清理，必要时运行 `stty sane` 恢复终端。

重定向输入或无 TTY 时不启用热键，使用 `--assist` 或网页。单独 --assist 结束后返回 shell；它不启动原任务。先停止正在运行的任务释放项目锁，再单独启动协助；不能在另一终端同时修改运行中的项目。

## 验证沙箱

安装兼容的 Codex CLI 并完成登录。Linux 离线验证还需要 bubblewrap（Debian/Ubuntu：`sudo apt install bubblewrap`），以及操作系统允许创建相应命名空间。

测试进程使用只读根文件系统、可写暂存目录、独立 /tmp、PID 与网络命名空间。禁止直接访问主机网络；独立命名空间内的本机 HTTP 测试仍可运行。沙箱允许读取主机文件，不是机密数据读取隔离。缺少 bwrap 或系统拒绝命名空间时验证失败，不会降级为裸跑测试或应用修改。

## 验收范围

GitHub Actions 配置覆盖 Ubuntu/Windows 与 Python 3.11/3.13；POSIX PTY 测试检查热键进入、退出、中文输入、暂停与异常恢复。Windows 会跳过该 PTY 测试。真实 Codex 模型请求与实机运动必须单独验收，不能用模拟 SDK 或跨平台单元测试代替。
