from loguru import logger
from tqdm import tqdm
import sys

# 打包成「无控制台」的桌面应用时 sys.stderr 会是 None，
# 此时所有控制台输出都必须静默跳过，只保留文件日志（chaoxing.log）。
tqdm_stream = sys.stderr


def tqdm_sink(msg):
    stream = tqdm_stream
    if stream is None:
        return
    try:
        tqdm.write(msg.rstrip(), file=stream)
        stream.flush()
    except Exception:  # noqa: BLE001 - 控制台不可用时绝不能让日志把主流程带崩
        pass


logger.remove()
if tqdm_stream is not None:
    logger.add(tqdm_sink, colorize=True, enqueue=True)
logger.add("chaoxing.log", rotation="10 MB", level="TRACE")
