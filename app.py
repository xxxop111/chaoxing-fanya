import os
import sys
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
import threading
import queue
import time
import json
from typing import Dict, List

# 确定静态文件目录（支持便携版 / PyInstaller 打包版）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from api.paths import app_path, bundle_path

# 只读的前端构建产物跟着程序走；可写配置放在 exe / 项目根目录
STATIC_DIR = bundle_path("web", "dist")

from api.base import Chaoxing, Account, StudyResult, disambiguate_course_titles
from api.answer import Tiku
from api.exceptions import LoginError
from api.logger import logger
import main as main_module

# 如果存在构建好的前端，则使用静态文件服务
if os.path.exists(STATIC_DIR):
    app = Flask(__name__, static_folder=STATIC_DIR, static_url_path='')
else:
    app = Flask(__name__)

CORS(app)

# Web 配置文件路径（可写：打包后位于 exe 同目录）
CONFIG_FILE = app_path("web_config.json")


def load_web_config() -> Dict:
  """加载前端保存的配置"""
  if not os.path.exists(CONFIG_FILE):
      return {}
  try:
      # utf-8-sig：容忍文件带 BOM。Windows 上用 PowerShell 的
      # `Set-Content -Encoding UTF8` 写文件会插入 BOM，用 utf-8 读会抛
      # "Unexpected UTF-8 BOM"，导致**整份配置丢失**（题库配置随之变空，
      # 盘点时会在 Tiku 初始化处 KeyError: 'model'）。
      with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
          return json.load(f)
  except Exception as e:
      logger.error(f"读取 Web 配置失败: {e}")
      return {}


def save_web_config(data: Dict) -> bool:
  """保存前端配置到本地 JSON 文件"""
  try:
      tmp_path = CONFIG_FILE + ".tmp"
      with open(tmp_path, "w", encoding="utf-8") as f:
          json.dump(data, f, ensure_ascii=False, indent=2)
      os.replace(tmp_path, CONFIG_FILE)
      return True
  except Exception as e:
      logger.error(f"保存 Web 配置失败: {e}")
      return False


# 存储学习任务状态
task_status: Dict[str, dict] = {}
log_queue = queue.Queue()

# 任务详细信息缓存
task_details: Dict[str, dict] = {}

# 每个任务一个停止信号。置位后：不再领取新章节，正在跑的章节做完即退出。
# 对应前端的「停止」按钮（POST /api/task/<task_id>/stop）。
task_stop_events: Dict[str, threading.Event] = {}


def is_stop_requested(task_id: str) -> bool:
    ev = task_stop_events.get(task_id)
    return bool(ev is not None and ev.is_set())

class LogCapture:
    """捕获日志输出"""
    def __init__(self, task_id: str):
        self.task_id = task_id
        self.logs = []
    
    def write(self, message):
        # loguru 传入的是 Message 对象，这里统一转成字符串再处理
        text = str(message).strip()
        if text:
            self.logs.append({
                'time': time.time(),
                'message': text
            })
            log_queue.put({
                'task_id': self.task_id,
                'message': text
            })

@app.route('/api/login', methods=['POST'])
def login():
    """用户登录接口"""
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        use_cookies = data.get('use_cookies', False)
        
        if not username or not password:
            return jsonify({'status': False, 'msg': '用户名或密码不能为空'}), 400
        
        account = Account(username, password)
        tiku = Tiku()
        chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=0)
        
        login_result = chaoxing.login(login_with_cookies=use_cookies)
        
        if login_result['status']:
            # 保存登录状态到session
            return jsonify({
                'status': True,
                'msg': '登录成功',
                'data': {
                    'username': username
                }
            })
        else:
            return jsonify({'status': False, 'msg': login_result['msg']}), 401
            
    except Exception as e:
        logger.error(f"登录错误: {e}")
        return jsonify({'status': False, 'msg': str(e)}), 500

