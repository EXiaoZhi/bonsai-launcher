# Bonsai 本地 LLM 启动器

一个**纯标准库、零依赖、双击即用**的本地大模型启动器 + 聊天界面。管理多个模型的启停、参数、MCP 工具与文件空间，内置 OpenAI 兼容网关。

## 特性

- **多模型直启**：每个模型独立进程，KVMem（三元量化）与 llama.cpp 双引擎，互不干扰
- **内置聊天页**：流式对话、思考强度、工具调用、会话历史、导出/复制对话
- **MCP 服务器**：全局配置，所有模型共用工具；渐进式披露省 token
- **文件空间（Skill）**：选一个本地文件夹，让模型读取/写入其中的文件
- **OpenAI 兼容网关**：`/v1/chat/completions`、`/v1/embeddings`、`/v1/rerank`、`/v1/models`
- **全部相对路径**：解压即用，不写死任何绝对路径，可直接上传 GitHub 分发

## 快速开始

1. 安装 **Python 3.8+**（Windows 建议勾选「Add to PATH」）
2. 双击 `start-launcher.bat`（或命令行 `python main.py`）
3. 浏览器自动打开控制面板 `http://127.0.0.1:18300`

> 首次启动若提示缺少引擎，见下方「放置引擎与模型」。

## 目录结构

```
bonsai-launcher/
├── main.py            # 入口
├── core.py            # 常量 / 共享状态 / 系统信息
├── models.py          # 模型清单 / 引擎宿主 / 网关桥接
├── workspace.py       # 文件空间 / 工具授权 / 对话历史
├── mcp.py             # MCP 服务器（stdio）
├── tools.py           # 工具注册 / 执行 / 解析
├── agent.py           # 引擎流式调用 + Agent 循环
├── server.py          # HTTP 服务与路由
├── webui.py           # 启动面板前端加载
├── engine_host.py     # 引擎宿主实现
├── param_specs.py     # 参数规格（由 tools/gen_param_specs.py 生成）
├── gateway/           # 内嵌聊天后端（OpenAI 兼容网关）
├── web/               # 聊天页前端
├── webui/panel.html   # 启动面板前端
├── engine/            # 引擎二进制（见下）
├── model/             # 模型文件（见下）
├── models-config.json # 模型与槽位配置
├── mcp-servers.json   # MCP 服务器配置
└── start-launcher.bat # 双击启动
```

## 放置引擎与模型

引擎和模型体积大，**不随代码分发**，需自行放入：

### 引擎（`engine/` 目录）

| 文件 | 用途 | 必需 |
|---|---|---|
| `llama-kvmem-server.exe` | KVMem 引擎（三元量化） | 选装 |
| `llama-server.exe` | llama.cpp 官方引擎（标准 GGUF） | 选装 |
| `cudart64_13.dll`、`cublas64_13.dll`、`cublasLt64_13.dll` | CUDA 运行时 | GPU 推理必需 |

> `llama-server.exe` 也可放在别处，在面板里指定，或在 `models-config.json` 的 `llama_exe` 里配置。

### 模型（`model/` 目录）

把 `.gguf` 模型文件放进 `model/` 目录即可。面板会扫描该目录列出模型。

- 三元量化模型 → 选 **KVMem** 引擎
- 标准 GGUF 模型 → 选 **llama.cpp** 引擎
- 嵌入模型（如 embeddinggemma）→ llama.cpp + 勾选 `--embeddings`
- 重排模型（如 bge-reranker）→ llama.cpp + 勾选 `--rerank`

## 使用

- **启动模型**：面板「模型启动」卡选模型 → 选引擎 → 填参数 → 启动
- **聊天**：点右上角「聊天界面」，或用 OpenAI 兼容网关连外部工具
- **文件空间**：面板「文件空间」卡选一个文件夹，模型即可读取/写入
- **MCP 工具**：面板「MCP 服务器」卡添加 stdio 服务器（如 `cmd /c npx -y @modelcontextprotocol/server-xxx`）

## 端口

| 端口 | 用途 |
|---|---|
| 18300 | 启动器面板（本页） |
| 18401 | OpenAI 兼容网关 |
| 18200+ | 各模型实例端口（自动分配） |

## 许可与致谢

- llama-kvmem / llama.cpp：上游 LLM 引擎，遵循其各自许可
- 本项目代码：见 `licenses/`（如随附）

## 常见问题

**Q：双击 bat 没反应？**
命令行跑 `python main.py` 看报错；确认 Python 3.8+ 且已加入 PATH。

**Q：启动模型报「引擎不存在」？**
确认 `engine/` 目录里有对应引擎 exe，且 CUDA DLL 齐全。

**Q：模型文件放好了但列表没有？**
刷新面板；确认文件是 `.gguf` 且不叫 `mmproj`（视觉投影文件会被忽略）。
