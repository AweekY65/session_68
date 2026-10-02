# 本地 HTTP/1.1 请求解析器与连接状态机

纯内存实现的增量式 HTTP/1.1 请求解析器。所有数据只存在于本机内存中，
不依赖 Nginx / Apache / Netty / 远程服务器或任何外部服务，仅使用
Python 3 标准库。

## 文件

- `http_parser.py` — 解析器与连接状态机实现
- `test_http_parser.py` — 自动化测试（unittest，终端直接运行）

## 状态机

每个 `HttpConnection` 实例代表一条逻辑连接，内部状态按序流转：

```
REQUEST_LINE → HEADERS → BODY → (keep-alive ? REQUEST_LINE : CLOSED)
```

- `feed(data)` 接收任意大小的字节块（可逐字节、可半包），返回本次
  完成的 `HttpRequest` 列表；未消费完的字节留在内部缓冲区，与下一块
  拼接后继续解析。
- 同一连接上的 pipelined request 依次解析：每个请求只消费自己的字节，
  缓冲区中剩余数据属于下一个请求，不会被吞掉。
- keep-alive 判定遵循 RFC 7230：
  - HTTP/1.1 默认保持连接，`Connection: close` 后进入 `CLOSED`；
  - HTTP/1.0 默认关闭，显式 `Connection: keep-alive` 才保持；
  - `Connection` 令牌大小写不敏感，支持逗号列表。
- 连接关闭后再 `feed` 数据抛出 `ConnectionClosedError`；若关闭帧后
  紧跟多余字节，异常携带已成功解析的 `completed` 请求列表。

## 协议支持范围

- request line：`METHOD SP target SP HTTP/1.0|1.1 CRLF`，方法须为合法
  token，GET / POST / PUT / DELETE / HEAD / OPTIONS / PATCH 等均可区分。
- headers：`Name: value`，CRLF 结尾，空行结束；拒绝无冒号、冒号前
  空格、obs-fold 折叠行、值中的控制字符。
- body：仅支持 `Content-Length` 定长 body（含 `Content-Length: 0` 和
  GET 携带 body 的情况）；`Transfer-Encoding` 一律返回 501 错误。
- 重复的 `Content-Length`（多行或逗号列表）取值一致时接受，冲突时报
  `ConflictingContentLength`。

## 资源限制

所有上限均可在构造 `HttpConnection` 时覆盖：

| 限制 | 默认值 | 超限错误 |
|---|---|---|
| 请求行长度 | 8 KiB | `RequestLineTooLong` |
| 单个 header 行 | 8 KiB | `HeaderTooLarge` (431) |
| header 个数 | 100 | `HeaderTooLarge` (431) |
| header 块总大小 | 64 KiB | `HeaderTooLarge` (431) |
| body 大小 | 1 MiB | `BodyTooLarge` (413) |

解析器按行增量消费缓冲区，从不按声明长度预分配内存：即使对端声明
`Content-Length: 4294967296`，也只会在头部解析完成时立即抛出
`BodyTooLarge`，缓冲区占用始终受上述上限约束。未等到 CRLF 的半行
同样受单行上限约束，无法让缓冲区无界增长。

## 错误类型

所有错误继承 `HttpParseError`，并带有建议的 HTTP 状态码（`status`
属性）：`BadRequestLine` / `BadHeader` / `InvalidContentLength` /
`ConflictingContentLength`（400）、`HeaderTooLarge`（431）、
`BodyTooLarge`（413）、`UnsupportedTransferEncoding`（501）、
`ConnectionClosedError`。

## 运行测试

```sh
cd /mnt2/zjh/code/Goleta/session_68/b
python3 -m unittest -v        # 或 python3 test_http_parser.py
```

测试全部在本机内存中执行，不需要浏览器、网络或任何外部服务。覆盖
场景包括：完整 GET/POST、逐字节输入、半行 header、部分 body、所有
二切分点等价性、pipeline（整包 / 逐字节 / 乱序分片）、keep-alive 与
Connection: close、HTTP/1.0 语义、非法 / 冲突 / 超大 Content-Length、
各类大小上限和畸形 request line / header。