@app.route('/api/courses', methods=['POST'])
def get_courses():
    """获取课程列表"""
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        use_cookies = data.get('use_cookies', False)
        
        account = Account(username, password)
        tiku = Tiku()
        chaoxing = Chaoxing(account=account, tiku=tiku, query_delay=0)
        
        login_result = chaoxing.login(login_with_cookies=use_cookies)
        if not login_result['status']:
            return jsonify({'status': False, 'msg': '登录失败'}), 401
        
        courses = chaoxing.get_course_list()
        
        return jsonify({
            'status': True,
            # 同一门课教多个班时标题相同，加教师名区分（见 api.base）
            'data': disambiguate_course_titles(courses),
        })
        
    except Exception as e:
        logger.error(f"获取课程列表错误: {e}")
        return jsonify({'status': False, 'msg': str(e)}), 500


@app.route('/api/config', methods=['GET', 'POST'])
def web_config():
    if request.method == 'GET':
        data = load_web_config()
        return jsonify({'status': True, 'data': data})

    data = request.json
    if not isinstance(data, dict):
        return jsonify({'status': False, 'msg': '配置格式错误'}), 400

    if not save_web_config(data):
        return jsonify({'status': False, 'msg': '保存失败'}), 500

    return jsonify({'status': True, 'msg': '保存成功'})

