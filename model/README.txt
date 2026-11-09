把 .gguf 模型文件放进这个目录，启动面板会自动扫描列出。

- 三元量化模型（PTQ1_0 等）→ 选 KVMem 引擎
- 标准 GGUF → 选 llama.cpp 引擎
- 嵌入模型（如 embeddinggemma）→ llama.cpp + 勾选 --embeddings
- 重排模型（如 bge-reranker）→ llama.cpp + 勾选 --rerank
- 视觉投影文件（mmproj）也会放这里，面板会自动忽略它们
