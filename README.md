# rtl8822bu-setup

为 **Realtek RTL8822BU USB Wi-Fi / 蓝牙组合适配器**安装针对性的电源管理配置，并提供可选的同网段多网卡处理。当前硬件识别白名单是 **USB `0bda:b82c`**。

本组件复用 Linux 自带的 `rtw88_8822bu`、`btusb` / `btrtl`，**不包含或编译内核驱动、固件、DKMS 模块**。不要把内置 MT7902 的 `mt7902-bt-dkms` 装到这张 Realtek USB 网卡上。

版本：0.1.0。MIT，Copyright (c) 2026 ljc。

## 测试范围

| 环境 | 已验证内容 |
|---|---|
| 原 12800H / Ubuntu 24.04.4 / 内核 7.0.0-38 | 外置适配器真实传输、蓝牙配对/文件发送、休眠、启动和真实插拔；原主机专用配置已生效 |
| 本组件 / macOS Python 3.15 与 Ubuntu Python 3.12 | 自动测试：设备识别、备份恢复、重复安装、冲突保护等 |
| 本组件 / Ubuntu Linux 隔离网络命名空间 | 真实 iproute2 源路由、ARP 参数、清理与主默认路由保留 |
| 本组件 / 原 12800H 的真实硬件 | 只读 doctor、安装 dry-run；正确定位 USB Wi-Fi 和 USB 蓝牙，没有套用到内置卡 |
| **目标 Ubuntu 26.04** | **待插入硬件后验收**；安装器会检查实际内核模块、固件和网络管理器，不按发行版名称盲装 |

原主机已经有专用管理脚本，本组件会拒绝与它重复安装。历史硬件测试通过，不代表该通用安装器已在目标 Ubuntu 26.04 上实机验收。

## 快速使用：Ubuntu 桌面 / NetworkManager

把压缩包复制到目标 Ubuntu，插入 USB 适配器：

```sh
tar -xzf rtl8822bu-setup-v0.1.0.tar.gz
cd rtl8822bu-setup
./check.sh
./install.sh --dry-run
sudo ./install.sh --install-deps
```

`--install-deps` 只通过发行版 apt 安装工具、BlueZ 和 linux-firmware，不添加 PPA，不替换内核，不安装 NetworkManager 或切换网络 renderer。默认安装需要目标已运行 NetworkManager。

如果安装前缺固件或设备探测失败，补齐依赖后重新插拔适配器，再运行 check/verify，让驱动重新探测；安装器不会擅自重置正在使用的整个 USB 设备。

安装器会：

1. 按 USB ID 和 sysfs 物理路径定位设备，重新识别目标机器上的无线接口、永久 MAC 和蓝牙控制器。
2. 确认当前内核有原生 Wi-Fi / btusb 模块，接受 `.bin`、`.bin.zst` 等固件存储形式。
3. 为该 USB ID 安装 udev 规则，禁止该设备 runtime autosuspend。
4. 以本机识别到的 **USB 适配器永久 MAC**匹配 NetworkManager 默认省电设置，不匹配内置无线网卡。
5. 为实际使用 USB 网卡的活动连接记录原省电值并关闭省电；后续连接通过 dispatcher 自动维护。
6. 保存文件原始内容和模式、连接设置以及组件自建资源，供卸载恢复。

安装不会创建 Wi-Fi 连接、索取密码、改变 SSID/BSSID/频段/DNS/默认路由 metric、重启网络或自动重启系统。若同一连接配置也被其他网卡复用，它的省电值是**连接级**属性，复用时也会体现；卸载会按记录恢复未被用户再次修改的值。

连接由系统原有界面或 nmcli 完成。先从 `check.sh` 找到本机接口名，再执行：

```sh
nmcli --ask device wifi connect "你的SSID" ifname "实际USB无线接口名"
sudo python3 rtl8822bu_setup.py refresh
sudo python3 rtl8822bu_setup.py verify --dns-server 223.5.5.5 --https-host www.qq.com
```

`--ask` 在终端交互输入 Wi-Fi 密码，避免密码出现在命令参数和脚本中。新连接的默认省电值会按永久 MAC 匹配；如果插卡前就安装了组件，首次识别到卡时才补齐该匹配项。

verify 会保留每一层的结果；TLS 超时不会被当作成功，也不自动归因于驱动。弱链路可增加 `--timeout 20`，并选择本网络可访问的目标。详见测试记录中的真实失败样本。

## 多网卡同网段：按需启用

