# 测试记录与边界

日期：2026-11-10，组件 0.1.0。

## 自动测试

30 项测试在 macOS Python 3.15 和原 Ubuntu 主机 Python 3.12 上均通过。Ubuntu 验证还使用了压缩包解出的独立副本，避免只验证开发目录而遗漏发布文件。

执行：

```sh
python3 -m unittest discover -s tests -v
```

覆盖 USB 物理父设备匹配、内置网卡排除、变动的网卡/HCI 名、压缩固件、BlueZ D-Bus 读取、精确永久 MAC 默认设置、连接省电值备份恢复、外部编辑保留、符号链接拒绝、写入中途失败回滚、重复安装/卸载、路由资源冲突、复杂/虚拟拓扑拒绝及 VPN 新增规则处理。蓝牙回显解析额外防止把 100% loss 误当成 0% loss；另有回归测试确认 HTTP 读超时不会被错误标记为 TLS 握手失败。

文件安装测试使用临时目录和模拟系统命令；它们验证安装/恢复事务，不能替代目标系统的真实部署和设备验收。

## Linux 路由引擎集成测试

```sh
sudo python3 tests/integration_netns.py
```

测试创建一个随机命名的临时 network namespace，所有 dummy 接口都建在该 namespace 内；物理设备 sysfs 元数据用夹具代替，iproute2 和该 namespace 的 /proc 网络参数是真实操作。

已验证源路由选择 USB 接口、ARP 参数生效、重复 reconcile 不新增重复规则、清理只删除组件完整规则、已有 table 30000 保留、主路由表与有线默认出口不变。测试结束会删除自己创建的 namespace，不修改现用物理接口。

## 真实硬件只读测试

在原 Ubuntu 24.04.4 / 7.0.0-38、Python 3.12 上执行 doctor、安装 dry-run；自动找到 USB 0bda:b82c 的无线接口和对应蓝牙控制器，并排除内置 MT7902。原主机已有专用配置，组件能识别并阻止重复叠加安装。

## 发布包与真实网络探测

发布压缩包在 Ubuntu 端通过 SHA-256 校验，解包后的 doctor/dry-run 正常。对实际 USB 无线接口执行 verify，一次绑定设备/源地址的 DNS 查询成功、TLS 握手超时，工具如实输出 tls_ok=false；同一 USB 设备下的蓝牙控制器 Powered=true。这表明本次该端点探测失败，不能把 DNS 成功当成整体联网验收通过，也不能仅据此断言驱动损坏。增加到 15 秒超时后，对原百度端点又出现过 HTTP 读取超时，因此后续版本将 DNS、TCP、TLS 和 HTTP 分层输出，保留真实失败阶段。随后选择 www.qq.com 交叉探测，实际 USB 接口的 DNS、TCP、TLS、HTTP 各层均成功，返回 HTTP/1.1 200 OK，CLI 退出码为 0；失败样本继续保留。这个 verify 没有传入 --peer，故不把它表述成新一轮手机通信测试通过。

弱链路或不同网络可以用 `--timeout 20` 指定每个数据套接字的超时，并选择当前网络可访问的 DNS/HTTPS 目标。任何后续探测结果应另行记录，不覆盖原先的失败样本。

## 尚未完成

目标 Ubuntu 26.04 的真实安装、重启/休眠/插拔恢复、实际 AP 和手机/耳机验收尚未完成。原主机的硬件测试成绩见 PROCESS.md 及项目外的详细验收报告，不能把它当成“通用安装器已在 26.04 全部通过”。

GitHub Actions 配置在 push 和 pull request 时运行单元测试、语法检查和 Linux 路由集成测试。每次提交的运行状态见 [Actions 页面](https://github.com/we1005/rtl8822bu-setup/actions)；CI 结果不替代目标 Ubuntu 26.04 的实机验收。

## 同日后续蓝牙复测

原 Ubuntu 24.04 / 7.0 测试机最后一轮重启后的手机通信已补测：内置和外置蓝牙各 5/5 L2CAP Echo，内置发送 4,154 字节、外置重发发送 4,163 字节，均得到 OBEX Status=complete，并由用户确认接收。外置第一次传输曾返回 Forbidden，失败日志保留；重发接受后成功。

这些结果仍属于原测试机，未转写为目标 Ubuntu 26.04 的测试结果。目标机本次 SSH 未连通，未进行实际安装。
