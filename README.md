# OmniX VTLink VT02/12 · VT03/13 图传SDK

版本 **1.0.0** · 整理日期 **2026-11-17** · Python 协议层要求 **3.10+**

本项目由Sirin (m@sirin.moe | misaka@sirin.moe) 开发

### **本项目是一个提供直接与DJI RoboMaster VT12（老图传）和VT13（新图传）接收端进行交互实现拉流和双向通信的SDK，不再需要使用RoboMaster选手端/客户端进行转发**

### **本项目没有对DJI硬件或其软件、协议进行破解，仅仅将接收端视为黑盒分析其枚举行为并实现通信，因此很多功能“按照现状”(AS-IS)提供，可能并不完善**

### **本项目旨在提升比赛机器人调试效率（队伍中电控大部分是Mac，调试中运行Windows+Unity官方选手端十分困难，因此开发），2027赛季规则并不允许自定义终端直接与图传交互（必须使用官方选手端中转）请参赛队伍自行遵守比赛规则，并建议依旧最终在官方选手端中转时进行自定义终端验证**

考虑到老图传（VT02/12）已经退役，官方也发布了离线版本的发送端协议 ，因此配合本接收端开发包可以直接实现带有图传和双向通信的机器人遥控系统。如本队已经在退役的步兵上使用老图传+SDK+Switch直接实现带图传的机器人遥控，极大便利了交流和参观活动

若本项目内容侵犯了DJI及比赛组委会的相关权利，请联系作者光速删除

--上述宇宙安全免责声明虽然一股GPT的防御性输出味儿，但确实是作者本人写的（逃--

本包面向直接接入图传硬件的开发者，包含 USB/串口参数、视频接收、配置命令、透明数据封装、离线样本与验证工具。VT02、VT03 是发送端；VT12、VT13 是接收端。数据载荷的具体业务含义由调用方定义，应用层负责定义数据载荷与控制逻辑。

## 硬件


| 项目               | VT02 / VT12                | VT03 / VT13                           |
| ------------------ | -------------------------- | ------------------------------------- |
| 接收端 USB VID:PID | `2CA3:001F`                | `2CA3:1020`                           |
| 视频入口           | RNDIS → IPv4 → UDP 8080  | Interface 0，Bulk IN`0x81`            |
| 码流               | 8 字节前导 + H.264 Annex B | 连续 HEVC/H.265 Annex B，无额外业务头 |
| 样本分辨率         | 1280×720                  | 1920×1080；亦有其他尺寸记录          |
| 主机协议串口       | CDC Control 4 / Data 5     | Protocol CDC Control 2 / Data 3       |
| 主机串口设置       | 961200、8N1、无流控        | 961200、8N1、无流控                   |
| 自定义下行参考     | `0x0302`，30 字节          | 当前`0x0311`，30 字节                 |
| 主动关键帧请求     | 无成功命令                 | Protocol 串口 DUML`01/01` 请求及取消  |

注意到，老图传使用的是USB虚拟网卡枚举，而新图传直接使用了USB Bulk枚举

视频解码需要完整参数集和关键帧。VT12 建议先启动接收端和 SDK，再启动发送端；中途接入时需等待 SPS/PPS 和 IDR，恢复前可能无图或出现损坏画面。VT13 预览工具会通过 Protocol CDC 请求关键帧，并等待 VPS/SPS/PPS 和 IRAP 完成同步。

## 从这里开始

在本文件所在目录执行。以下命令仅生成协议字节、运行协议测试，无需安装第三方库，也不会连接硬件，旨在验证代码完整性

```sh
python3 -m vtlink frames --family vt12 --channel 3
python3 -m vtlink frames --family vt13
python3 examples/offline_packets.py
python3 -m unittest discover -s tests -v
python3 tools/verify_integrity.py
```

安装可选视频/串口依赖后，按[快速接入](docs/01_快速接入.md)选择平台：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m vtlink ports
```

Windows 对应解释器为 `.venv\Scripts\python.exe`。协议模块可用 `python -m pip install .` 安装到其他开发环境；

## 文档


| 入口                                   | 内容                                              |
| -------------------------------------- | ------------------------------------------------- |
| [01 快速接入](docs/01_快速接入.md)     | Windows、Linux、macOS 的最小使用路径              |
| [02 硬件接口](docs/02_硬件接口.md)     | USB 功能、CDC 识别、网络、发送端 UART、电气边界   |
| [03 视频接收](docs/03_视频接收.md)     | H.264/HEVC、分片、NAL、关键帧、缓存、录制         |
| [04 通信协议](docs/04_通信协议.md)     | DUML、A5、CRC、AA/type-2 封装、命令与键鼠字节布局 |
| [05 SDK 接口](docs/05_SDK接口.md)      | Python API、上下行示例、串口集成                  |
| [06 排障与验证](docs/06_排障与验证.md) | 枚举、无图、花屏、串口占用、逐层接收确认          |
| [包结构与组件](COMPONENTS.md)          | 包内模块、运行依赖与组件说明                      |

## 目录

```text
vtlink/                可导入的协议 SDK；公开 API 均只生成/解析字节
tools/                 两代视频工具、完整性校验、离线验证入口
examples/              离线封包、显式串口监听/状态查询/关键帧请求
tests/                 当前 SDK 固定向量、CRC、断包与边界测试
profiles/              机器可读设备参数
samples/               H.264/HEVC 离线验证样本
validation/release/    此次交付的离线验证结果
MANIFEST.sha256.json   本次交付文件校验清单
work/                  使用者运行输出，不纳入完整性清单
```

**发送接口统一使用 `vtlink`。** VT12 自定义和键鼠下行包含完整的 4 字节传输尾部；VT13 自定义发送使用 `vt13_custom()` 的 `0x0311` 接口。
