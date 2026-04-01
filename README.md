# GrokBridge

GrokBridge 是一个以中文使用体验为主的 Grok 网页接口代理，兼容 OpenAI API，并内置多账号配置页面，适合通过 Docker 快速部署。

## 项目来源

本项目基于 [CNFlyCat/GrokProxy](https://github.com/CNFlyCat/GrokProxy) 二次开发，保留原项目 `LICENSE`，并在此基础上增加了浏览器会话接入、配置页面、图片回传适配和更适合自部署的 Docker 流程。


## 当前特性

- 兼容 OpenAI API，可直接对接 OpenWebUI、OpenClaw 等客户端
- 通过 Docker Compose 同时启动 Grok 浏览器和 API 代理
- 内置 `/ui` 多账号配置页，支持从本机浏览器复制请求头或 cURL 后快速导入 Cookie
- 支持自动生成调用 API Key，并写回本地配置
- 支持多账号列表管理，可编辑、删除、排序，并按顺序轮询
- 已适配 Grok 图片卡片，可返回图片链接，并额外输出 `MEDIA:` 供支持多媒体的前端消费


## 快速开始

### 1. 克隆仓库

```bash
git clone https://github.com/lianying1716/GrokBridge.git
cd GrokBridge
```

### 2. 准备配置文件

```bash
cp cookies.example.yaml cookies.local.yaml
```

首次部署时，你可以先不手动填写 Cookie，后面直接通过网页管理页导入并保存多个账号。

### 3. 可选修改端口

如果你希望改端口，可以新建或修改本地 `.env`：

```dotenv
GROKPROXY_PORT=18080
BROWSER_WEB_PORT=13000
```

不写也可以，默认就是这两个端口。

### 4. 启动服务

```bash
docker compose up -d --build
```

## 首次使用

启动完成后，默认有 2 个常用入口：

- API: `http://127.0.0.1:18080/v1`
- 配置页: `http://127.0.0.1:18080/ui`



1. 在你本机浏览器里登录 `grok.com`
2. 打开开发者工具 `Network`
3. 选中一个请求，复制 `Request Headers`，或者直接使用 `Copy as cURL`
4. 打开 `http://127.0.0.1:18080/ui`
5. 把内容粘贴到账号编辑区
6. 点击“解析粘贴内容”或“从剪贴板读取”
7. 页面会自动填入 Cookie、User-Agent 和语言
8. 点击“加入账号列表”，需要多个账号就重复抓取cookie导入多次
9. 再填写或生成“调用 API Key”
10. 最后点击“保存全部配置”

保存完成后，配置会写入本地的 `cookies.local.yaml`，并通过 Docker 挂载到容器内的 `/app/cookies.yaml`。运行时会按列表顺序轮询这些账号。

## 配置示例

仓库中的模板文件如下：

```yaml
sessions:
  - name: "账号 1"
    cookie: "sso=replace-with-your-first-cookie"
    user_agent: "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    accept_language: "zh-CN,zh;q=0.9,en;q=0.8"
  - name: "账号 2"
    cookie: "sso=replace-with-your-second-cookie"
    user_agent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    accept_language: "en-US,en;q=0.9"

# Used in Authorization: Bearer <key> when calling GrokBridge /v1
password: "replace-with-your-api-key"
```

## API 测试

### 查看模型

```bash
curl -H "Authorization: Bearer <your-api-key>" \
  http://127.0.0.1:18080/v1/models
```

这里的 `<your-api-key>` 指的就是你在 `/ui` 页面保存的“调用 API Key”。

### 文本对话

```bash
curl -H "Authorization: Bearer <your-api-key>" \
  -H "Content-Type: application/json" \
  -X POST http://127.0.0.1:18080/v1/chat/completions \
  -d '{"model":"grok-auto","stream":false,"messages":[{"role":"user","content":"Reply with exactly OK."}]}'
```

### 图片回传

如果前端支持 `MEDIA:` 语法，图片生成结果会同时包含：

- 可点击的图片 URL
- `MEDIA:http://...` 形式的媒体地址

## 目录说明

- `cookies.example.yaml`: 配置模板文件，供首次部署时参考
- `cookies.local.yaml`: 实际运行使用的本地配置文件
- `.env`: 端口和运行参数配置文件
- `browser-data/`: 浏览器缓存、登录状态和会话数据目录
- `ui/`: 内置配置页

## 免责声明

本项目采用 MIT License 发布，仅适合学习、研究和自部署场景。请自行确认目标服务条款、网络环境和当地法律法规，使用风险由使用者自行承担。
