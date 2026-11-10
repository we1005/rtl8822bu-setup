# Ubuntu 26.04 迁移步骤

本文针对 USB ID 0bda:b82c 的 RTL8822BU 组合适配器。目标主机的实测尚未完成；以下流程通过安装前识别来避免把发行版版本号当成驱动兼容证明。

## 1. 复制文件

把 `rtl8822bu-setup-v0.1.0.tar.gz` 与同名 `.sha256` 文件带到新主机。组件代码不依赖联网下载；只有选择安装 apt 依赖或进行网络探测时才使用网络。

```sh
sha256sum -c rtl8822bu-setup-v0.1.0.sha256
tar -xzf rtl8822bu-setup-v0.1.0.tar.gz
cd rtl8822bu-setup
./check.sh
```

检查输出里的 USB ID、真实无线接口名、蓝牙控制器、驱动、固件与 NetworkManager 状态。拔走原主机的 USB 适配器前确保原主机的 SSH 仍由有线提供。

## 2. 先确认模块和固件

```sh
uname -r
lsusb
modinfo rtw88_8822bu
modinfo btusb
```

正常固件位置包括：

```text
/lib/firmware/rtw88/rtw8822b_fw.bin[.zst/.xz/.gz]
/lib/firmware/rtl_bt/rtl8822b_fw.bin[.zst/.xz/.gz]
/lib/firmware/rtl_bt/rtl8822b_config.bin[.zst/.xz/.gz]
```

如果模块缺失，先确认安装了**与正在运行的内核匹配**的发行版模块包。某些 Ubuntu 内核变体将驱动放在 linux-modules-extra 中，但不要把另一台机器的包名/版本硬套过去，也不要拷贝旧主机的 .ko。安装器不会自动换内核或编译第三方驱动。

如果固件缺失，用发行版的 linux-firmware 更新。已有第三方 88x2bu 驱动或 blacklist 时，先查清当前绑定与冲突；本工具不会偷偷卸载第三方驱动。

补固件后如设备仍未绑定驱动，应重新插拔，再做检查；复制固件文件本身不保证会自动重试先前失败的 USB probe。

## 3. 根据网络管理器选择模式

Ubuntu 桌面、NetworkManager 已运行：

```sh
sudo ./install.sh --install-deps
```

USB Wi-Fi 与另一张物理网卡同网段并且确实需要同时使用、系统没有其他自定义策略路由时：

```sh
sudo ./install.sh --install-deps --multihoming
```

Ubuntu Server 使用 netplan/networkd、不准备迁移网络管理器：

```sh
sudo ./install.sh --power-only --install-deps
```

完整模式需要 NetworkManager 已正常运行；脚本不会在 SSH 下擅自把 networkd 换成 NetworkManager。

## 4. 建立 Wi-Fi 连接

从 doctor 输出取接口名。新主机可能使用 wlx 开头，也可能是管理员命名的其他名称，不能照抄旧主机的名字。

```sh
nmcli --ask device wifi connect "目标SSID" ifname "实际USB接口名"
sudo python3 rtl8822bu_setup.py refresh
sudo python3 rtl8822bu_setup.py verify --dns-server 223.5.5.5 --https-host www.qq.com
```

根据所在地和网络策略选择能访问的 DNS/HTTPS 目标。verify 对数据套接字绑定实际 USB 网卡和源地址；不会用有线默认出口代替测试。

分别测试 2.4GHz、5GHz；某个接入点失败时，用自己控制的手机热点交叉验证，记录认证、DHCP、DNS、TLS 哪一层失败，再决定是否需要驱动或 AP 侧工作。

## 5. 建立蓝牙连接

doctor 根据 USB 父设备定位控制器，使用输出中的 HCI 名和地址，不照抄 hci0/hci1。

在目标 Ubuntu 的蓝牙设置里开启控制器，对自己的手机或耳机配对。安装不自动放行 rfkill 或信任扫描到的陌生设备。

```sh
sudo python3 rtl8822bu_setup.py verify --peer 你的设备蓝牙地址
```

再做一次真实文件传输或耳机/键鼠业务测试。Host is down 需要先区分本地控制器、距离和对端蓝牙状态；不能自动归罪固件，也不能未经测试宣称通过。

## 6. 恢复性验收

依次检查：真实拔插后 Wi-Fi 重连与蓝牙配对保留、重启后策略生效、休眠唤醒后设备恢复、持续传输与蓝牙并发。每个阶段保留 check.sh / verify 的输出及相关内核日志。

USB power/control 可能被驱动改回 auto；autosuspend_delay_ms 的负值才是本策略另一项有效判据。系统整体休眠与 USB runtime autosuspend 是两件事，不因为禁用了后者就宣称不会系统休眠。

本组件不自动执行 suspend/reboot，也不在没有真实耳机/键鼠的情况下把音频或 HID 标成已验证。

## 7. 回滚

```sh
sudo ./uninstall.sh
```

如有“文件被外部编辑”的提示，先检查该文件及 `/var/lib/rtl8822bu-setup/install.json`，不要直接删除备份。卸载后必要时重新插拔，让实时电源设置回到系统默认。