@app.route('/api/start', methods=['POST'])
def start_study():
    """开始学习任务"""
    try:
        data = request.json
        username = data.get('username')
        password = data.get('password')
        course_list = data.get('course_list', [])
        speed = float(data.get('speed', 1.0))
        jobs = int(data.get('jobs', 4))
        notopen_action = data.get('notopen_action', 'retry')
        # 只刷前 N 章：0 / 空 / 非数字 都表示不限制
        try:
            max_chapters = int(data.get('max_chapters') or 0)
        except (TypeError, ValueError):
            max_chapters = 0
        if max_chapters < 0:
            max_chapters = 0
        tiku_config = data.get('tiku_config', {})
        notification_config = data.get('notification_config', {})
        ocr_config = data.get('ocr_config', {})
        
        # 设置 OCR 配置环境变量
        if ocr_config.get('provider') and ocr_config.get('key'):
            os.environ['CHAOXING_VISION_OCR_PROVIDER'] = ocr_config['provider']
            os.environ['CHAOXING_VISION_OCR_KEY'] = ocr_config['key']
            if ocr_config.get('endpoint'):
                os.environ['CHAOXING_VISION_OCR_ENDPOINT'] = ocr_config['endpoint']
            if ocr_config.get('model'):
                os.environ['CHAOXING_VISION_OCR_MODEL'] = ocr_config['model']
            # 重置 vision_ocr 模块的配置缓存，使新配置生效
            try:
                from api.vision_ocr import reset_vision_ocr_config
                reset_vision_ocr_config()
            except ImportError:
                pass
            logger.info(f"已配置外部 AI 视觉 OCR: provider={ocr_config['provider']}")
        
        # 创建任务ID
        task_id = f"{username}_{int(time.time())}"
        
        # 初始化任务状态
        task_status[task_id] = {
            'status': 'running',
            'progress': 0,
            'total': 0,
            'current_course': '',
            'current_chapter': '',
            'current_task': '',
            'logs': [],
            'start_time': time.time(),
            'stats': {
                'total_chapters': 0,
                'completed_chapters': 0,
                'total_tasks': 0,
                'completed_tasks': 0,
                'failed_tasks': 0,
                'skipped_tasks': 0
            }
        }
        
        # 初始化任务详细信息
        task_details[task_id] = {
            'courses': [],  # 存储每个课程的详细信息
            'active_jobs': {}  # 存储活跃的视频任务进度
        }
        
        # 构建配置
        stop_event = threading.Event()
        task_stop_events[task_id] = stop_event
        common_config = {
            'username': username,
            'password': password,
            'course_list': course_list,
            'speed': min(2.0, max(1.0, speed)),
            'jobs': jobs,
            'notopen_action': notopen_action,
            'use_cookies': False,
            'max_chapters': max_chapters,
            'stop_event': stop_event,
        }
        if max_chapters > 0:
            logger.info(f"已设置章节上限：每门课只处理前 {max_chapters} 章")
        
        # 在后台线程中运行学习任务
        def run_task():
            from api.notification import Notification
            import traceback
            
            notification = None
            # 为当前任务注册日志捕获，将 loguru 日志通过 LogCapture 推送到前端
            capture = LogCapture(task_id)
            log_sink_id = logger.add(capture.write, enqueue=True)
            
            def update_video_progress(course, job, current_time, duration):
                """更新视频播放进度"""
                try:
                    if task_id in task_details:
                        task_details[task_id]['active_jobs'][job['jobid']] = {
                            'course_name': course['title'],
                            'job_name': job.get('name', '未知任务'),
                            'current_time': current_time,
                            'duration': duration,
                            'progress': (current_time / duration * 100) if duration > 0 else 0,
                            'timestamp': time.time()
                        }
                        
                        # 清理太久没有更新的任务（例如已完成或异常退出的）
                        current_ts = time.time()
                        expired_jobs = []
                        for jid, info in task_details[task_id]['active_jobs'].items():
                            if current_ts - info['timestamp'] > 10:  # 10秒无更新视为非活跃
                                expired_jobs.append(jid)
                        for jid in expired_jobs:
                            task_details[task_id]['active_jobs'].pop(jid, None)
                except Exception:
                    pass

            try:
                # 初始化超星实例
                chaoxing = main_module.init_chaoxing(common_config, tiku_config)

                # 把停止信号挂到实例上，便于 study_video 的长视频循环中途退出
                chaoxing.stop_event = common_config.get('stop_event')
                
                # 注入回调
                common_config['video_progress_callback'] = update_video_progress
                login_result = chaoxing.login(login_with_cookies=False)
                
                if not login_result['status']:
                    task_status[task_id]['status'] = 'error'
                    task_status[task_id]['error'] = '登录失败'
                    return
                
                # 设置外部通知
                notification = Notification()
                notification.config_set(notification_config)
                notification = notification.get_notification_from_config()
                notification.init_notification()
                
                all_courses = chaoxing.get_course_list()
                course_task = main_module.filter_courses(all_courses, course_list)

                task_status[task_id]['total'] = len(course_task)

                # 初始化课程详情
                for course in course_task:
                    task_details[task_id]['courses'].append({
                        'id': course['courseId'],
                        'title': course['title'],
                        'status': 'pending',
                        'chapters': [],
                        'start_time': None,
                        'end_time': None
                    })

                # 为 main.process_course 配置章节开始/完成的回调，用于实时更新章节与任务统计
                def chapter_start_callback(course_obj, point_obj):
                    try:
                        # 更新当前课程与章节名称，便于前端展示“正在学习”信息
                        task_status[task_id]['current_course'] = course_obj.get('title', task_status[task_id].get('current_course', ''))
                        task_status[task_id]['current_chapter'] = point_obj.get('title', '')
                    except Exception as e:
                        logger.debug(f"更新当前章节状态失败: {e}")

                def chapter_done_callback(course_obj, point_obj):
                    try:
                        if task_id not in task_status or task_id not in task_details:
                            return

                        stats = task_status[task_id]['stats']
                        courses_detail = task_details[task_id]['courses']
                        course_id = course_obj.get('courseId')

                        target_course = None
                        for c in courses_detail:
                            if c.get('id') == course_id:
                                target_course = c
                                break

                        if not target_course:
                            return

                        chapters = target_course.get('chapters', [])
                        for chapter in chapters:
                            if chapter.get('id') == point_obj.get('id'):
                                if not chapter.get('has_finished'):
                                    chapter['has_finished'] = True
                                    chapter['status'] = 'completed'
                                    try:
                                        job_count = int(chapter.get('jobCount', 1) or 1)
                                    except (TypeError, ValueError):
                                        job_count = 1
                                    stats['completed_chapters'] += 1
                                    stats['completed_tasks'] += job_count
                                else:
                                    # 已完成章节统一标记为 completed
                                    chapter['status'] = 'completed'
                                break
                    except Exception as e:
                        logger.debug(f"更新章节统计失败: {e}")

                # 将回调注入 common_config，供 main.process_course / process_chapter 使用
                common_config['chapter_start_callback'] = chapter_start_callback
                common_config['chapter_done_callback'] = chapter_done_callback

                for idx, course in enumerate(course_task):
                    course_detail = task_details[task_id]['courses'][idx]
                    course_detail['status'] = 'running'
                    course_detail['start_time'] = time.time()
                    
                    task_status[task_id]['current_course'] = course['title']
                    task_status[task_id]['progress'] = idx
                    
                    # 获取章节信息并处理
                    try:
                        point_list = chaoxing.get_course_point(
                            course['courseId'], course['clazzId'], course['cpi']
                        )

                        points = point_list.get('points', [])
                        stats = task_status[task_id]['stats']

                        # 统计总章节数
                        stats['total_chapters'] += len(points)

                        # 记录章节信息与任务数量
                        for point in points:
                            has_finished = point.get('has_finished', False)
                            # jobCount 来自 decode_course_point，表示章节内任务数量，缺失时按 1 计
                            try:
                                job_count = int(point.get('jobCount', 1) or 1)
                            except (TypeError, ValueError):
                                job_count = 1

                            chapter_info = {
                                'id': point.get('id'),
                                'title': point.get('title', ''),
                                'status': 'completed' if has_finished else 'pending',
                                'has_finished': has_finished,
                                'jobCount': job_count,
                            }
                            course_detail['chapters'].append(chapter_info)

                            # 累计任务统计
                            stats['total_tasks'] += job_count
                            if has_finished:
                                stats['completed_chapters'] += 1
                                stats['completed_tasks'] += job_count

                        # 处理课程（原有逻辑，但章节/任务统计通过回调实时更新）
                        main_module.process_course(chaoxing, course, common_config)

                        # 课程处理完成后，确保已完成的章节状态为 completed（统计已在回调中完成）
                        for chapter in course_detail['chapters']:
                            if chapter.get('has_finished'):
                                chapter['status'] = 'completed'

                        course_detail['status'] = 'completed'
                        course_detail['end_time'] = time.time()

                    except Exception as course_error:
                        logger.error(f"课程处理失败 {course['title']}: {course_error}")
                        course_detail['status'] = 'error'
                        course_detail['error'] = str(course_error)
                        course_detail['end_time'] = time.time()
                    
                    task_status[task_id]['progress'] = idx + 1
                
                if is_stop_requested(task_id):
                    task_status[task_id]['status'] = 'stopped'
                    logger.warning(
                        "任务已按停止请求退出：当前章节已收尾，未开始的章节保持原样（下次可继续）"
                    )
                else:
                    task_status[task_id]['status'] = 'completed'

                # 发送完成通知
                if notification:
                    if is_stop_requested(task_id):
                        notification.send("超星学习通: 学习任务已按请求停止")
                    else:
                        notification.send("超星学习通: 所有课程学习任务已完成")
                
            except Exception as e:
                logger.error(f"任务执行错误: {e}")
                task_status[task_id]['status'] = 'error'
                task_status[task_id]['error'] = str(e)
                
                # 发送错误通知
                if notification:
                    try:
                        notification.send(f"超星学习通: 出现错误 {type(e).__name__}: {e}\n{traceback.format_exc()}")
                    except Exception:
                        pass
            finally:
                # 无论任务成功或失败，都移除当前任务的日志捕获 sink
                try:
                    logger.remove(log_sink_id)
                except Exception:
                    pass
        
        thread = threading.Thread(target=run_task, daemon=True)
        thread.start()
        
        return jsonify({
            'status': True,
            'data': {
                'task_id': task_id
            }
        })
        
    except Exception as e:
        logger.error(f"启动任务错误: {e}")
        return jsonify({'status': False, 'msg': str(e)}), 500