仅当 USB 无线接口与其他**物理**网卡同时处于重叠 IPv4 网段、出现回包走错接口时，才使用这一模式：

```sh
# 尚未安装时：
sudo ./install.sh --install-deps --multihoming
```

如果已经用默认选项安装，先 `sudo ./uninstall.sh`，再按上面命令重新安装；重复安装相同模式是幂等的，不同模式需要先卸载。

此模式动态识别与 USB 网卡网段重叠的物理接口，为它们设置接口级 ARP 行为，并根据各接口的实际地址和主表接口路由创建源地址规则。资源从空闲表 30000–30063、优先级 20000–20063 选择，不沿用旧主机的固定地址、接口名或路由表编号。普通主路由表及其默认出口保持原样。

它会拒绝已有的其他自定义策略规则、桥接/虚拟拓扑和复杂 nexthop 路由，防止把 VPN、Tailscale 或其他路由管理器当成普通双网卡场景。发现新 VPN 策略时，dispatcher 会撤回本组件拥有的源规则并报告冲突，不尝试覆盖 VPN。没有重叠网段时不会创建这些规则。

## Ubuntu Server / systemd-networkd

如果服务器没有运行 NetworkManager，默认完整安装会停止，而不是接管现有网络。可以明确选择仅 USB 电源策略：

```sh
sudo ./install.sh --power-only --install-deps
```

`--power-only` 不安装 NetworkManager 配置/dispatcher，不调整连接省电或路由；无线连接及持久无线省电仍由原来的 netplan/networkd 配置管理。需要完整 NetworkManager 模式时，应另外规划网络管理器迁移，不在一键脚本里偷偷切换。

## 蓝牙验收

`check.sh` 依据 USB 物理父设备找到蓝牙控制器。优先读取 BlueZ D-Bus 状态，兼容回退到 hciconfig；不会假定外置蓝牙一定是 hci0，也不会给所有控制器解锁或自动信任陌生设备。

打开系统蓝牙设置，对自己的手机/耳机完成配对。之后可以对已经配对、当前可连接的设备测试：

```sh
sudo python3 rtl8822bu_setup.py verify --peer AA:BB:CC:DD:EE:FF
```

把示例地址换成自己授权设备的地址。`verify` 的网络请求同时绑定 USB 接口和源地址；蓝牙回显只从这个 USB 适配器对应的控制器发起，不会借内置蓝牙“代测”。不传 `--peer` 时，蓝牙结果仅是控制器状态，**不表示配对、音频或实际传输已验收**。

有些对端不支持 L2CAP Echo。回显失败时检查对端状态，再用真实文件或对应业务测试，不能只凭一次回显超时断言驱动损坏。

## 卸载与文件所有权

```sh
sudo ./uninstall.sh
```

卸载恢复本组件覆盖前的文件和模式，仅恢复仍等于本组件设置值的连接参数；清理路由时使用完整源地址/优先级/表和路由描述，不清空别人的路由表。安装后被用户另行修改的管理文件会保留并报告，备份不丢弃。

最初插在同一 USB 路径的设备可恢复安装前 runtime 电源值；其他枚举路径没有可靠的原值，删除规则后重新插拔即可交回系统默认策略。恢复连接文件并不等于立即重连网卡，必要时由用户重连/重插。

安装位置：

```text
/usr/local/libexec/rtl8822bu-setup
/etc/udev/rules.d/81-rtl8822bu-setup.rules
/etc/NetworkManager/conf.d/81-rtl8822bu-setup.conf       # 完整模式
/etc/NetworkManager/dispatcher.d/85-rtl8822bu-setup       # 完整模式
/etc/rtl8822bu-setup/config.json
/var/lib/rtl8822bu-setup/                               # 文件/连接备份，无密码
/run/rtl8822bu-setup/                                   # 本次开机的运行状态
```

## 源码、过程和限制

- [本次究竟改了哪张网卡的代码](docs/PROCESS.md)
- [Ubuntu 26.04 迁移清单](docs/UBUNTU26_MIGRATION.md)
- [测试与验证范围](docs/TESTING.md)
- [配置依据和上游项目](docs/SOURCES.md)

支持对象以硬件识别为准。相同“AC1300”标签可能使用其他芯片；同一个 USB ID 的其他硬件实现也不保证一定带蓝牙。组件不会安装 MT7902 补丁，不会为不存在的蓝牙功能制造一个控制器，也不会把某个路由器的协议问题伪装成已经修复。
