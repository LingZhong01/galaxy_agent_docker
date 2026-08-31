# Galaxy Agent GPU image

该工程生成一个完整的 Galaxy Agent GPU 镜像，包含 communication service、
galaxy morphology MCP、GalfitS、GALFIT、Codex CLI 和 Claude CLI。

MCP、GalfitS 和结果后处理统一运行在同一个 Python 3.11 环境中。GPU 栈参考已验证的
`galaxy_morphology_mcp:0.0.15`，使用 JAX 0.8.0、CUDA 12 插件、CUPTI 和 NVCC/PTXAS。

构建和部署说明已分开维护：

- [BUILD.md](BUILD.md)：基于 Dockerfile 构建、验证及导出镜像。
- [DEPLOY.md](DEPLOY.md)：导入配置和认证、启动服务、检查日志及停止服务。

当前验证通过的本地镜像：

```text
galaxy-agent:gpu-local
```

