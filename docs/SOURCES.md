# 配置依据和源码来源

本组件的 Python/安装脚本为项目新增代码，没有复制 Realtek 驱动、内核源文件或固件。

- [Linux USB 电源管理文档](https://github.com/torvalds/linux/blob/master/Documentation/driver-api/usb/power-management.rst)：autosuspend_delay_ms 为负表示不进行 runtime autosuspend；power/control=on 禁止 runtime autosuspend，但不阻止系统整体休眠。
- [Linux IPv4 sysctl 文档](https://github.com/torvalds/linux/blob/master/Documentation/networking/ip-sysctl.rst)：ARP 的弱主机默认行为、arp_ignore 和 arp_announce。
- [NetworkManager dispatcher](https://networkmanager.dev/docs/api/latest/NetworkManager-dispatcher.html)：脚本的 root 所有权/权限、up/down/DHCP/reapply 事件；排队的事件可能过期，因此本组件总是重新读取当前拓扑。
- [NetworkManager.conf](https://networkmanager.dev/docs/api/latest/NetworkManager.conf.html)：connection 默认设置、按永久 MAC 匹配设备以及 wifi.powersave。已同时核对原测试机 NetworkManager 1.46 的本地手册。
- [lwfinger/rtw88](https://github.com/lwfinger/rtw88)：新版驱动对照来源，不包含在本组件中。
- [morrownr/88x2bu-20210702](https://github.com/morrownr/88x2bu-20210702)：同芯片厂商驱动的社区维护版；不是本组件默认安装对象。
- [MediaTek MT7902 上游蓝牙补丁](https://git.kernel.org/torvalds/c/aab25984e559)：属于内置 MediaTek 网卡的另一个项目，与 Realtek USB 设备不同。

不要以 AC1300 商品名称替代硬件 ID。不要把本组件的配置策略称为自行开发的 Realtek 驱动。
