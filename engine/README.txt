把引擎二进制放进这个目录（体积大，不随代码分发，需自行下载/复制）：

| 文件 | 用途 | 必需 |
|---|---|---|
| llama-kvmem-server.exe | KVMem 引擎（三元量化 PTQ1_0） | 选装 |
| llama-kvmem-cli.exe   | KVMem 命令行工具 | 选装 |
| llama-server.exe      | llama.cpp 官方引擎（标准 GGUF） | 选装 |
| cudart64_13.dll       | CUDA 运行时 | GPU 推理必需 |
| cublas64_13.dll       | cuBLAS 运行时 | GPU 推理必需 |
| cublasLt64_13.dll     | cuBLASLt 运行时 | GPU 推理必需 |

说明：
- llama-server.exe 也可放在别处，在启动面板里指定路径，或在 models-config.json 的 llama_exe 里配置。
- 只用 CPU 推理则不需要 CUDA 的 3 个 dll。
