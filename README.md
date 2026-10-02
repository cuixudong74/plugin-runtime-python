# plugin-runtime-python

在现有 FastAPI 服务之上的可插拔能力运行时：每项能力以插件形式独立安装、启用、
停用与升级，依赖与版本自动对齐，全程无需重启服务。

- 插件运行时源码：`plugin_runtime/`
- 清单格式、版本解析规则与生命周期语义：见 [docs/plugins.md](docs/plugins.md)
- 测试：`uv run python -m pytest tests/ -q`
