# -*- coding: utf-8 -*-
import argparse
import configparser
import enum
import sys
import threading
import time
import traceback
from concurrent.futures.thread import ThreadPoolExecutor
from dataclasses import dataclass
from queue import PriorityQueue
try:
    from queue import ShutDown
except ImportError:
    # ShutDown is only available in Python 3.13+
    class ShutDown(Exception):
        pass
from threading import RLock
from typing import Any

from tqdm import tqdm

from api.answer import Tiku
from api.base import Chaoxing, Account, StudyResult
from api.exceptions import LoginError, InputFormatError
from api.logger import logger
from api.notification import Notification
from api.live import Live
from api.live_process import LiveProcessor

class ChapterResult(enum.Enum):
    SUCCESS=0,
    ERROR=1,
    NOT_OPEN=2,
    PENDING=3


def log_error(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except BaseException as e:
            logger.error(f"Error in thread {threading.current_thread().name}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)
            raise

    return wrapper


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Samueli924/chaoxing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--use-cookies", action="store_true", help="使用cookies登录")

    parser.add_argument(
        "-c", "--config", type=str, default=None, help="使用配置文件运行程序"
    )
    parser.add_argument("-u", "--username", type=str, default=None, help="手机号账号")
    parser.add_argument("-p", "--password", type=str, default=None, help="登录密码")
    parser.add_argument(
        "-l", "--list", type=str, default=None, help="要学习的课程ID列表, 以 , 分隔"
    )
    parser.add_argument(
        "-s", "--speed", type=float, default=1.0, help="视频播放倍速 (默认1, 最大2)"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=4, help="同时进行的章节数 (默认4, 如果一个章节有多个任务点，不会限制同时处理任务点的数量)"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        "--debug",
        action="store_true",
        help="启用调试模式, 输出DEBUG级别日志",
    )
    parser.add_argument(
        "-a", "--notopen-action", type=str, default="retry", 
        choices=["retry", "ask", "continue"],
        help="遇到关闭任务点时的行为: retry-重试, ask-询问, continue-继续"
    )

    # ---- 泛雅「任务」模块（任务引擎）自动化 ----
    parser.add_argument(
        "--fanya-tasks", action="store_true",
        help="处理课程「任务」标签下的任务点（AI实践/主题讨论/作业/文档/视频等），默认只盘点不执行",
    )
    parser.add_argument(
        "--fanya-execute", action="store_true",
        help="配合 --fanya-tasks：真正执行自动完成（不加则只输出盘点结果）",
    )
    parser.add_argument(
        "--fanya-plans", type=str, default=None,
        help="只处理指定的任务点 planId，以逗号分隔",
    )
    parser.add_argument(
        "--also-study", action="store_true",
        help="配合 --fanya-tasks：处理完「任务」后继续原有的章节学习",
    )

    # 在解析之前捕获 -h 的行为
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args()


def load_config_from_file(config_path):
    """从配置文件加载设置"""
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf8")
    
    common_config: dict[str, Any] = {}
    tiku_config: dict[str, Any] = {}
    notification_config: dict[str, Any] = {}
    
    # 检查并读取common节
    if config.has_section("common"):
        common_config = dict(config.items("common"))
        # 处理course_list，将字符串转换为列表
        if "course_list" in common_config and common_config["course_list"]:
            common_config["course_list"] = [item.strip() for item in common_config["course_list"].split(",") if item.strip()]
        # 处理speed，将字符串转换为浮点数
        if "speed" in common_config:
            common_config["speed"] = float(common_config["speed"])
        if "jobs" in common_config:
            common_config["jobs"] = int(common_config["jobs"])
        # 处理notopen_action，设置默认值为retry
        if "notopen_action" not in common_config:
            common_config["notopen_action"] = "retry"
        if "use_cookies" in common_config:
            common_config["use_cookies"] = str_to_bool(common_config["use_cookies"])
        if "username" in common_config and common_config["username"] is not None:
            common_config["username"] = common_config["username"].strip()
        if "password" in common_config and common_config["password"] is not None:
            common_config["password"] = common_config["password"].strip()

    # 检查并读取tiku节
    if config.has_section("tiku"):
        tiku_config = dict(config.items("tiku"))
        # 处理数值类型转换
        for key in ["delay", "cover_rate"]:
            if key in tiku_config:
                tiku_config[key] = float(tiku_config[key])

    # 检查并读取notification节
    if config.has_section("notification"):
        notification_config = dict(config.items("notification"))
    
    return common_config, tiku_config, notification_config


def build_config_from_args(args):
    """从命令行参数构建配置"""
    common_config = {
        "use_cookies": args.use_cookies,
        "username": args.username,
        "password": args.password,
        "course_list": [item.strip() for item in args.list.split(",") if item.strip()] if args.list else None,
        "speed": args.speed if args.speed else 1.0,
        "jobs": args.jobs,
        "notopen_action": args.notopen_action if args.notopen_action else "retry"
    }
    return common_config, {}, {}


def init_config(args=None):
    """初始化配置"""
    args = args if args is not None else parse_args()
    
    if args.config:
        return load_config_from_file(args.config)
    else:
        return build_config_from_args(args)


def init_chaoxing(common_config, tiku_config):
    """初始化超星实例"""
    username = common_config.get("username", "")
    password = common_config.get("password", "")
    use_cookies = common_config.get("use_cookies", False)
    
    # 如果没有提供用户名密码，从命令行获取
    if (not username or not password) and not use_cookies:
        username = input("请输入你的手机号, 按回车确认\n手机号:")
        password = input("请输入你的密码, 按回车确认\n密码:")
    
    account = Account(username, password)
    
    # 设置题库
    tiku = Tiku()
    tiku.config_set(tiku_config)  # 载入配置
    tiku = tiku.get_tiku_from_config()  # 载入题库
    tiku.init_tiku()  # 初始化题库
    
    # 获取查询延迟设置
    query_delay = tiku_config.get("delay", 0)
    # 获取AI题库并发配置（仅在使用AI题库时生效）
    ai_concurrency = tiku_config.get("ai_concurrency")
    
    # 实例化超星API
    chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=query_delay, ai_concurrency=ai_concurrency)
    
    return chaoxing


def process_job(chaoxing: Chaoxing, course: dict, job: dict, job_info: dict, speed: float, progress_callback=None) -> StudyResult:
    """处理单个任务点"""
    # 视频任务
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        # 超星的接口没有返回当前任务是否为Audio音频任务
        video_result = chaoxing.study_video(
            course, job, job_info, _speed=speed, _type="Video", progress_callback=progress_callback
        )
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = chaoxing.study_video(
                course, job, job_info, _speed=speed, _type="Audio", progress_callback=progress_callback)
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    # 文档任务
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return chaoxing.study_document(course, job)
    # 测验任务
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        return chaoxing.study_work(course, job, job_info)
    # 阅读任务
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return chaoxing.study_read(course, job, job_info)
    # 直播任务
    elif job["type"] == "live":
        logger.trace(f"识别到直播任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        try:
            # 准备直播所需参数
            defaults = {
                "userid": chaoxing.get_uid(),
                "clazzId": course.get("clazzId"),
                "knowledgeid": job_info.get("knowledgeid")
            }
            
            # 创建直播对象
            live = Live(
                attachment=job,
                defaults=defaults,
                course_id=course.get("courseId")
            )
            
            # 启动直播处理线程
            thread = threading.Thread(
                target=LiveProcessor.run_live,
                args=(live, speed),
                daemon=True
            )
            thread.start()
            thread.join()  # 等待直播处理完成
            return StudyResult.SUCCESS
        except Exception as e:
            logger.error(f"处理直播任务时出错: {str(e)}")
            return StudyResult.ERROR

    logger.error(f"未知任务类型: {job['type']}")
    return StudyResult.ERROR


@dataclass(order=True)
class ChapterTask:
    index: int
    point: dict[str, Any]
    result: ChapterResult = ChapterResult.PENDING
    tries: int = 0

class JobProcessor:
    def __init__(self, chaoxing: Chaoxing, course: dict[str, Any], tasks: list[ChapterTask], config: dict[str, Any]):
        self.chaoxing = chaoxing
        self.course = course
        self.speed = config["speed"]
        self.max_tries = 5
        self.tasks = tasks
        self.failed_tasks: list[ChapterTask] = []
        self.task_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.retry_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.wait_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.threads: list[threading.Thread] = []
        self.worker_num = config["jobs"]
        self.config = config
        # 优雅停止信号：置位后不再领取新章节，正在跑的章节收尾后退出
        self.stop_event = (config or {}).get("stop_event")
        self.stopped_tasks: list[ChapterTask] = []

    def _stop_requested(self) -> bool:
        """是否已请求停止（来自前端的「停止」按钮）。"""
        return self.stop_event is not None and self.stop_event.is_set()

    def run(self):
        for task in self.tasks:
            self.task_queue.put(task)

        for i in range(self.worker_num):
            thread = threading.Thread(target=self.worker_thread, daemon=True)
            self.threads.append(thread)
            thread.start()

        threading.Thread(target=self.retry_thread, daemon=True).start()

        self.task_queue.join()
        time.sleep(0.5)

        if self._stop_requested():
            logger.warning(
                "停止请求生效：已处理 {} 个章节，剩余 {} 个章节未开始",
                len(self.tasks) - len(self.stopped_tasks), len(self.stopped_tasks),
            )


    @log_error
    def worker_thread(self):
        tqdm.set_lock(tqdm.get_lock())
        while True:
            try:
                task = self.task_queue.get()
            except ShutDown:
                logger.info("Queue shut down")
                return

            # 优雅停止：不再开始新章节。这里依然 task_done() 把队列消费干净，
            # 使 task_queue.join() 能正常返回，而不是硬杀工作线程。
            if self._stop_requested():
                self.stopped_tasks.append(task)
                logger.info("已请求停止，跳过未开始的章节: {}", task.point.get("title"))
                self.task_queue.task_done()
                continue

            # 处理单个章节，并在需要时通过 config 中的回调上报章节完成进度
            task.result = process_chapter(self.chaoxing, self.course, task.point, self.speed, self.config)

            match task.result:
                case ChapterResult.SUCCESS:
                    logger.debug("Task success: {}", task.point["title"])
                    self.task_queue.task_done()
                    logger.debug(f"unfinished task: {self.task_queue.unfinished_tasks}")

                case ChapterResult.NOT_OPEN:
                    # task.tries += 1
                    if self.config["notopen_action"] == "continue":
                        logger.warning("章节未开启: {}, 正在跳过", task.point["title"])
                        self.task_queue.task_done()
                        continue

                    if task.tries >= self.max_tries:
                        logger.error(
                            "章节未开启: {} 可能由于上一章节的章节检测未完成, 也可能由于该章节因为时效已关闭，"
                            "请手动检查完成并提交再重试。或者在配置中配置(自动跳过关闭章节/开启题库并启用提交)"
                        , task.point["title"])
                        self.task_queue.task_done()
                        continue

                    # self.wait_queue.put(task)
                    self.retry_queue.put(task)

                case ChapterResult.ERROR:
                    task.tries += 1
                    logger.warning("Retrying task {} ({}/{} attempts)", task.point["title"], task.tries,
                                   self.max_tries)
                    if task.tries >= self.max_tries:
                        logger.error("Max retries reached for task: {}", task.point["title"])
                        self.failed_tasks.append(task)
                        self.task_queue.task_done()
                        continue
                    self.retry_queue.put(task)

                case _:
                    logger.error("Invalid task state {} for task {}", task.result, task.point["title"])
                    self.failed_tasks.append(task)
                    self.task_queue.task_done()

    @log_error
    def retry_thread(self):
        try:
            while True:
                task = self.retry_queue.get()
                self.task_queue.put(task)
                self.task_queue.task_done() # task_done is not called when a task failed and needs to be retried, so if is reput into the queue, the task num will increase by one and become more than the real task number
                time.sleep(1)
        except ShutDown:
            pass


def process_chapter(chaoxing: Chaoxing, course:dict[str, Any], point:dict[str, Any], speed:float, config: dict[str, Any] | None = None) -> ChapterResult:
    """处理单个章节

    当所有任务点成功完成时，如果 config 中提供了 chapter_done_callback，
    则回调通知外部（如 Web 端）更新进度统计。
    """
    logger.info(f'当前章节: {point["title"]}')

    # 通知外部当前章节开始（用于前端显示当前正在学习的章节）
    if config is not None:
        start_cb = config.get("chapter_start_callback")
        if callable(start_cb):
            try:
                start_cb(course, point)
            except Exception as e:
                logger.debug(f"调用 chapter_start_callback 时出错: {e}")
    if point["has_finished"]:
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        # 已经在超星端标记为完成的章节，这里直接视为成功，但不再重复回调
        return ChapterResult.SUCCESS
    
    # 随机等待，避免请求过快
    chaoxing.rate_limiter.limit_rate(random_time=True,random_min=0, random_max=0.2)
    
    # 获取当前章节的所有任务点
    job_info = None
    jobs, job_info = chaoxing.get_job_list(course, point)

    # 发现未开放章节, 根据配置处理
    if job_info.get("notOpen", False):
        return ChapterResult.NOT_OPEN

    # 已经默认处理空任务，此处不需要判断
    if not jobs:
        pass

    # TODO: 个别章节很恶心，多到5个点，可以并行处理，将来会让不同课程不同章节的所有任务点共享一个队列，从而实现全局并行
    job_results:list[StudyResult]=[]
    video_progress_callback = config.get("video_progress_callback") if config else None
    with ThreadPoolExecutor(max_workers=5) as executor:
        for result in executor.map(lambda job: process_job(chaoxing, course, job, job_info, speed, progress_callback=video_progress_callback), jobs):
            job_results.append(result)
    
    for result in job_results:
        if result.is_failure():
            return ChapterResult.ERROR

    # 所有任务点均成功，通知外部本章节已完成（用于前端进度统计）
    if config is not None:
        callback = config.get("chapter_done_callback")
        if callable(callback):
            try:
                callback(course, point)
            except Exception as e:
                logger.debug(f"调用 chapter_done_callback 时出错: {e}")

    return ChapterResult.SUCCESS



def process_course(chaoxing: Chaoxing, course:dict[str, Any], config: dict):
    """处理单个课程"""
    logger.info(f"开始学习课程: {course['title']}")
    
    # 获取当前课程的所有章节
    point_list = chaoxing.get_course_point(
        course["courseId"], course["clazzId"], course["cpi"]
    )

    # 为了支持课程任务回滚, 采用下标方式遍历任务点

    _old_format_sizeof = tqdm.format_sizeof
    tqdm.format_sizeof = format_time
    tqdm.set_lock(RLock())

    tasks=[]

    for i, point in enumerate(point_list["points"]):
        task = ChapterTask(point=point, index=i)
        tasks.append(task)

    # 章节上限：只处理前 N 章（按章节序号），其余章节完全不请求、不修改。
    # 注意已完成的章节也算在序号里 —— 它们会被瞬间跳过，但不占用“新刷”的额度。
    max_chapters = (config or {}).get("max_chapters")
    if max_chapters:
        try:
            max_chapters = int(max_chapters)
        except (TypeError, ValueError):
            max_chapters = 0
        if max_chapters > 0 and len(tasks) > max_chapters:
            logger.warning(
                "章节上限 {}: 本课程共 {} 章，本次只处理前 {} 章，剩余 {} 章保持原样",
                max_chapters, len(tasks), max_chapters, len(tasks) - max_chapters,
            )
            tasks = tasks[:max_chapters]

    p = JobProcessor(chaoxing, course, tasks, config)
    p.run()


    tqdm.format_sizeof = _old_format_sizeof

    """
    while __point_index < len(point_list["points"]):
        point = point_list["points"][__point_index]
        logger.debug(f"当前章节 __point_index: {__point_index}")
        
        result, auto_skip_notopen = process_chapter(
            chaoxing, course, point, RB, notopen_action, speed, auto_skip_notopen
        )
        
        if result == -1:  # 退出当前课程
            break
        elif result == 0:  # 重试前一章节
            __point_index -= 1  # 默认第一个任务总是开放的
        else:  # 继续下一章节
            __point_index += 1
    """



def filter_courses(all_course, course_list):
    """过滤要学习的课程"""
    if not course_list:
        # 手动输入要学习的课程ID列表
        print("*" * 10 + "课程列表" + "*" * 10)
        for course in all_course:
            print(f"ID: {course['courseId']} 课程名: {course['title']}")
        print("*" * 28)
        try:
            course_list = input(
                "请输入想要学习的课程列表,以逗号分隔,例: 2151141,189191,198198\n"
            ).split(",")
        except Exception as e:
            raise InputFormatError("输入格式错误") from e

    # 筛选需要学习的课程
    course_task = []
    course_ids = []
    for course in all_course:
        if course["courseId"] in course_list and course["courseId"] not in course_ids:
            course_task.append(course)
            course_ids.append(course["courseId"])
    
    # 如果没有指定课程，则学习所有课程
    if not course_task:
        course_task = all_course
    
    return course_task


def format_time(num, suffix='', divisor=''):
    total_time = round(num)
    sec = total_time % 60
    mins = (total_time % 3600) // 60
    hrs = total_time // 3600

    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{sec:02d}"

    return f"{mins:02d}:{sec:02d}"


def _fmt_progress(value) -> str:
    """任务进度格式化。

    多数任务返回 0~1 的比例，但实测个别任务（COM0018A 的项目1/项目2）返回的是
    8.33 这样的百分数，一律乘 100 会显示成「833%」。
    """
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if 0.0 <= num <= 1.0:
        return f"{num * 100:.1f}%"
    return f"{num:g}（服务端原始值，疑似百分数）"


def run_fanya_tasks(chaoxing: Chaoxing, course_task: list, common_config: dict,
                    tiku_config: dict, execute: bool, plan_ids: list[int] | None) -> None:
    """处理课程「任务」模块（任务引擎）下的任务点。

    默认只做只读盘点并把结果打印出来；加 ``--fanya-execute`` 才真正执行。
    """
    from api.fanya_task import (
        ContentGenerator, FanyaTaskClient, FanyaTaskRunner, build_task_preview,
    )

    client = FanyaTaskClient()

    if not execute:
        logger.info("开始盘点课程「任务」模块（只读，不会修改任何学习记录）")
        preview = build_task_preview(chaoxing, course_task, client=client)
        summary = preview["summary"]
        print("\n" + "=" * 72)
        print(f"课程「任务」盘点结果：{summary['courseCount']} 门课程存在任务，"
              f"{summary['taskCount']} 个任务")
        print(f"待完成任务点 {summary['pendingCount']} 个："
              f"可自动完成 {summary['completableCount']} 个，"
              f"需人工 {summary['manualCount']} 个，"
              f"暂未支持 {summary['unsupportedCount']} 个")
        print("=" * 72)
        for course in preview["courses"]:
            if not course["hasTaskEngine"]:
                continue
            print(f"\n■ {course['title']}  ({course['courseId']})")
            if course.get("error"):
                print(f"    ! {course['error']}")
            for task in course["tasks"]:
                print(f"  · {task['name']}  进度 {_fmt_progress(task['progress'])}"
                      f"{'' if task['allowStudy'] else '  [不在学习时间内]'}")
                for item in task["items"]:
                    if item["isFinish"]:
                        continue
                    mark = {"auto": "√", "partial": "△", "manual": "×",
                            "pending": "?"}.get(item["status"], "?")
                    print(f"      {mark} [{item['typeLabel']}] {item['name'][:48]}"
                          f"  — {item['note'] or item['skipReason']}")
        print("\n提示：加上 --fanya-execute 才会真正执行自动完成。\n")
        return

    generator = ContentGenerator.from_tiku_config(tiku_config)
    if generator.available:
        logger.info(f"作答内容生成已启用：{generator.model}")
    else:
        logger.warning("未配置 AI 接口，需要文字作答的任务将使用模板文本")

    runner = FanyaTaskRunner(chaoxing=chaoxing, client=client, generator=generator)

    if plan_ids:
        wanted = {int(x) for x in plan_ids}
        logger.info(f"只处理指定的 {len(wanted)} 个任务点")
        for course in course_task:
            try:
                tasks = client.get_course_tasks(course["courseId"], course["clazzId"])
            except Exception as exc:
                logger.error(f"课程「{course.get('title')}」任务读取失败: {exc}")
                continue
            for task in tasks:
                runner.run_task(course, task, only_plan_ids=list(wanted))
    else:
        runner.run_courses(course_task)

    results = runner.results
    ok = len([r for r in results if r["ok"]])
    skipped = len([r for r in results if r["skipped"]])
    failed = len(results) - ok - skipped
    logger.info(f"「任务」模块处理结束：成功 {ok} 个，失败 {failed} 个，跳过 {skipped} 个")
    for r in results:
        if not r["ok"] and not r["skipped"]:
            logger.warning(f"未完成：{r['courseTitle']} / {r['taskName']} / "
                           f"{r['name']} — {r['message']}")


def main():
    """主程序入口"""
    try:
        # 初始化配置
        args = parse_args()
        common_config, tiku_config, notification_config = init_config(args)
        
        # 强制播放按照配置文件调节
        common_config["speed"] = min(2.0, max(1.0, common_config.get("speed", 1.0)))
        common_config["notopen_action"] = common_config.get("notopen_action", "retry")
        
        # 初始化超星实例
        chaoxing = init_chaoxing(common_config, tiku_config)
        
        # 设置外部通知
        notification = Notification()
        notification.config_set(notification_config)
        notification = notification.get_notification_from_config()
        notification.init_notification()
        
        # 检查当前登录状态
        _login_state = chaoxing.login(login_with_cookies=common_config.get("use_cookies", False))
        if not _login_state["status"]:
            raise LoginError(_login_state["msg"])
        
        # 获取所有的课程列表
        all_course = chaoxing.get_course_list()
        
        # 过滤要学习的课程
        course_task = filter_courses(all_course, common_config.get("course_list"))
        
        # 处理课程「任务」模块（任务引擎：AI实践 / 主题讨论 / 作业 / 文档 / 视频 …）
        if getattr(args, "fanya_tasks", False):
            plan_ids = None
            if args.fanya_plans:
                try:
                    plan_ids = [int(x.strip()) for x in args.fanya_plans.split(",") if x.strip()]
                except ValueError:
                    raise InputFormatError("--fanya-plans 需要以逗号分隔的数字 planId")
            run_fanya_tasks(
                chaoxing, course_task, common_config, tiku_config,
                execute=bool(args.fanya_execute), plan_ids=plan_ids,
            )
            if not args.also_study:
                logger.info("「任务」模块处理完毕（未指定 --also-study，跳过章节学习）")
                return
        
        # 开始学习
        logger.info(f"课程列表过滤完毕, 当前课程任务数量: {len(course_task)}")
        for course in course_task:
            process_course(chaoxing, course, common_config)
        
        logger.info("所有课程学习任务已完成")
        notification.send("chaoxing : 所有课程学习任务已完成")
        
    except SystemExit as e:
        if e.code != 0:
            logger.error(f"错误: 程序异常退出, 返回码: {e.code}")
        sys.exit(e.code)
    except KeyboardInterrupt as e:
        logger.error(f"错误: 程序被用户手动中断, {e}")
    except BaseException as e:
        logger.error(f"错误: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        try:
            notification.send(f"chaoxing : 出现错误 {type(e).__name__}: {e}\n{traceback.format_exc()}")
        except Exception:
            pass  # 如果通知发送失败，忽略异常
        raise e


if __name__ == "__main__":
    main()
