# A Minecraft Launcher

一个轻量、现代的 Minecraft 启动器，使用 NiceGUI + pywebview 构建。

当前版本：**v1.3.1**

## 功能

- **启动**：离线登录、选择版本、一键启动，启动过程显示实时进度；左侧为可折叠的更新日志。
- **版本管理**：本地版本列表、版本隔离、补全缺失文件、删除版本、崩溃日志诊断；安装时可自定义版本名称并重命名，同一原版版本可共存多份。
- **下载中心**：安装官方游戏版本，可选装 Fabric / Forge / NeoForge / Quilt 加载器；按本地版本自动筛选并下载 Mod、资源包、光影包；支持整合包在线安装与本地导入，安装前确认版本名称，并自动补全原版文件、安装包内加载器。
- **Mod 管理**：读取 JAR 元数据显示真实名称、版本与图标；支持启用 / 禁用、检查更新并按当前游戏版本与加载器一键更新。
- **存档与服务器**：存档可备份、导入与删除；服务器列表直接写入 `servers.dat`，支持添加与删除。
- **Java 管理**：自动检测本机 Java，缺少对应版本时自动下载安装，也可手动指定路径。
- **下载加速**：多线程并发 + 大文件动态分片 + 断点续传；BMCLAPI 镜像与官方源都可用时双源并行下载。
- **任务中心**：统一展示下载 / 安装进度、总速度、已完成大小；任务完成后自动清理。
- **设置**：启动器设置与游戏设置分离，可修改游戏目录、下载源、内存、窗口尺寸等。
- **哈希存储（实验性）**：游戏文件按 SHA1 去重后统一存放，启动时物化到临时目录，退出后回收；可随时原子迁移开启 / 关闭。

## 快速开始

环境要求：Python 3.11+，Windows 10/11（桌面窗口依赖 WebView2）。

```powershell
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

浏览器模式（无桌面环境或调试时）：

```powershell
python main.py --web
```

显示运行日志面板：

```powershell
python main.py --debug
```

## 数据目录

所有运行数据保存在启动器同级目录的 `data/` 中：

| 路径 | 内容 |
| --- | --- |
| `data/launcher_config.json` | 启动器配置 |
| `data/accounts.json` | 本地账号 |
| `data/.minecraft/` | 游戏文件（版本、libraries、assets、mods 等） |
| `data/.minecraft/runtime/` | 自动下载的 Java 运行时 |
| `data/.minecraft/modpacks/` | 导入或下载的整合包归档 |
| `data/backups/` | 存档备份 zip |
| `data/mcfile/` | 哈希文件库（开启哈希存储后） |
| `data/.work/` | 运行期间物化的工作目录（退出时回收） |

整个 `data/` 可以随启动器一起拷贝，实现便携使用。

## 哈希存储（实验性）

在「设置 → 启动器设置 → 存储方式」中开启。开启后：

- 游戏文件（版本、libraries、assets、mods、存档、日志等）全部按 SHA1 命名存入 `data/mcfile/`，相同内容只存一份；
- 配置文件 / 存档 / 日志等可写文件用 zlib 压缩存储（带 `AMLZ` 魔数头），jar、libraries、assets 保持原样以支持硬链接；
- `data/mcfile/tree.json` 保存「相对路径 → 哈希」的文件树映射；
- 启动游戏时把文件树物化到 `data/.run/<id>/`：libraries/assets/jar 用硬链接，其余复制，退出后同步回文件库；
- 启动器关闭时会结束仍在运行的游戏并同步存档，然后清空 `data/.work/`；
- 下载 / 安装前若文件已存在（哈希与大小一致）会直接跳过。

注意：

- 开启后游戏文件**无法被其它启动器读取**；关闭该选项会用原子迁移还原成普通 `.minecraft`。
- 迁移需要约 2 倍磁盘空间，迁移期间请勿关闭启动器。
- 系统关机或强制关窗时无法弹窗确认，启动器会直接结束游戏进程并尽力同步。

## 构建

```powershell
python build.py
```

产物输出到 `dist/<版本号>/A Minecraft Launcher.exe`，单文件便携版。

## 测试

```powershell
python -m unittest discover -s tests -q
```

## 目录结构

```text
app/        应用层：配置、任务、服务编排、API、NiceGUI 界面
engine/     引擎层：版本解析、下载、启动、Java、整合包、内容管理
tests/      单元测试
assets/     图标资源
main.py     程序入口
build.py    版本化构建脚本
cli.py      引擎命令行工具
```

## 说明

- 仅支持离线登录；微软 / 外置登录的底层能力保留，界面暂未开放。
- 自动安装 Java 依赖 Adoptium API，网络不可达时请手动在设置中指定 Java。

## 许可证

本项目基于 [GNU Affero General Public License v3.0](LICENSE)（AGPL-3.0）发布。

你可以自由使用、修改和分发本项目，但分发修改后的版本（包括通过网络提供服务）时必须同样以 AGPL-3.0 开放源代码。
