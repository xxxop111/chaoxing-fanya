# -*- coding: utf-8 -*-
"""纯函数单元测试：这些是平台一改版就会**静默失效**的解析层。

**零依赖**，两种跑法都行：

    .venv\\Scripts\\python.exe tests\\test_parsers.py     # 直接跑（内置 runner）
    .venv\\Scripts\\python.exe -m pytest tests -q           # 若已装 pytest

不登录、不联网、不写入任何账号数据。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api.fanya_ladder import parse_choice_question          # noqa: E402
from api.fanya_work import _is_refusal, _resolve_choice_answer, build_submission  # noqa: E402
from api.fanya_upload import needs_dialogue, copy_is_banned, min_chars  # noqa: E402
from api.fanya_task import _format_progress, _window_reason  # noqa: E402
from api.fanya_exam import (  # noqa: E402
    describe_exam_page, looks_like_answering_page,
)


# ---------------- 选择题解析 ----------------

def test_parse_choice_from_json_string():
    raw = ('{"questionStem":"下列说法正确的是？",'
           '"questionOptions":"[{\\"option\\":\\"A\\",\\"optionContent\\":\\"甲\\"},'
           '{\\"option\\":\\"B\\",\\"optionContent\\":\\"乙\\"}]"}')
    got = parse_choice_question(raw)
    assert got is not None
    assert got["stem"] == "下列说法正确的是？"
    assert [o["option"] for o in got["options"]] == ["A", "B"]


def test_parse_choice_from_dict_with_list_options():
    got = parse_choice_question({
        "questionStem": "S",
        "questionOptions": [{"option": "A", "optionContent": "甲"},
                            {"option": "B", "optionContent": "乙"}],
    })
    assert got is not None and len(got["options"]) == 2


def test_parse_choice_returns_none_for_plain_text():
    # 平台的回复常是 Markdown 文本，不能当成题目
    assert parse_choice_question("你好，请继续作答。") is None
    assert parse_choice_question("") is None
    assert parse_choice_question(None) is None


def test_parse_choice_ignores_options_without_identifier():
    assert parse_choice_question(
        {"questionStem": "S", "questionOptions": [{"optionContent": "没有 option 字段"}]}
    ) is None


# ---------------- 题库拒答识别 ----------------

def test_refusal_detected():
    # 真实发生过：题库返回这句，被当成答案提交进了作业
    real = "由于题目中只提供了一个图片链接，没有具体的题目文字内容，我无法看到题目要求。"
    assert _is_refusal(real) is True
    assert _is_refusal("请提供题目的文字描述或重新上传清晰的题目图片，以便我能够为您解答。") is True


def test_normal_answer_not_refusal():
    assert _is_refusal("E = q / (4 pi epsilon0 r^2)，方向沿径向") is False
    assert _is_refusal("") is False


# ---------------- 题目要求识别 ----------------

def test_requirement_detection():
    req = "至少进行5轮问答交互。提交物：①对话实录：完整记录你与AI的对话过程（截图<=9张）"
    assert needs_dialogue(req) is True
    assert min_chars("提交不少于 300 字文本") == 300
    assert min_chars("随便写点") == 0


def test_copy_ban_detection():
    assert copy_is_banned("禁止直接大段复制 AI 输出，用自己的话整理") is True
    assert copy_is_banned("可借助 AI 辅助理解") is False


# ---------------- 展示层格式化 ----------------

def test_format_progress_handles_percentage_like_value():
    # 实测个别任务返回的是 8.33 这种百分数，不能渲染成 833%
    assert _format_progress(0.5) == "50.0%"
    assert "原始值" in _format_progress(8.33)
    assert _format_progress(None) == "None"


def test_window_reason_is_human_readable():
    assert "学习窗口未开放" in _window_reason("任务点学习时间：2026-01-01 至 2026-01-02，当前不在学习时间内")
    assert "暂不可进入" in _window_reason("任务点未解锁，不允许学习")




# ---------------- 课程去重（「两个大学物理1」的根因）----------------

def test_dedup_drops_exact_duplicates():
    from api.base import dedup_course_list
    courses = [
        {"courseId": "1", "clazzId": "100", "title": "大学物理1"},
        {"courseId": "2", "clazzId": "200", "title": "概率论"},
        {"courseId": "1", "clazzId": "100", "title": "大学物理1"},   # 重复
    ]
    out = dedup_course_list(courses)
    assert len(out) == 2
    assert [c["title"] for c in out] == ["大学物理1", "概率论"]      # 保持顺序


def test_dedup_keeps_same_course_different_class():
    """同一门课教两个班（clazzId 不同）必须都保留，否则会漏掉一个班。"""
    from api.base import dedup_course_list
    courses = [
        {"courseId": "1", "clazzId": "100", "title": "大学物理1"},
        {"courseId": "1", "clazzId": "101", "title": "大学物理1"},
    ]
    assert len(dedup_course_list(courses)) == 2


def test_dedup_handles_missing_fields():
    from api.base import dedup_course_list
    assert len(dedup_course_list([{}, {}])) == 1
    assert dedup_course_list([]) == []


# ---------------- 同名课程区分（「两个大学物理1」的显示层修复）----------------

def test_same_title_gets_teacher_suffix():
    from api.base import disambiguate_course_titles
    out = disambiguate_course_titles([
        {"courseId": "1", "clazzId": "100", "title": "数据结构与算法", "teacher": "张三"},
        {"courseId": "1", "clazzId": "101", "title": "数据结构与算法", "teacher": "李四"},
    ])
    assert [c["displayTitle"] for c in out] == [
        "数据结构与算法（张三）", "数据结构与算法（李四）"]
    # title 本身不能被改（别处依赖它做匹配）
    assert out[0]["title"] == "数据结构与算法"


def test_unique_title_unchanged():
    from api.base import disambiguate_course_titles
    out = disambiguate_course_titles([{"courseId": "2", "title": "概率论", "teacher": "王五"}])
    assert out[0]["displayTitle"] == "概率论"


def test_same_teacher_falls_back_to_index():
    from api.base import disambiguate_course_titles
    out = disambiguate_course_titles([
        {"courseId": "1", "clazzId": "1", "title": "大学物理1", "teacher": "张三"},
        {"courseId": "1", "clazzId": "2", "title": "大学物理1", "teacher": "张三"},
    ])
    a, b = (c["displayTitle"] for c in out)
    assert a != b, "教师相同时必须仍能区分"
    assert a == "大学物理1（张三）"


def test_missing_teacher_labeled():
    from api.base import disambiguate_course_titles
    out = disambiguate_course_titles([
        {"courseId": "1", "clazzId": "1", "title": "数学建模"},
        {"courseId": "1", "clazzId": "2", "title": "数学建模"},
    ])
    assert all("未标注教师" in c["displayTitle"] for c in out)


# ---------------- AI剧情（type=7）纯函数 ----------------

def test_story_extract_prompt_from_requirement():
    """题目常写「复制下方提示词发给 AI」—— 第一轮该发这段原文。"""
    from api.fanya_story import extract_prompt
    req = ("业务背景：审计抽样，错报率 p=0.02。请完成 1/2/3。\n"
           "把以上内容整理成文字，复制下方提示词发给AI校验模型正确性。\n\n"
           "AI提示词：\n请帮我完成概率论离散型随机变量建模校验。业务背景：审计抽样。\n"
           "请回答：1.X属于离散型还是连续型？\n\n提交物：文本作答 + AI对话文本/截图")
    got = extract_prompt(req)
    assert "请帮我完成概率论离散型随机变量建模校验" in got
    assert "离散型还是连续型" in got
    assert "提交物" not in got, "不应把提交物要求带进提示词"


def test_story_extract_prompt_absent():
    from api.fanya_story import extract_prompt
    assert extract_prompt("随便写点什么，没有提示词") == ""
    assert extract_prompt("") == ""
    assert extract_prompt(None) == ""


def test_story_chapter_hit_semantics():
    """condition[].hit 全为真才算达标；没有 condition 视为未达标。"""
    from api.fanya_story import chapter_hit
    assert chapter_hit({"condition": [{"hit": True}]}) is True
    assert chapter_hit({"condition": [{"hit": False}]}) is False
    assert chapter_hit({"condition": [{"hit": True}, {"hit": False}]}) is False
    assert chapter_hit({"condition": []}) is False
    assert chapter_hit({}) is False


def test_story_build_round_prompt_uses_given_prompt_first():
    """第 1 轮优先用题目给定的提示词；后续轮改成要求继续追问。"""
    from api.fanya_story import build_round_prompt
    ch = {"title": "审计抽样建模校验"}
    req = "AI提示词：请帮我校验二项分布建模。\n\n提交物：文本"
    first = build_round_prompt(ch, 1, [], req)
    assert first.startswith("请帮我校验二项分布建模")
    later = build_round_prompt(ch, 2, [], req)
    assert "第 2 轮" in later and "审计抽样建模校验" in later


# ---------------- AI剧情 评分参数（回归：2026-10-12 修正的 bug）----------------

class _StubResp:
    """最小可用的响应替身：evaluate_chapter 只用到 iter_lines/close。"""

    status_code = 200

    def iter_lines(self, decode_unicode=True):  # noqa: ARG002
        return iter(())

    def close(self):
        pass


class _StubSession:
    """记录最后一次请求的参数，用于断言。"""

    def __init__(self):
        self.url = None
        self.kw = None

    def get(self, url, **kw):  # noqa: ARG002
        self.url = url
        self.kw = kw
        return _StubResp()


def test_story_evaluate_record_uuid_is_outer_record():
    """★ 回归测试：evaluate/stream 的 ``recordUuid`` 必须是**外层记录**。

    背景（2026-10-12）：这里曾经传成**章节记录**，服务端回「会话不存在」，
    于是得出了一个错误结论 ——「评分必须由浏览器建立对答会话，无法纯 HTTP 自动化」，
    并把它写进了 README 与协议文档。

    真实原因只是参数传错。对照实测（同一条记录、同一个接口）：

    * 传外层记录 → ``任务未提交，无法开始评估``（真实业务状态）
    * 传章节记录 → ``会话不存在``

    本测试把「外层记录 / 章节记录各归其位」钉住，防止再次退化。
    """
    from api.fanya_story import evaluate_chapter

    sess = _StubSession()
    study_url = {"url": "https://x/answer?courseid=111&clazzid=222&cpi=333"}
    evaluate_chapter(sess, study_url, {}, "AIENC",
                     "OUTER-RECORD", "CHAPTER-RECORD")
    params = sess.kw["params"]
    assert params["recordUuid"] == "OUTER-RECORD", \
        "recordUuid 必须是外层记录；传章节记录会得到「会话不存在」"
    assert params["chapterRecordUuid"] == "CHAPTER-RECORD"
    assert params["courseId"] == "111" and params["clazzId"] == "222"
    assert params["cpi"] == "333" and params["aiEnc"] == "AIENC"
    assert params["useJsonScheme"] == "true"


def test_story_evaluate_omits_chapter_uuid_by_default():
    """对话页并不发送 ``chapterUuid``；只有调用方明确提供时才附带。"""
    from api.fanya_story import evaluate_chapter

    sess = _StubSession()
    evaluate_chapter(sess, {"url": "?courseid=1&clazzid=2&cpi=3"}, {}, "E",
                     "OUTER", "CHAPTER")
    assert "chapterUuid" not in sess.kw["params"]

    sess2 = _StubSession()
    evaluate_chapter(sess2, {"url": "?courseid=1&clazzid=2&cpi=3"}, {}, "E",
                     "OUTER", "CHAPTER", "CH-UUID")
    assert sess2.kw["params"]["chapterUuid"] == "CH-UUID"


def test_story_evaluate_uses_get_not_post():
    """评分走 EventSource，即 **GET**（此前用的是 POST）。"""
    from api.fanya_story import evaluate_chapter

    sess = _StubSession()
    evaluate_chapter(sess, {"url": "?courseid=1&clazzid=2&cpi=3"}, {}, "E",
                     "OUTER", "CHAPTER")
    assert sess.kw.get("stream") is True
    assert "evaluate/stream" in sess.url
    assert not hasattr(sess, "post"), "不应再使用 POST"


# ----------------------------------------------------------------------
# 累计观看时长门槛：判据是 score.totalTime >= watchDuration(分钟) × 60000
# 这层一旦算错，就会把「上报成功」当成「已完成」（曾经真的这样）
# ----------------------------------------------------------------------
def test_watch_duration_ms_converts_minutes():
    from api.fanya_task import watch_duration_ms

    extra = {"planBreakthroughSet": {"enableVideoWatchDuration": 1,
                                     "videoWatchDuration": 13.0}}
    assert watch_duration_ms(extra, "video") == 780000


def test_watch_duration_ms_zero_when_not_enabled():
    from api.fanya_task import watch_duration_ms

    # enable 位关了 / 没这个字段 / 值为 0，都算「没有要求」
    assert watch_duration_ms(
        {"planBreakthroughSet": {"enableVideoWatchDuration": 0,
                                 "videoWatchDuration": 13.0}}, "video") == 0
    assert watch_duration_ms(
        {"planBreakthroughSet": {"videoWatchDuration": 13.0}}, "video") == 0
    assert watch_duration_ms(
        {"planBreakthroughSet": {"enableVideoWatchDuration": 1,
                                 "videoWatchDuration": 0}}, "video") == 0


def test_watch_duration_ms_tolerates_broken_input():
    """任务点字段是平台返回的原始 JSON，缺字段/类型不对都不能抛。"""
    from api.fanya_task import watch_duration_ms

    assert watch_duration_ms(None, "video") == 0
    assert watch_duration_ms({}, "video") == 0
    assert watch_duration_ms({"planBreakthroughSet": None}, "video") == 0
    assert watch_duration_ms(
        {"planBreakthroughSet": {"enableVideoWatchDuration": 1,
                                 "videoWatchDuration": "abc"}}, "video") == 0
    assert watch_duration_ms(
        {"planBreakthroughSet": {"enableVideoWatchDuration": 1,
                                 "videoWatchDuration": None}}, "video") == 0
    assert watch_duration_ms(
        {"planBreakthroughSet": {"enableVideoWatchDuration": 1,
                                 "videoWatchDuration": 5}}, "movie") == 0


def test_watch_duration_ms_per_kind():
    from api.fanya_task import watch_duration_ms

    assert watch_duration_ms(
        {"planBreakthroughSet": {"enableDocumentWatchDuration": 1,
                                 "documentWatchDuration": 3.0}}, "document") == 180000
    assert watch_duration_ms(
        {"planBreakthroughSet": {"enableAudioWatchDuration": 1,
                                 "audioWatchDuration": 2.5}}, "audio") == 150000


def test_watched_ms_reads_total_time():
    from api.fanya_task import watched_ms

    # 实测：唯一完成的那个任务点 totalTime=304000（304s ≥ 5 分钟）
    assert watched_ms({"totalTime": 304000}) == 304000
    assert watched_ms({}) == 0
    assert watched_ms({"totalTime": None}) == 0
    assert watched_ms(None) == 0
    assert watched_ms("304000") == 0  # score 不是 dict 时不能瞎猜


# ----------------------------------------------------------------------
# 考试「状态页」识别：过期 / 已交卷 / 从未参加
#
# 背景：2026-10-15 实测 COM0018A 的 11 个未完成测验，全都被旧实现含糊地
# 报成「作答页缺少必需隐藏域 saveUrl」。真实成因只有两种：窗口已结束，
# 或试卷在任务计划建立之前就已交卷（平台不认旧交卷）。这里钉住识别逻辑。
# ----------------------------------------------------------------------
def test_describe_exam_page_detects_expired():
    html = '<h2>考试已过期</h2><p>开始时间：2026-05-11 09:59</p>'
    assert "已过期" in describe_exam_page(html)


def test_describe_exam_page_detects_submitted():
    assert "已经交卷" in describe_exam_page('<span>交卷成功</span>')
    assert "查看试卷详情" in describe_exam_page('<a>查看试卷详情</a>')


def test_describe_exam_page_detects_never_attended():
    html = '{"msg":"学生未参加考试","status":false}'
    assert "未参加考试" in describe_exam_page(html)


def test_describe_exam_page_returns_empty_for_answering_page():
    # 真正的作答页不该被认成状态页，否则会误报「无法补完成」
    html = '<input id="saveUrl" value="/x"><form id="submitTest"></form><div class="questionLi">'
    assert describe_exam_page(html) == ""
    assert describe_exam_page("<html><body>nothing here</body></html>") == ""


def test_exam_answering_page_needs_save_url():
    assert looks_like_answering_page('<input id="saveUrl"><form id="submitTest">')
    assert looks_like_answering_page('<div class="questionLi">')
    # 只有 saveUrl 没有 submitTest、也没有 questionLi → 不算作答页
    assert not looks_like_answering_page('<input id="saveUrl">')


# ----------------------------------------------------------------------
# 学习窗口措辞：服务端对「还没开」和「已经关了」用同一句报错，
# 必须自己比较终点与当前时间。实测 2026-10-15 全账号 19 个被拒任务点
# 的窗口终点全部已过 —— 旧文案「等窗口开启后再盘点」是在让用户白等。
# ----------------------------------------------------------------------
def test_window_reason_says_expired_for_past_window():
    msg = ("任务点学习时间：2026-09-13 20:26至2026-09-20 23:59，当前不在学习时间内")
    out = _window_reason(msg)
    assert "已结束" in out
    assert "无法补完成" in out
    assert "等窗口开启" not in out


def test_window_reason_says_not_open_for_future_window():
    future = "2099-09-01 00:00至2099-09-30 23:59"
    out = _window_reason(f"任务学习时间：{future}，当前不在学习时间内")
    assert "尚未开放" in out
    assert "等窗口开启" in out


def test_window_reason_handles_unparseable_window():
    # 抽不出时间时不能瞎猜成「还没开」；要给出保守且不误导的说明
    out = _window_reason("任务学习时间未知，当前不在学习时间内")
    assert "未开放" in out
    assert "等窗口开启" not in out


def test_window_reason_passes_through_other_errors():
    out = _window_reason("some other server error")
    assert out.startswith("暂不可进入该任务点：")


# ---------------- 浏览器通道（api/fanya_browser）----------------
# 这些是纯逻辑/契约测试，**不启动浏览器**。

def test_browser_module_imports_without_playwright():
    # 模块必须能在没装 playwright 的环境里被导入（playwright 是可选依赖）
    from api import fanya_browser as fb
    assert callable(fb.read_document_in_browser)
    assert issubclass(fb.BrowserUnavailable, RuntimeError)


def test_document_learn_url_carries_epu():
    from api.fanya_browser import document_learn_url
    url = document_learn_url("BEDCjrlChRDhp@abc123")
    assert url.startswith("https://task.chaoxing.com/documentStudy/learnPage?")
    assert url.endswith("encryPlanUserId=BEDCjrlChRDhp@abc123")


def test_browser_cookies_get_real_chaoxing_domain():
    """requests 的 cookie domain 常是空串；必须改写成 .chaoxing.com。

    否则 passport2.chaoxing.com 收不到 cookie，浏览器会被踢回登录页
    （这是实测踩过的坑，不是理论风险）。
    """
    from api.fanya_browser import _cookies_for_playwright

    class _C:
        def __init__(self, name, value, domain):
            self.name, self.value, self.domain = name, value, domain
            self.path, self.secure = "/", False

    class _Jar(list):
        pass

    class _S:
        cookies = _Jar([
            _C("JSESSIONID", "x", ""),                    # 空 domain → 要救回来
            _C("uf", "y", None),                          # None → 也要救回来
            _C("UID", "z", ".chaoxing.com"),              # 正常
            _C("evil", "w", ".example.com"),              # 非 chaoxing → 丢掉
        ])

    out = _cookies_for_playwright(_S())
    names = {c["name"] for c in out}
    assert names == {"JSESSIONID", "uf", "UID"}
    assert all(c["domain"] == ".chaoxing.com" for c in out)


def test_browser_temp_dir_is_writable_and_used():
    """必须把 TEMP/TMP/TMPDIR 指到工作区内的可写目录。

    ★ 沙箱下 %TEMP% 不可写，Playwright 启动浏览器时 mkdtemp 会直接失败；
      所以这一步必须在导入 playwright 之前完成。
    """
    import os
    from api.fanya_browser import _prepare_temp

    tmp = _prepare_temp()
    assert os.path.isdir(tmp)
    for key in ("TEMP", "TMP", "TMPDIR"):
        assert os.environ[key] == tmp


# ---------------- 选择题答案 → 选项字母 ----------------
_OPTS4 = ["A. 甲的说法", "B. 乙的说法", "C. 丙的说法", "D. 丁的说法"]


def test_resolve_choice_answer_accepts_letters():
    assert _resolve_choice_answer("B", _OPTS4) == "B"
    assert _resolve_choice_answer("b", _OPTS4) == "B"
    assert _resolve_choice_answer("答：AB", _OPTS4) == "AB"
    assert _resolve_choice_answer("A,B", _OPTS4) == "AB"
    # 字母超出选项范围 → 宁可留空，也不能把 "E" 交上去
    assert _resolve_choice_answer("E", _OPTS4) is None


def test_resolve_choice_answer_maps_option_text():
    assert _resolve_choice_answer("乙的说法", _OPTS4) == "B"
    assert _resolve_choice_answer("B. 乙的说法", _OPTS4) == "B"
    # 模型只回了一小段选项原文（含在选项里）也要能对上
    assert _resolve_choice_answer("丁的说法", _OPTS4) == "D"


def test_resolve_choice_answer_rejects_plain_text():
    """旧实现会把整段话写进单选字段，服务端判错、页面上「我的答案」变成一段话。"""
    assert _resolve_choice_answer("Logistic 曲线呈 S 形", _OPTS4) is None
    assert _resolve_choice_answer("", _OPTS4) is None


class _StubTiku:
    def __init__(self, answer):
        self.answer = answer

    def query(self, _payload):
        return self.answer


def _one_choice_work():
    return {
        "formData": {"answertypeq1": "0", "answerq1": ""},
        "answerwqbid": "q1",
        "questions": [{
            "id": "q1", "type": "single", "typeCode": 0, "title": "下列说法正确的是？",
            "options": _OPTS4, "answerFields": {"answertypeq1": "0", "answerq1": ""},
        }],
    }


def test_build_submission_accepts_option_text_answer():
    payload, found, total, details = build_submission(_one_choice_work(), _StubTiku("乙的说法"))
    assert (found, total) == (1, 1)
    assert payload["answerq1"] == "B"
    assert details[0]["answered"] is True


def test_build_submission_leaves_choice_empty_when_unmappable():
    payload, found, total, details = build_submission(
        _one_choice_work(), _StubTiku("Logistic 曲线呈 S 形"))
    assert (found, total) == (0, 1)
    assert payload["answerq1"] == ""
    assert details[0]["answered"] is False
    assert details[0]["reason"] == "答案无法对应选项"


# ----------------------------------------------------------------------
# 大模型复核（verifier）：非大模型主题库串题时的兜底
# 实测背景：题库把「Logistic 曲线形状」答成「周期正弦曲线」，整卷交成 4/9，
# 而同一个模型对同一份卷子 9/9。两个来源冲突时以大模型为准。
# ----------------------------------------------------------------------
def test_build_submission_without_verifier_keeps_tiku_answer():
    payload, found, _total, _details = build_submission(_one_choice_work(), _StubTiku("A"))
    assert payload["answerq1"] == "A"
    assert found == 1


def test_verifier_overrides_disagreeing_tiku_answer():
    payload, found, total, details = build_submission(
        _one_choice_work(), _StubTiku("A"), verifier=_StubTiku("B"))
    assert payload["answerq1"] == "B"
    assert (found, total) == (1, 1)
    assert details[0]["answered"] is True


def test_verifier_agreement_keeps_primary_answer():
    payload, _found, _total, _details = build_submission(
        _one_choice_work(), _StubTiku("乙的说法"), verifier=_StubTiku("B"))
    assert payload["answerq1"] == "B"


def test_verifier_useless_answer_keeps_primary():
    """复核回了一段对不上选项的话 → 视为「没有意见」，不能因此把答案弄丢。"""
    for bad in ("我不确定", "", None, "E"):
        payload, found, _total, _details = build_submission(
            _one_choice_work(), _StubTiku("A"), verifier=_StubTiku(bad))
        assert payload["answerq1"] == "A", bad
        assert found == 1, bad


def test_verifier_failure_is_swallowed():
    class _Boom:
        def query(self, _payload):
            raise RuntimeError("endpoint down")

    payload, found, _total, _details = build_submission(
        _one_choice_work(), _StubTiku("A"), verifier=_Boom())
    assert payload["answerq1"] == "A"
    assert found == 1


def test_verifier_skipped_when_primary_unanswered():
    """题库没命中时不去问模型（避免把本该留空的题变成「有答案」）。"""
    calls = []

    class _Counting:
        def query(self, payload):
            calls.append(payload)
            return "B"

    payload, found, _total, _details = build_submission(
        _one_choice_work(), _StubTiku(None), verifier=_Counting())
    assert payload["answerq1"] == ""
    assert found == 0
    assert calls == []


def test_judgement_answer_normalised_to_true_false():
    """判断题取值必须是字符串 "true"/"false"：写「对/错」会被服务端整份拒绝。"""
    work = _one_choice_work()
    work["questions"][0]["type"] = "judgement"
    work["questions"][0]["options"] = []
    for raw, expect in (("正确", "true"), ("对", "true"), ("√", "true"),
                        ("错误", "false"), ("错", "false"), ("×", "false")):
        payload, found, _total, _details = build_submission(work, _StubTiku(raw))
        assert payload["answerq1"] == expect, raw
        assert found == 1, raw


def test_ai_verifier_returns_none_for_ai_provider_and_for_incomplete_config():
    """复核是纯增强：主题库已经是大模型、或没配齐 endpoint/key/model 时都返回 None。"""
    from api.fanya_task import _build_ai_verifier

    class _T:
        def __init__(self, conf):
            self._conf = conf

    assert _build_ai_verifier(_T({"provider": "AI"})) is None
    assert _build_ai_verifier(_T(None)) is None
    assert _build_ai_verifier(_T({})) is None
    # 非大模型主题库但缺 key → 放弃复核，而不是抛异常
    assert _build_ai_verifier(_T({"provider": "LIKE",
                                  "endpoint": "https://x/y",
                                  "model": "m"})) is None


# ----------------------------------------------------------------------
# 内置 runner：没装 pytest 也能跑（本仓库刻意保持零测试依赖）
# ----------------------------------------------------------------------
def _run_all() -> int:
    import traceback

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed, failed = 0, []
    for name, func in tests:
        try:
            func()
            passed += 1
            print(f"  PASS  {name}")
        except Exception:  # noqa: BLE001
            failed.append(name)
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {len(failed)} failed")
    return 1 if failed else 0


def test_handle_course_work_passes_ai_verifier_down():
    """handler 必须把复核器真的传下去。

    ``_build_ai_verifier`` 单独存在没有任何意义：只有 ``handle_course_work`` 把它
    递给 complete_course_work / prepare_course_work，非大模型主题库的答案才会被
    复核。少了这一步，用户装上后行为与修复前完全一样。
    """
    from types import SimpleNamespace

    import api.answer as answer_mod
    import api.fanya_work as fw
    from api import fanya_task as ft

    class _FakeAI:
        DISABLE = False

        def __init__(self):
            self.conf = None

        def config_set(self, conf):
            self.conf = conf

        def init_tiku(self):
            return True

    class _Tiku:
        _conf = {"provider": "LIKE", "endpoint": "https://example.invalid/chat",
                 "key": "k", "model": "m"}
        COVER_RATE = 0.0

        def get_submit_params(self):
            return "1"  # 保存但不提交

    captured = {}

    def _fake_complete(session, entry_url, tiku, **kwargs):
        captured.update(kwargs)
        captured["entry_url"] = entry_url
        return True, "ok"

    def _fake_prepare(session, entry_url, tiku, **kwargs):
        captured.update(kwargs)
        captured["entry_url"] = entry_url
        return True, "prepared", {"total": 1, "found": 1, "rate": 1.0,
                                  "payload": {"answerq1": "B"}}

    real_ai = answer_mod.AI
    real_complete = fw.complete_course_work
    real_prepare = fw.prepare_course_work
    ctx = SimpleNamespace(
        study_url={"url": "https://mooc2-ans.chaoxing.com/work?workId=1"},
        chaoxing=SimpleNamespace(tiku=_Tiku()),
        client=SimpleNamespace(session=object()),
        require_approval=False,
        label="作业",
        register_approval=lambda **kwargs: "approval-1",
    )

    try:
        answer_mod.AI = _FakeAI
        fw.complete_course_work = _fake_complete
        fw.prepare_course_work = _fake_prepare

        # 1) 免审批：直接提交，复核器必须传进 complete_course_work
        captured.clear()
        ctx.require_approval = False
        assert ft.handle_course_work(ctx)[0] is True
        assert isinstance(captured.get("verifier"), _FakeAI), captured
        assert captured["verifier"].conf["model"] == "m"

        # 2) 审批模式：答案在 prepare 阶段就定下来（提交发生在审批之后），
        #    所以复核器同样必须传进 prepare_course_work
        captured.clear()
        ctx.require_approval = True
        assert ft.handle_course_work(ctx)[0] is True
        assert isinstance(captured.get("verifier"), _FakeAI), captured
    finally:
        answer_mod.AI = real_ai
        fw.complete_course_work = real_complete
        fw.prepare_course_work = real_prepare


# ----------------------------------------------------------------------
# 一次性作业保护（2026-10-16 自查）：平台不给重做次数时不能盲交
# ----------------------------------------------------------------------


def _work_parsed(n: int = 2) -> dict:
    questions = [{"id": f"q{i}", "type": "single", "title": f"题目{i}",
                  "options": ["A. 甲", "B. 乙"],
                  "answerFields": {f"answerq{i}": "", f"answertypeq{i}": 0}}
                 for i in range(1, n + 1)]
    return {"action": "/save", "formData": {}, "answerwqbid": "q1,",
            "questions": questions}


class _AnswerBookTiku:
    """按题干返回答案的假题库（多次查询返回不同答案用）。

    注意：不要复用文件上半部分的 ``_StubTiku``（它是「每次查询都返回同一份
    答案」，签名不同）。
    """

    def __init__(self, answers=None):
        self.answers = answers or {}

    def query(self, q):
        return self.answers.get(q["title"])


def test_build_prepared_never_submits_an_empty_sheet():
    """一道题都没答上时，旧代码会 do_submit=True 把空白卷交上去。"""
    from api import fanya_work as fw

    prepared = fw.build_prepared(_work_parsed(2), "http://x", None, cover_rate=0.0)
    assert prepared["do_submit"] is False, prepared
    assert "一道题都没答上" in prepared["blocked_reason"]


def test_build_prepared_single_shot_stays_draft_even_when_full():
    from api import fanya_work as fw

    tiku = _AnswerBookTiku({"题目1": "A", "题目2": "B"})
    prepared = fw.build_prepared(_work_parsed(2), "http://x", tiku, single_shot=True)
    assert prepared["rate"] == 1.0
    assert prepared["do_submit"] is False, prepared
    assert "不可逆" in prepared["blocked_reason"]
    assert prepared["single_shot"] is True


def test_build_prepared_single_shot_with_opt_in_needs_full_coverage():
    from api import fanya_work as fw

    partial = fw.build_prepared(_work_parsed(2), "http://x", _AnswerBookTiku({"题目1": "A"}),
                                single_shot=True, allow_single_shot=True)
    assert partial["do_submit"] is False
    assert "还有 1 题没答上" in partial["blocked_reason"]

    full = fw.build_prepared(_work_parsed(2), "http://x",
                             _AnswerBookTiku({"题目1": "A", "题目2": "B"}),
                             single_shot=True, allow_single_shot=True)
    assert full["do_submit"] is True
    assert full["blocked_reason"] == ""


def test_opt_in_only_accepts_truthy_strings():
    from api.fanya_work import _opt_in

    for value in ("true", "True", "1", "yes", "on", " ON "):
        assert _opt_in(value) is True, value
    for value in ("", None, "0", "false", "no", "随便"):
        assert _opt_in(value) is False, value


def test_submission_blockers_reports_cover_rate():
    from api.fanya_work import submission_blockers

    reasons = submission_blockers(total=4, found=2, rate=0.5, cover_rate=0.8)
    assert reasons and "低于设定阈值" in reasons[0]
    assert submission_blockers(total=4, found=4, rate=1.0, cover_rate=0.8) == []


def test_handle_course_work_treats_unknown_redo_times_as_one_shot():
    """平台查不到重做次数时，handler 必须按「只有一次机会」处理。"""
    from types import SimpleNamespace

    import api.fanya_work as fw
    from api import fanya_task as ft

    class _Tiku:
        _conf = {"provider": "AI", "endpoint": "e", "key": "k", "model": "m"}
        COVER_RATE = 0.0

        def get_submit_params(self):
            return "0"

    captured = {}

    def _fake_complete(session, entry_url, tiku, **kwargs):
        captured.update(kwargs)
        return True, "ok"

    real_complete = fw.complete_course_work
    ctx = SimpleNamespace(
        study_url={"url": "http://x"}, chaoxing=SimpleNamespace(tiku=_Tiku()),
        client=SimpleNamespace(session=object()), require_approval=False,
        label="作业", item=None, register_approval=lambda **kwargs: "a1",
    )
    try:
        fw.complete_course_work = _fake_complete

        # 1) 什么都没探测到 → 按一次性处理
        assert ft.handle_course_work(ctx)[0] is True
        assert captured["single_shot"] is True
        assert captured["allow_single_shot"] is False

        # 2) 平台还剩 2 次重做 → 不是一次性，正常交卷
        ctx.item = SimpleNamespace(extra={"redo_times": 2})
        captured.clear()
        assert ft.handle_course_work(ctx)[0] is True
        assert captured["single_shot"] is False

        # 3) 次数为 0 + 配置显式放行 → 两个标记都要传下去
        ctx.item = SimpleNamespace(extra={"redo_times": 0})
        ctx.chaoxing.tiku._conf = dict(_Tiku._conf, allow_one_shot_submit="true")
        captured.clear()
        assert ft.handle_course_work(ctx)[0] is True
        assert captured["single_shot"] is True
        assert captured["allow_single_shot"] is True
    finally:
        fw.complete_course_work = real_complete


def test_handle_course_work_warns_irreversible_in_approval_preview():
    from types import SimpleNamespace

    import api.fanya_work as fw
    from api import fanya_task as ft

    class _Tiku:
        _conf = {"provider": "AI", "endpoint": "e", "key": "k", "model": "m"}
        COVER_RATE = 0.0

        def get_submit_params(self):
            return "0"

    def _fake_prepare(session, entry_url, tiku, **kwargs):
        assert kwargs["single_shot"] is True
        return True, "预览文本", {"total": 2, "found": 2, "rate": 1.0,
                                  "payload": {}, "single_shot": True}

    approvals = {}
    real_prepare = fw.prepare_course_work
    ctx = SimpleNamespace(
        study_url={"url": "http://x"}, chaoxing=SimpleNamespace(tiku=_Tiku()),
        client=SimpleNamespace(session=object()), require_approval=True,
        label="作业", item=None,
        register_approval=lambda **kwargs: approvals.update(kwargs) or "a1",
    )
    try:
        fw.prepare_course_work = _fake_prepare
        ok, message = ft.handle_course_work(ctx)
        assert ok is True
        assert "不可逆" in approvals["preview"]
        assert approvals["extra"]["singleShot"] is True
        assert "不可逆" in message
    finally:
        fw.prepare_course_work = real_prepare


# ----------------------------------------------------------------------
# 内置 runner 必须放在**文件末尾**：_run_all() 用 globals() 收集 test_*，
# 放在中间会漏掉它下面定义的所有用例（2026-10-16 才发现）。
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_run_all())
