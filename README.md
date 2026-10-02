# 本地 HTTP/1.1 请求解析器与连接状态机

纯 Python 标准库实现，所有数据只存在于本机内存中。不依赖 Nginx / Apache /
Netty / 远程服务器或任何外部服务，也不进行任何真实网络通信。

## 文件

- `http_parser.py` — 增量解析器 + 单连接状态机
- `test_http_parser.py` — 基于 `unittest` 的自动化测试（终端直接运行）

## 解析器状态机

每个连接对应一个 `HttpConnection` 实例，内部状态按序推进：

```
REQUEST_LINE → HEADERS → BODY → (keep-alive ? REQUEST_LINE : CLOSED)
```

- `feed(data: bytes) -> list[HttpRequest]`：喂入任意分片的字节流，
  返回本次完成的请求列表；未消费完的字节留在内部缓冲区，等待下一次喂入。
- TCP 分片：半个 request line、半个 header、半个 body 都会被缓存拼接，
  直到数据齐全才产出请求。
- Pipeline：同一连接上的多个请求依次解析，前一个请求只消费自己的字节
  （request line + headers + Content-Length 指定的 body），
  剩余数据完整留给下一个请求。
- 请求解析完成后根据协议规则决定连接去向：
  - HTTP/1.1 默认 keep-alive，除非 `Connection: close`；
  - HTTP/1.0 默认关闭，除非 `Connection: keep-alive`；
  - 进入 `CLOSED` 后再 `feed()` 会抛出 `ConnectionClosedError`，
    且 close 之后 pipeline 中残留的数据不再处理。

## 协议支持范围

- 解析 request line（方法 / 目标 / 版本）、headers、Content-Length body。
- 方法不做白名单限制，GET、POST、PUT、DELETE 等合法 token 均可区分。
- 仅支持 HTTP/1.0 与 HTTP/1.1；仅支持 `Content-Length` 定长 body，
  `Transfer-Encoding: chunked` 等会返回 `UnsupportedTransferEncoding` 错误。
- 要求严格的 CRLF 换行；拒绝 obs-fold 折叠头、无冒号头、非法头名。

## 资源限制（防恶意输入）

所有限制在 `HttpConnection(...)` 构造时可配置，默认值如下：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `max_request_line` | 8192 | request line 最大字节数 |
| `max_header_bytes` | 16384 | 整个 header 块最大字节数 |
| `max_headers` | 100 | header 字段最大条数 |
| `max_body` | 1 MiB | body 最大字节数 |

限制在数据到达时即检查（未收到完整行/块也会按已缓冲字节数判断），
不会在内存中无界累积。`Content-Length` 在 header 解析完成时立即校验：
非数字、负数、带符号、超大声明（超过 `max_body`）都会在收到任何 body
字节之前直接报错，恶意长度声明不会触发无界内存申请。

## 错误类型

均继承自 `HttpParseError`：

- `BadRequestLine` / `RequestLineTooLong` — 畸形或过长的 request line
- `BadHeader` / `HeaderTooLarge` / `TooManyHeaders` — 畸形或超限的 header
- `InvalidContentLength` / `ConflictingContentLength` — 非法或重复冲突的长度
- `BodyTooLarge` — 声明长度超过上限
- `UnsupportedTransferEncoding` — 不支持的 Transfer-Encoding
- `ConnectionClosedError` — 连接关闭后仍有数据喂入

## 运行测试

```sh
cd /mnt2/zjh/code/Goleta/session_68/a
python3 -m unittest -v
```

测试全部在本机内存中执行，不依赖浏览器、网络或任何外部服务，覆盖：
完整 GET/POST、逐字节喂入、半包 header、分片 body、pipeline 多请求、
keep-alive / Connection: close、非法与冲突 Content-Length、
header/body 大小上限、畸形 request line 等场景。
