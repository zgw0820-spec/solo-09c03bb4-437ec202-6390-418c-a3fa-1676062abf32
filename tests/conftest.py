import os
import tempfile

# 模块级 app 在导入 app.main 时即创建；把 DATA_DIR 指到临时目录，避免污染仓库。
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="diskrepair-itest-"))
