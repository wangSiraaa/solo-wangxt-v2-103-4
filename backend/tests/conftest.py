import os
import tempfile

# 在导入应用前把数据库指到临时 SQLite，避免污染本地文件
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