@app.route('/api/task/<task_id>', methods=['GET'])
def get_task_status(task_id):
    """获取任务状态"""
    if task_id not in task_status:
        return jsonify({'status': False, 'msg': '任务不存在'}), 404
    
    return jsonify({
        'status': True,
        'data': task_status[task_id]
    })

@app.route('/api/task/<task_id>/stop', methods=['POST'])
def stop_task(task_id):
    """请求停止任务（优雅停止：不再开始新章节，当前章节收尾后退出）"""
    if task_id not in task_status:
        return jsonify({'status': False, 'msg': '任务不存在'}), 404

    state = task_status[task_id].get('status')
    if state != 'running':
        return jsonify({'status': False, 'msg': f'任务当前状态为 {state}，无需停止'}), 400

    stop_event = task_stop_events.get(task_id)
    if stop_event is None:
        return jsonify({'status': False, 'msg': '该任务没有停止信号（可能是旧版本启动的）'}), 400

    stop_event.set()
    task_status[task_id]['stop_requested'] = True
    task_status[task_id]['current_chapter'] = task_status[task_id].get('current_chapter', '')
    logger.warning("收到停止请求：将不再开始新章节，当前章节收尾后退出")
    return jsonify({
        'status': True,
        'msg': '已请求停止，当前章节收尾后退出',
        'data': {'task_id': task_id, 'stop_requested': True},
    })


