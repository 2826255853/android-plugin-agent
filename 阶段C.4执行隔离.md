# 阶段 C.4 执行隔离

电脑端为每次构建、测试或浏览器预览创建一台独立的 QEMU 虚拟机。可信协调器通过 `scripts/execution_isolation.py` 提交一个入口和本轮显式指定的文件，接收有大小上限的运行结果。入口包括 `build`（`build.sh`）、`test`（`test.sh`）和 `preview`（`index.html`）。脚本由虚拟机内的 BusyBox Shell 执行，镜像包含 Python、Node.js 和 Chromium。预览在虚拟机内启动无头 Chromium 并返回渲染后的 DOM，供 C.5 接入 ZIP 预览和交互检查。

## 隔离配置

| 项目 | 配置 |
| --- | --- |
| 宿主 | Windows x64、QEMU x86_64 TCG。每轮启动新的 VM，Windows Job 限制启动器和 QEMU 两个进程、3 GiB 提交内存及 25% 的宿主 CPU 时间。Job 建立、资源限制和进程加入均成功后才启动 QEMU。 |
| 输入与文件系统 | 本轮输入最多 64 个文件、共 8 MiB，经 initramfs 注入只读 `/input`。以 UID/GID 65534 运行，经 chroot 使用只读工具链和专用 `/work`。临时目录、浏览器配置和捕获日志共用 64 MiB、4096 inode 的 tmpfs。 |
| 网络与设备 | QEMU 不配置网卡、宿主目录共享、磁盘、USB 或设备透传。客体启动时检查只有 `lo` 并启用客体内部 loopback。进程只可访问 `/dev/null`、`zero`、`random`、`urandom` 和指向本轮 tmpfs 的 `shm`。启动器与客体均使用显式设置的环境变量。 |
| 客体资源 | 单 vCPU、2 GiB 虚拟机内存。cgroup v2 将工作负载 CPU 速率限制为单核的 75%，内存 768 MiB，进程及线程合计 128，并关闭工作负载 swap。构建和测试默认累计 CPU 上限 20 秒、墙钟上限 90 秒。Chromium 冷启动使用累计 CPU 60 秒、墙钟 120 秒。墙钟从准备本轮镜像副本开始计时。 |
| 终止与清理 | 客体每 50 ms 检查资源计数，必要时终止整个 cgroup，成功返回前确认 cgroup 清空。超时、取消和正常退出后，协调器终止 Windows Job 并确认无活动进程，再删除本轮临时目录。仅返回最多 8 KiB 的捕获输出。 |

协调器在启动前校验工具链镜像和内核的 SHA-256。宿主 Job 无法设限、镜像不匹配或客体隔离初始化失败时，入口返回 `ISOLATION_UNAVAILABLE`。CPU、内存、磁盘和进程数量分别有独立错误码；超时与取消分别返回 `ISOLATION_TIMEOUT` 和 `ISOLATION_CANCELLED`。Chromium 的进程沙箱由 VM 边界承载，浏览器进程使用与构建和测试相同的受限客体身份。

## 准备与复现

1. 在目标机准备 Windows x64 发行包，校验发行者提供的 SHA-512 后安装或解包到专用目录。通过绝对路径指定 `qemu-system-x86_64.exe`。[QEMU 下载](https://www.qemu.org/download/)
2. 在可信的 Alpine 3.23 x86_64 环境（可用 WSL 2）以 root 安装 GNU cpio，运行 `sh scripts/prepare-stage-c4.sh <新镜像目录>`。脚本从 Alpine 仓库安装经签名校验的软件包，生成 `vmlinuz`、`rootfs.cpio.gz` 和 `image.json`。准备时可通过 `ALPINE_C4_MIRROR` 指定包含 `main` 和 `community` 的 Alpine 3.23 镜像目录。将输出目录放在协调器可读取的可信位置，并保留生成的摘要清单。
3. 在 Windows 的仓库根目录执行验收。`$qemu` 和 `$image` 分别为 QEMU 可执行文件和镜像目录的绝对路径。

```powershell
python scripts/verify-stage-c4.py --qemu $qemu --image $image
```

单次运行使用 `python scripts/execution_isolation.py --qemu $qemu --image $image test --input test.sh <本机固定脚本路径>`。如果需要传入其他文件，可重复 `--input`，每组给出客体相对路径与明确选定的本机文件。C.5 的协调器可直接使用 `run_isolated`，传入文件字节和用于取消的 `threading.Event`。

验收使用固定程序覆盖本轮读写、跨轮无状态、宿主凭据与设备不可访问、IPv4/IPv6 外部路由拒绝、客体 loopback、Chromium JavaScript 渲染、四类资源限制、超时、取消和配置失效。每次实际启动都检查 Windows Job 的活动进程数为零、本轮临时目录已删除。正常及资源终止结果还检查客体 cgroup 已清空。

## 目标机验收

2026-11-26 在 Windows 11 家庭版 x64（构建 26300）运行。宿主使用 Python 3.15.0、QEMU 11.1.0 和 TCG。客体使用 Alpine 3.23、Linux `6.18.65-0-virt`、Python 3.12.16、Node.js 24.22.3 及 Chromium 149.0.7827.53。本机运行时保存在 `$env:LOCALAPPDATA/AndroidPluginAgent-C4` 的 `qemu-runtime` 与 `image` 目录。

| 场景 | 实测结果 |
| --- | --- |
| 构建与跨轮隔离 | 读取指定输入，在 `/work` 写入并读回文件，Node.js 正常退出。写入输入和系统目录被拒。下一轮读不到前一轮的输入与工作文件，合成的宿主凭据文件、环境变量和设备接口在客体内不可见。 |
| 网络与浏览器 | 外部 IPv4、IPv6 和 QEMU 宿主别名连接均因无路由失败，客体内部 TCP loopback 可用。Chromium 执行独立 JavaScript 资源，输出包含 `fixture rendered 42` 的 DOM。 |
| 资源限额 | 分别用忙循环、超限内存分配、填满 tmpfs 和派生进程触发 CPU、内存、磁盘及进程数上限，各自返回相应的 `ISOLATION_*_LIMIT`。 |
| 超时与取消 | 固定脚本在 VM 中启动后台子进程。超时和用户取消分别返回 `ISOLATION_TIMEOUT` 与 `ISOLATION_CANCELLED`。Job 报告零活动进程，并清理本轮临时目录。 |
| 配置失效 | QEMU 缺失或镜像摘要不符时返回 `ISOLATION_UNAVAILABLE`，并验证本轮工作负载没有启动。越界输入路径返回 `ISOLATION_INPUT_INVALID`。 |

Android Debug 构建通过。此次验收使用固定脚本和固定页面，生成后 ZIP 的交互检查及 Android 端到端验收分别在 C.5 和 C.7 执行。
