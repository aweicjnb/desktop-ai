# ChatGPT 网页直连验证

验证日期：2026-11-24（Asia/Singapore）。

结论：本机独立脚本已经完成两次真实请求，服务器接受了客户端计算的校验结果，返回完整流式回答；第二次请求复用了第一次的会话并成功接续上下文。

## 实测结果

| 阶段 | 首次提问 | 同会话追问 |
| --- | --- | --- |
| 读取可用模型 | HTTP 200 | HTTP 200 |
| 领取服务器挑战 | HTTP 200 | HTTP 200 |
| 在独立 JavaScript 环境计算校验结果 | 完成 | 完成 |
| 提交校验并领取发送凭据 | HTTP 200，返回凭据 | HTTP 200，返回凭据 |
| 发送消息 | HTTP 200，SSE | HTTP 200，SSE |
| 收到完整回复流 | 收到 `[DONE]` | 收到 `[DONE]` |
| 最终 assistant 正文 | `OK` | `OK` |
| 服务端回复状态 | `finished_successfully` | `finished_successfully` |

首次发送「连通性测试：请只回复 OK。」；第二次在同一会话发送「上一条你回复了什么？请只重复上一条回复。」。验证读取并解码了最终 assistant 消息，没有使用请求正文中的 `OK` 作为成功依据。

## 运行方式

使用从 Chrome 当前有效会话中取得的登录凭据。Python 直接执行 HTTPS 请求，Node.js 24.22.3 与 jsdom 31.0.1 执行已定位的网页校验模块。两次发送均未调用 Chrome、浏览器扩展或浏览器自动化接口。

测试包括 `/backend-api/sentinel/chat-requirements/prepare`、`/backend-api/sentinel/chat-requirements/finalize` 和 `/backend-api/f/conversation`。登录凭据、临时挑战、校验凭据、会话标识和原始回复流仅保存在本机 Windows DPAPI 加密文件中；此报告不包含这些数据。

本次验证证明当前流程可以独立运行。随后已完成正式弹窗接入，实际通过了提问、联网搜索、同会话追问、停止和图片发送，详见 [弹窗接入验收](chatgpt-popup-2026-11-24.md)。长期闲置与网页升级后的兼容性仍需持续观察。
