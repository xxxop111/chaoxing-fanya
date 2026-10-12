# 测试

只为**纯函数**写测试 —— 这些是平台改版后会**静默失效**的解析层。

**零依赖**，不登录、不联网、不写入任何账号数据：

```bash
.venv\Scripts\python.exe tests\test_parsers.py
```
或（若已安装 pytest）：

```bash
.venv\Scripts\python.exe -m pytest tests -q
```