@app.route('/api/task/<task_id>/details', methods=['GET'])
def get_task_details(task_id):
    """获取任务详细信息"""
    if task_id not in task_details:
        return jsonify({'status': False, 'msg': '任务详情不存在'}), 404
    
    return jsonify({
        'status': True,
        'data': task_details[task_id]
    })

@app.route('/api/logs/<task_id>', methods=['GET'])
def get_logs(task_id):
    """获取任务日志"""
    logs = []
    while not log_queue.empty():
        log_entry = log_queue.get()
        if log_entry['task_id'] == task_id:
            # 分析日志级别
            message = log_entry['message']
            level = 'info'
            if 'ERROR' in message.upper() or '错误' in message or '失败' in message:
                level = 'error'
            elif 'WARNING' in message.upper() or '警告' in message:
                level = 'warning'
            elif 'SUCCESS' in message.upper() or '成功' in message or '完成' in message:
                level = 'success'
            elif '开始' in message or '正在' in message:
                level = 'info'
            
            logs.append({
                'message': message,
                'level': level,
                'timestamp': time.time()
            })
    
    return jsonify({
        'status': True,
        'data': logs
    })

@app.after_request
def _no_cache_html(response):
    """HTML 绝不缓存：否则我更新 App 后，浏览器会拿旧的前端包继续跑，
    表现为「界面行为跟新版本不一致」，极难排查（哈希过的静态资源仍可缓存）。
    """
    ctype = (response.headers.get('Content-Type') or '').lower()
    if 'text/html' in ctype:
        response.headers['Cache-Control'] = 'no-store, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
    return response


@app.route('/api/version', methods=['GET'])
def api_version():
    """返回前端包的时间戳，用于确认浏览器加载的是不是最新构建。"""
    import os as _os
    from api.paths import bundle_path

    index = bundle_path('web', 'dist', 'index.html')
    try:
        stamp = int(_os.path.getmtime(index))
    except OSError:
        stamp = 0
    return jsonify({'status': True, 'data': {'webBuild': stamp}})


