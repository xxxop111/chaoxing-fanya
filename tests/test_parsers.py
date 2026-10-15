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
from api.fanya_work import _is_refusal  # noqa: E402
from api.fanya_upload import needs_dialogue, copy_is_banned, min_chars  # noqa: E402
from api.fanya_task import _format_progress, _window_reason  # noqa: E402


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


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_run_all())
