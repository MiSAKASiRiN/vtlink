# SDK 接口与集成

[返回开发包](../README.md)

`import vtlink` 不连接设备。公开编码函数返回 `bytes`，解析器接受字节流，串口生命周期和发送调度由调用方持有。`vtlink.protocol` 提供两代公开入口，`vtlink.wire` 提供 DUML/A5 基础封装，`vtlink.business` 提供带尾部的下行传输和上行解析。

## 公开 API


| API                                     | 输出 / 用途                                                         |
| --------------------------------------- | ------------------------------------------------------------------- |
| `vt12_query(sequence=0)`                | VT12 接收端模式查询 DUML                                            |
| `vt12_channel(x, y=None, sequence=0)`   | 生成 1–6 信道配置；y 省略时等于 x                                  |
| `vt12_custom(payload, ...)`             | 0302/30B，默认 AA/type-2 传输格式；`aa_type2=False` 选择 A5/type-2 传输格式 |
| `vt13_custom(payload, ...)`             | 0311/30B，AA/type-2 传输格式                                               |
| `vt12_keyboard(...)`                    | VT12 0304 键鼠封装，带完整尾部                                      |
| `vt13_keyboard(InputState(...))`        | 返回 receiver 06/0E 的两条报告                                      |
| `vt13_keyframe(True/False)`             | 请求 / 取消 VT13 关键帧                                             |
| `vt02_set_channel(channel, sequence=0)` | VT02 单机型 UART A5 配置帧                                          |
| `vt02_query_channel(sequence=10)`       | VT02 单机型 UART A5 查询帧                                          |
| `DumlParser().feed(data)`               | CRC 合法的 DUML 字典列表，包含 raw、payload、地址、序号、set/id     |
| `A5Parser().feed(data)`                 | `(command, payload, sequence, raw)` 元组列表                        |
| `UplinkDecoder().feed(data)`            | 已支持封装的消息列表及类型；未知/坏传输抛出 ValueError              |
| `vtlink.hevc.SyncDecoder().feed(data)`  | HEVC 实时同步解码；需 PyAV，返回`(frames, needs_reset)`             |

`vt12_custom()` 的 `aa_type2` 参数选择传输格式：`True` 为 AA/type-2，`False` 为 A5/type-2。两种格式的类型字段均为 `0x02`。

直接封装 A5 业务帧时，使用 `vtlink.business.aa_type2_transport(data, timestamp_ms=0, sequence=0)` 生成 AA/type-2 传输段；`vtlink.business.downlink_transport(...)` 生成 A5/type-2 传输段。两者均包含 4 字节时间与序号尾部。

自定义编码可传 `a5_sequence`、`transport_sequence`、`timestamp_ms`。VT12 另可传 DUML `sequence`；VT13 当前参考外层序号固定 0。时间单位明确为毫秒。

## 仅生成字节

```python
from vtlink import vt12_custom, vt13_custom

payload = bytes(range(30))  # 离线封包测试数据；业务含义由应用定义
a = vt12_custom(payload, a5_sequence=1, transport_sequence=1, timestamp_ms=1000)
b = vt13_custom(payload, a5_sequence=1, transport_sequence=1, timestamp_ms=1000)
print(a.hex(), b.hex())
```

`examples/offline_packets.py` 会反向解析两代封包，证明载荷和命令区分。其行为只打印，不发送。

## 串口集成

`examples/serial_monitor.py` 是最小端口生命周期示例。默认只接收；指定 `--query` 才发送 VT12 查询，指定 `--keyframe` 才发送 VT13 请求/取消。脚本要求显式端口和设备系列。

调用方已有独占串口时，可把 SDK 返回字节写入该串口：

```python
from vtlink import vt12_channel

packet = vt12_channel(3)
# port 是调用方已核对身份、打开且独占的 961200/8N1 Protocol CDC。
written = port.write(packet)
if written != len(packet):
    raise IOError("incomplete serial write")
port.flush()
```

`port.write()` 返回交给主机串口层的字节数。配置结果通过对应 ACK 和状态回读确认；业务接收与执行结果由外部接收端反馈。断线重连后重新枚举并清空旧待发数据。

## 接收端数据分层

```python
from vtlink import DumlParser, UplinkDecoder

outer = DumlParser()
inner = UplinkDecoder()
def receive(chunk):
    for frame in outer.feed(chunk):
        if (frame['set'], frame['id']) != (0x09, 0x62):
            continue
        # 在集成层额外校验 sender/receiver 与本次选定设备匹配。
        try:
            messages, kind = inner.feed(frame['payload'])
        except ValueError:
            continue  # 实际程序应记录不支持/校验失败计数
        for msg in messages:
            print(hex(msg['command_id']), msg['payload_hex'])
```

此示例解析 raw-A5/type-2 数据。`feed()` 需要完整传输段；DUML 层由解析器处理串口断包，A5 层可跨已支持的传输段延续。跨设备重连时新建解析器，避免把旧会话半包拼到新会话。

## 接口范围

VT12 下行使用带完整 4 字节尾部的 A5/type-2 或 AA/type-2 传输格式；VT13 自定义下行使用 `vt13_custom()`。上行解码支持已识别的 raw-A5/type-2。