@app.route('/api/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({'status': True, 'msg': 'OK'})


# ==================== 泛雅「任务」模块自动化（任务引擎） ====================
#
# 与「开始学习」互补：原有的 /api/start 只处理**章节任务点**，
# 这里处理课程页左侧「任务」标签背后由 task.chaoxing.com 提供的任务点，
# 例如 AI实践、主题讨论、作业、文档等。
#
# 流程刻意分成两步，避免误触直接改动作业状态：
#   1) POST /api/fanya/preview   → 后台盘点所有任务点，返回可自动化清单
#   2) POST /api/fanya/execute   → 用户在界面上二次确认后，才真正执行

# 预览结果与执行结果（按 task_id 存放）
fanya_previews: Dict[str, dict] = {}
fanya_results: Dict[str, dict] = {}


def _fanya_login(username, password, use_cookies, tiku_config):
    """登录并返回 (chaoxing, 课程列表)。"""
    common_config = {
        'username': username,
        'password': password,
        'use_cookies': use_cookies,
    }
    chaoxing = main_module.init_chaoxing(common_config, tiku_config or {})
    login_result = chaoxing.login(login_with_cookies=use_cookies)
    if not login_result['status']:
        raise LoginError(login_result.get('msg') or '登录失败')
    courses = chaoxing.get_course_list()
    return chaoxing, courses


def _select_courses(all_courses, course_list):
    """按前端勾选筛选课程；未勾选则视为全部（不进入交互式输入分支）。"""
    if not course_list:
        return list(all_courses)
    wanted = {str(item) for item in course_list}
    picked = [c for c in all_courses if str(c.get('courseId')) in wanted]
    return picked or list(all_courses)


def _new_fanya_task(task_id: str, kind: str):
    """初始化一个泛雅任务的公共状态。"""
    stop_event = threading.Event()
    task_stop_events[task_id] = stop_event
    task_status[task_id] = {
        'status': 'running',
        'kind': kind,
        'progress': 0,
        'current_course': '',
        'current_task': '',
        'logs': [],
        'start_time': time.time(),
        'stats': {'pending': 0, 'done': 0, 'failed': 0, 'skipped': 0},
    }
    return stop_event


@app.route('/api/fanya/preview', methods=['POST'])
def fanya_preview_start():
    """阶段一：只读盘点所有课程的任务点，返回可自动化判定。"""
    try:
        data = request.json or {}
        username = data.get('username')
        password = data.get('password')
        use_cookies = bool(data.get('use_cookies', True))
        course_list = data.get('course_list') or []
        tiku_config = data.get('tiku_config') or {}

        if not use_cookies and (not username or not password):
            return jsonify({'status': False, 'msg': '缺少账号密码，且未启用 cookies 登录'}), 400

        task_id = f"fanya-preview-{username or 'cookies'}-{int(time.time())}"
        _new_fanya_task(task_id, 'fanya_preview')

        def run_preview():
            capture = LogCapture(task_id)
            sink_id = logger.add(capture.write, enqueue=True)
            try:
                logger.info("开始盘点课程任务（只读，不会修改任何学习记录）")
                _, all_courses = _fanya_login(username, password, use_cookies, tiku_config)
                selected = _select_courses(all_courses, course_list)
                logger.info(f"共选中 {len(selected)} 门课程，开始读取任务引擎任务点...")

                from api.fanya_task import FanyaTaskClient, build_task_preview

                def on_progress(payload):
                    state = task_status[task_id]
                    if payload.get('type') == 'course_start':
                        state['progress'] = payload.get('index', 1) - 1
                        state['current_course'] = payload.get('title', '')
                    elif payload.get('type') == 'course_done':
                        state['progress'] = payload.get('index', 0)
                        state['current_course'] = payload.get('title', '')

                preview = build_task_preview(
                    chaoxing=None, courses=selected, client=FanyaTaskClient(),
                    on_progress=on_progress,
                    # 重做模式：已完成的任务点也要识别，否则无法重做
                    redo=bool((data.get('options') or {}).get('redo')),
                )
                fanya_previews[task_id] = preview
                summary = preview.get('summary') or {}
                logger.info(
                    "盘点完成：课程 {} 门，任务 {} 个，待完成 {} 个（可自动完成 {} 个，需人工 {} 个）",
                    summary.get('courseCount', 0), summary.get('taskCount', 0),
                    summary.get('pendingCount', 0), summary.get('completableCount', 0),
                    summary.get('manualCount', 0),
                )
                task_status[task_id]['status'] = 'completed'
            except Exception as exc:
                logger.error(f"盘点任务失败: {exc}")
                logger.debug(traceback.format_exc())
                task_status[task_id]['status'] = 'error'
                task_status[task_id]['error'] = str(exc)
            finally:
                try:
                    logger.remove(sink_id)
                except Exception:
                    pass

        threading.Thread(target=run_preview, daemon=True).start()
        return jsonify({'status': True, 'data': {'task_id': task_id}})

    except Exception as exc:
        logger.error(f"启动盘点任务错误: {exc}")
        return jsonify({'status': False, 'msg': str(exc)}), 500


@app.route('/api/fanya/preview/<task_id>', methods=['GET'])
def fanya_preview_get(task_id):
    """查询盘点进度与结果。"""
    if task_id not in task_status:
        return jsonify({'status': False, 'msg': '任务不存在'}), 404
    state = task_status[task_id]
    return jsonify({
        'status': True,
        'data': {
            'task': state,
            'preview': fanya_previews.get(task_id),
        },
    })


@app.route('/api/fanya/execute', methods=['POST'])
def fanya_execute():
    """阶段二：用户确认后，真正执行自动完成。"""
    try:
        data = request.json or {}
        username = data.get('username')
        password = data.get('password')
        use_cookies = bool(data.get('use_cookies', True))
        course_list = data.get('course_list') or []
        # {courseId: [taskId, ...]}，为空表示处理所有任务
        tasks_by_course = data.get('tasks_by_course') or None
        # [planId, ...]，为空表示处理所有可自动化任务点
        plan_ids = data.get('plan_ids') or None
        tiku_config = data.get('tiku_config') or {}
        options = data.get('options') or {}

        if not use_cookies and (not username or not password):
            return jsonify({'status': False, 'msg': '缺少账号密码，且未启用 cookies 登录'}), 400

        task_id = f"fanya-run-{username or 'cookies'}-{int(time.time())}"
        stop_event = _new_fanya_task(task_id, 'fanya_execute')

        def run_execute():
            capture = LogCapture(task_id)
            sink_id = logger.add(capture.write, enqueue=True)
            try:
                logger.info("开始执行「任务」模块自动完成")
                if not plan_ids:
                    logger.info("未指定任务点，将处理所有可自动化的任务点")
                chaoxing, all_courses = _fanya_login(username, password, use_cookies, tiku_config)
                selected = _select_courses(all_courses, course_list)

                from api.fanya_task import (
                    ContentGenerator, FanyaTaskClient, FanyaTaskRunner,
                )

                generator = ContentGenerator.from_tiku_config(tiku_config)
                if generator.available:
                    logger.info(f"作答内容生成已启用：{generator.model}")
                else:
                    logger.warning("未配置 AI 接口，主题讨论等需要文字作答的任务将使用模板文本")

                runner_options = dict(options)
                runner_options['stop_event'] = stop_event
                # 「生成后暂停、逐条审批」默认开启：除非前端显式关掉，
                # 否则任何会写入账号的内容都不提交，只登记待审批项。
                runner_options.setdefault('require_approval', True)

                def on_event(payload):
                    kind = payload.get('type')
                    stats = task_status[task_id]['stats']
                    if kind == 'item_done':
                        if payload.get('ok'):
                            stats['done'] += 1
                        elif payload.get('skipped'):
                            stats['skipped'] += 1
                        else:
                            stats['failed'] += 1
                    elif kind == 'item_start':
                        task_status[task_id]['current_task'] = payload.get('name', '')
                    elif kind == 'task_start':
                        task_status[task_id]['current_task'] = payload.get('name', '')
                        stats['pending'] += payload.get('pending', 0)

                runner = FanyaTaskRunner(
                    chaoxing=chaoxing,
                    client=FanyaTaskClient(),
                    generator=generator,
                    options=runner_options,
                    on_event=on_event,
                )

                if plan_ids:
                    # 只勾选了部分任务点时，逐个任务点执行，保持前端勾选语义
                    wanted = {int(x) for x in plan_ids}
                    for course in selected:
                        try:
                            tasks = runner.client.get_course_tasks(
                                course.get('courseId'), course.get('clazzId'))
                        except Exception as exc:
                            logger.error(f"课程读取失败: {exc}")
                            continue
                        for task in tasks:
                            runner.run_task(course, task, only_plan_ids=list(wanted))
                            if stop_event.is_set():
                                break
                        if stop_event.is_set():
                            break
                else:
                    runner.run_courses(selected, tasks_by_course)

                fanya_results[task_id] = {'results': runner.results}
                if stop_event.is_set():
                    task_status[task_id]['status'] = 'stopped'
                    logger.warning("任务已按停止请求退出")
                else:
                    stats = task_status[task_id]['stats']
                    task_status[task_id]['status'] = 'completed'
                    logger.info(
                        "执行结束：成功 {} 个，失败 {} 个，跳过 {} 个",
                        stats['done'], stats['failed'], stats['skipped'],
                    )
            except Exception as exc:
                logger.error(f"执行任务失败: {exc}")
                logger.debug(traceback.format_exc())
                task_status[task_id]['status'] = 'error'
                task_status[task_id]['error'] = str(exc)
            finally:
                try:
                    logger.remove(sink_id)
                except Exception:
                    pass

        threading.Thread(target=run_execute, daemon=True).start()
        return jsonify({'status': True, 'data': {'task_id': task_id}})

    except Exception as exc:
        logger.error(f"启动执行任务错误: {exc}")
        return jsonify({'status': False, 'msg': str(exc)}), 500


@app.route('/api/fanya/result/<task_id>', methods=['GET'])
def fanya_result(task_id):
    """查询执行结果明细。"""
    if task_id not in task_status:
        return jsonify({'status': False, 'msg': '任务不存在'}), 404
    return jsonify({
        'status': True,
        'data': {
            'task': task_status[task_id],
            'result': fanya_results.get(task_id),
        },
    })


# ---------------------- 待审批队列（生成后暂停、逐条审批） ----------------------


@app.route('/api/fanya/approvals', methods=['GET'])
def fanya_approvals():
    """列出待审批项（默认只列 pending，可带 include_decided=1）。"""
    from api.fanya_approval import STORE

    include = request.args.get('include_decided') in ('1', 'true', 'yes')
    items = STORE.list(include_decided=include)
    return jsonify({
        'status': True,
        'data': {
            'items': items,
            'pending': len([i for i in items if i['status'] == 'pending']),
        },
    })


@app.route('/api/fanya/approve', methods=['POST'])
def fanya_approve():
    """批准或拒绝一个待审批项。

    body: ``{"id": "ap-1", "approve": true}``

    批准才会执行该条目登记的提交动作；拒绝则丢弃，不写入任何内容。
    """
    from api.fanya_approval import STORE

    data = request.json or {}
    entry_id = data.get('id')
    if not entry_id:
        return jsonify({'status': False, 'msg': '缺少 id'}), 400
    approve = bool(data.get('approve', True))

    logger.info("收到审批决定：{} → {}", entry_id, "批准" if approve else "拒绝")
    ok, msg = STORE.decide(entry_id, approve)
    return jsonify({
        'status': ok,
        'msg': msg,
        'data': {'id': entry_id, 'approve': approve},
    }), (200 if ok else 400)


@app.route('/api/fanya/approvals/clear', methods=['POST'])
def fanya_approvals_clear():
    """清空待审批队列（keep_decided=1 时保留已决策记录）。"""
    from api.fanya_approval import STORE

    keep = bool((request.json or {}).get('keep_decided'))
    remaining = STORE.clear(keep_decided=keep)
    return jsonify({'status': True, 'data': {'remaining': remaining}})


# ==================== 静态文件服务（便携版支持） ====================

@app.route('/')
def serve_index():
    """服务前端首页"""
    if os.path.exists(STATIC_DIR):
        return send_from_directory(STATIC_DIR, 'index.html')
    return jsonify({'status': False, 'msg': '前端未构建，请访问 http://localhost:5173 使用开发模式'}), 404


@app.route('/<path:path>')
def serve_static(path):
    """服务静态文件，支持 SPA 客户端路由"""
    if os.path.exists(STATIC_DIR):
        # 如果请求的是 API 路径，跳过（已被上面的路由处理）
        if path.startswith('api/'):
            return jsonify({'status': False, 'msg': 'Not Found'}), 404
        
        # 尝试提供静态文件
        file_path = os.path.join(STATIC_DIR, path)
        if os.path.exists(file_path) and os.path.isfile(file_path):
            return send_from_directory(STATIC_DIR, path)
        
        # 对于 SPA 客户端路由，返回 index.html
        return send_from_directory(STATIC_DIR, 'index.html')
    
    return jsonify({'status': False, 'msg': '前端未构建'}), 404


if __name__ == "__main__":
    # 端口/监听地址/调试模式都可用环境变量覆盖，便于桌面版以更安全的参数启动
    host = os.environ.get("CHAOXING_HOST", "0.0.0.0")
    try:
        port = int(os.environ.get("CHAOXING_PORT", "5000"))
    except ValueError:
        port = 5000
    debug = str(os.environ.get("CHAOXING_DEBUG", "1")).strip().lower() in {"1", "true", "yes", "on"}

    # 检测是否存在前端构建
    if os.path.exists(STATIC_DIR):
        logger.info(f"检测到前端构建，将提供静态文件服务: {STATIC_DIR}")
        logger.info(f"请在浏览器中打开: http://localhost:{port}")
    else:
        logger.info("未检测到前端构建，仅提供 API 服务")
        logger.info("前端开发模式请访问: http://localhost:5173")

    # use_reloader=False 避免热重载导致后台线程 ThreadPoolExecutor 崩溃
    app.run(host=host, port=port, debug=debug, use_reloader=False)
