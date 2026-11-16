# 包结构与组件

| 当前文件 | 用途 |
|---|---|
| `vtlink/wire.py` | CRC、DUML/A5、VT13 关键帧和键鼠报告 |
| `vtlink/vt12_commands.py` | VT12 DUML 基础命令和 VT02 单机型 UART 信道命令 |
| `vtlink/business.py` | 带尾部的 A5/type-2、AA/type-2 下行和上行解析 |
| `vtlink/protocol.py` | 两代设备公开 API，明确命令、地址和长度 |
| `vtlink/hevc.py` | HEVC NAL 分帧和参数集/IRAP 同步 |
| `tools/` | 视频接收、预览、样本解码和验证入口 |
| `samples/`、`profiles/` | 当前离线测试使用的码流和固定协议向量 |

`samples/` 保存离线验证输入，`validation/release/` 保存检查结果。全部交付文件由顶层 SHA-256 清单管理。

## 第三方组件

协议 SDK 无第三方运行依赖；设备/视频工具使用 PySerial、PyUSB、PyAV、Pillow，系统侧使用 libusb、FFmpeg/ffplay。固定 type-2 编码为纯 Python 实现。

各组件遵循其原始许可，本次整理不为整个集合授予统一许可。Python 依赖范围见 `requirements.txt`，验证环境的实际版本见 `validation/release/environment.json`。
