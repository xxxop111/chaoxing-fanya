# -*- coding: utf-8 -*-
"""泛雅「作业」(planType 4) 任务点自动完成 —— mooc2 课程作业。

**与章节检测作业不是同一套入口**（实测）：

```
任务点跳转  https://mooc1.chaoxing.com/mooc-ans/mooc2/work/task?courseId=&classId=&cpi=&workId=&enc=
   ↓ 302
作答页      https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork?…&answerId=&standardEnc=&enc=
   ↓ <form action="/mooc-ans/work/addStudentWorkNewWeb?_classId=&courseid=&token=&totalQuestionNum=&wMicroNodeId=">
提交        该 form 的全部字段 + 每题的 answer 字段
```

章节检测作业走的是 ``/mooc-ans/api/work`` + ``work/addStudentWorkNew`` + ``jobid=work-<uuid>``；
课程作业的 ``jobid`` 为空、``knowledgeid=0``，因此**不能**复用 ``Chaoxing.study_work``。

题目 DOM 也与章节作业不同（``decode_questions_info`` 在本页直接抛
``AttributeError``）：题干在 ``div.singleQuesId`` 上，题型由
``input[name=answertype<qid>]`` 给出（0单选/1多选/2填空/3判断/4简答），
答案字段是 ``answer<qid>``（选择题）或 ``textarea#answer<qid>``（简答题）。
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Optional

import requests
from bs4 import BeautifulSoup
from loguru import logger

from api.decode import _extract_title, _get_question_type

MOOC1_DOMAIN = "https://mooc1.chaoxing.com"


class WorkError(Exception):
    """课程作业处理失败。"""


def parse_work_page(html: str) -> dict:
    """解析课程作业作答页。

    返回 ``{"action", "formData", "questions": [{id, typeCode, type, title,
    options, answerFields}]}``。纯函数，便于离线自测。
    """
    soup = BeautifulSoup(html, "lxml")
    form = soup.find("form")
    if form is None:
        raise WorkError("课程作业页面里没有找到提交表单")

    action = form.get("action") or ""
    # 收集表单级字段。注意：不能简单地「名字里含 answer 就跳过」——
    # ``answerwqbid``（题目 id 列表）名字里就含 answer，但它恰恰是必填项，
    # 漏掉它会直接返回「无效的参数：code-1」。
    form_data: dict[str, str] = {}
    per_question_answer = re.compile(r"^answer\d+(_\d+)?$")
    for tag in form.find_all("input"):
        name = tag.get("name")
        if not name or name.startswith("answertype") or per_question_answer.match(name):
            continue
        form_data[name] = tag.get("value", "")

    questions = []
    for block in form.find_all("div", class_="singleQuesId"):
        qid = block.get("data") or re.sub(r"\D", "", block.get("id") or "")
        if not qid:
            continue

        type_input = block.find("input", attrs={"name": f"answertype{qid}"})
        type_code = str(type_input.get("value")) if type_input else ""

        # 选项：章节作业用 ul.mark_letter > li
        ul = block.find("ul")
        options = []
        if ul is not None:
            raw_options = [re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
                           for li in ul.find_all("li")]
            # 空文本的选项**不能整条丢掉**（2026-10-17 事故）：图片选项的 li 里
            # 只有一个 <img>，get_text 得到空串；全过滤掉会让选项数变成 0，
            # 模型答对的字母（A/B/C）就无处可落，整卷被判成「没答上」。
            options = [o or "（图片选项）" for o in raw_options]

        # 题干：去掉选项与作答区后的剩余内容（保留图片以支持 OCR）
        stem = BeautifulSoup(str(block), "lxml")
        for node in stem.find_all("ul"):
            node.decompose()
        for node in stem.find_all("div", class_="stem_answer"):
            node.decompose()
        for node in stem.find_all("script"):
            node.decompose()
        # 题干：整块内容（含 h3 题号、正文段落与图片），已剔除选项与作答区。
        # 不能只取 h3 —— 简答题/公式题的真正题干常常是紧随其后的 <p><img></p>。
        title = _extract_title(stem)
        h3 = stem.find("h3")
        if h3 is not None and not title.strip():
            title = _extract_title(h3)

        # 答案字段：input[name^=answer] 与 textarea[name^=answer]
        answer_fields = {}
        for tag in block.find_all(["input", "textarea"]):
            name = tag.get("name") or ""
            if name.startswith("answer") and not name.startswith("answertype"):
                answer_fields[name] = tag.get("value", "") or ""
            elif name.startswith("answertype"):
                answer_fields[name] = tag.get("value", "")

        questions.append({
            "id": qid,
            "typeCode": type_code,
            "type": _get_question_type(type_code),
            "title": title,
            "options": options,
            "answerFields": answer_fields,
        })

    limit = _hidden_int(soup, _SUBMIT_LIMIT_ID)
    used = _hidden_int(soup, _SUBMIT_COUNT_ID)
    remaining = max(limit - (used or 0), 0) if limit is not None else None

    return {"action": action, "formData": form_data, "questions": questions,
            "answerwqbid": ",".join(q["id"] for q in questions) + ",",
            "submitLimit": limit, "submitCount": used, "submitRemaining": remaining}


# 一次性作业：平台没给重做次数（或次数已经用完）时，交卷不可逆。
# 这类作业默认**只保存草稿**，必须由用户显式批准（或在题库设置里打开
# ``allow_one_shot_submit`` 且整卷都答上）才会真正提交。
# 详见 docs/FANYA_TASK_API.md「一次性作业保护」。
SINGLE_SHOT_MIN_RATE = 1.0
_OPT_IN_TRUE = ("1", "true", "yes", "on")

#: 作答页里写着「本作业允许提交多少次」的两个隐藏域。它们**只有 id、没有 name**
#: （所以按 name 收集表单字段的代码抓不到），平台自己这样读：
#: ``var limitWorkSubmitTimes = $("#limitWorkSubmitTimes").val() || 100;``
#: ``if (parseInt(addTimes) > parseInt(limitWorkSubmitTimes)) { showWorkVerify(); }``
_SUBMIT_LIMIT_ID = "limitWorkSubmitTimes"
_SUBMIT_COUNT_ID = "addTimes"


def _hidden_int(soup: Any, element_id: str) -> Optional[int]:
    """按 id 取隐藏域里的非负整数；找不到或不是纯数字时返回 None。"""
    node = soup.find(id=element_id)
    if node is None:
        return None
    raw = str(node.get("value", "") or "").strip()
    if not re.fullmatch(r"\d+", raw):
        return None
    return int(raw)


def _parse_submit_limit_html(html: str) -> Optional[tuple]:
    """从作答页 HTML 里解出 ``(允许提交次数, 已提交次数)``；读不到返回 None。

    与 :func:`_hidden_int` 同一组字段，只是走正则（给不引入 BeautifulSoup 的
    调用方用，例如任务盘点的 ``_probe_work_redo_times``）。属性顺序不保证，
    所以先定位 ``<input … id="…" …>`` 标签，再在标签里找 ``value``。
    """
    def _value(element_id: str) -> Optional[int]:
        tag = re.search(rf'<input[^>]*id="{element_id}"[^>]*>', html or "")
        if not tag:
            return None
        value = re.search(r'value="(\d+)"', tag.group(0))
        return int(value.group(1)) if value else None

    limit = _value(_SUBMIT_LIMIT_ID)
    if limit is None:
        return None
    return limit, (_value(_SUBMIT_COUNT_ID) or 0)


def _opt_in(value: Any) -> bool:
    """把配置里的 "true"/"1"/"yes"/"on" 读成 True（其余一律 False）。"""
    return str(value or "").strip().lower() in _OPT_IN_TRUE


def submission_blockers(*, total: int, found: int, rate: float, cover_rate: float = 0.0,
                        single_shot: bool = False,
                        allow_single_shot: bool = False,
                        single_shot_note: str = "",
                        ocr_blocked: int = 0) -> list:
    """列出「这一次不该自动交卷」的理由；空列表表示可以交。

    2026-10-16 自查结论：``cover_rate`` 默认是 0（配置里根本没有这个键），
    于是**一道题都没答上也会** ``do_submit=True`` 把空白卷交上去；如果这恰好
    是「平台不给重做」的作业，这一交就不可逆（用户遇到过 44.5 分、redoTimes=0）。
    所以这里补三条硬规则：

    * 一题未命中 → 绝不自动交（空白卷必然低分）；
    * 一次性作业 → 只有用户显式放行、且整卷都答上时才交；
    * ``cover_rate`` 设了阈值时照旧生效。

    ``single_shot_note`` 用来替换「一次性」的理由文案（例如作答页其实写着
    「可提交 100 次」，只是那个次数还没被验证过能不能真的重做）。
    """
    reasons: list = []
    if total and found <= 0:
        reasons.append("一道题都没答上（空白卷必然低分）")
    elif total and found < total:
        # 2026-10-17 事故（概率论第二章章节测验：29 题只答上 4 题，交卷 14 分，
        # 不可重做）之后：**半张卷子一律不交**。要交就交完整卷；答不全就只存
        # 草稿，让人自己补完再交。
        reasons.append(f"还有 {total - found} 题没答上（半张卷子不交）")
    if ocr_blocked:
        reasons.append(
            f"有 {ocr_blocked} 道题的题干是图片，而视觉 OCR 没生效"
            "（设置里供应商/接口地址/模型至少缺一项），模型看不见公式，答案等于蒙")
    if single_shot and not ocr_blocked:
        why = single_shot_note or "平台没给这个作业重做次数（或查不到）"
        if not allow_single_shot:
            reasons.append(f"{why}，交卷不可逆")
        elif found < total:
            reasons.append(f"一次性作业还有 {total - found} 题没答上，交卷不可逆")
    if cover_rate and rate < cover_rate:
        reasons.append(f"覆盖率 {rate:.0%} 低于设定阈值 {cover_rate:.0%}")
    return reasons


def _normalise_letters(answer: str) -> str:
    """把题库答案规整成选项字母串（如 "A"、"AC"）。"""
    letters = re.findall(r"[A-Za-z]", answer or "")
    return "".join(sorted({x.upper() for x in letters}))


_OPTION_PREFIX = re.compile(r"^[A-Za-z]\s*[.．、,，)）:：]?\s*")
_NON_WORD = re.compile(r"[\s，。,.、;；:：()（）\[\]【】\"'`<>&=+*/\\|~^%$#@!?！？、-]+")


def _option_plain(option: str) -> str:
    """选项文本 → 去掉字母前缀与全部标点空白的可比对形式。"""
    text = _OPTION_PREFIX.sub("", str(option or "").strip())
    return _NON_WORD.sub("", text).lower()


def _resolve_choice_answer(answer: str, options: list) -> Optional[str]:
    """把题库答案对应到「选项字母」，对应不上返回 None。

    单选/多选的提交字段只接受选项字母。历史上这里用
    ``_normalise_letters(answer) or str(answer).strip()`` 兜底：题库一旦返回选项原文
    （例如「阻滞增长模型」），整段文本会被塞进单选字段，服务端只能判错，
    而且前端「我的答案」栏也会显示成一段话。本函数只接受能确定对应关系的答案：

    * 纯字母答案（``"B"``、``"AC"``、``"答：B"``）且字母都在选项范围内 → 直接用；
    * 选项原文或「B. 原文」→ 反查字母；
    * **选项读不到时（全是图片选项）** 纯字母答案直接采信 —— 2026-10-17 事故：
      概率论「第二章章节测验」的选项全是公式图，``parse_work_page`` 把空文本选项
      全过滤掉，``allowed`` 变成空列表，于是模型答对的「A」「B」「C」全被判成
      「无法对应选项字母」而留空，25 道题交白卷、整卷 14 分。宁可交一个字母
      （错了也只是这一题错），也不能把答对的题主动丢成空。
    * 其余 → ``None``（宁可留空，也不写错）。
    """
    options = list(options or [])
    allowed = [chr(ord("A") + i) for i in range(len(options))]
    raw = str(answer or "").strip()
    if not raw:
        return None

    letters = "".join(sorted({c.upper() for c in re.findall(r"[A-Za-z]", raw)}))
    core = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", raw)
    if not allowed:
        # 选项未知（图片选项 / 解析不到 ul）：只接受「干净的字母串」，范围 A-H。
        if letters and len(letters) <= 8 and all("A" <= x <= "H" for x in letters) \
                and len(core) <= max(4, 2 * len(letters) + 2):
            logger.warning("题目选项读不出来（可能是图片选项），采信字母答案 {}", letters)
            return letters
        return None
    # 纯字母答案：前后可以带「答：」这类引导词，但中间不能夹着一堆文本
    if letters and all(x in allowed for x in letters) and len(core) <= max(4, 2 * len(letters) + 2):
        return letters

    # 选项原文（可能带前缀，也可能是「B. 原文」）→ 反查字母
    plain_answer = _option_plain(raw)
    if plain_answer:
        best: Optional[tuple[int, str]] = None
        for idx, opt in enumerate(options):
            plain_opt = _option_plain(opt)
            if not plain_opt:
                continue
            if plain_opt == plain_answer:
                return allowed[idx]
            if len(plain_opt) >= 2 and (plain_opt in plain_answer or plain_answer in plain_opt):
                if best is None or len(plain_opt) > best[0]:
                    best = (len(plain_opt), allowed[idx])
        if best is not None:
            return best[1]
    return None


#: 会被「大模型复核」的选择题类型（判断题的答案也要复核，因为错就是 0 分）。
_CHOICE_TYPES = ("single", "multiple", "judgement")

#: 判断题里被当作「正确」的各种写法；其余一律 false。
_JUDGEMENT_TRUE = ("正确", "对", "T", "t", "true", "TRUE", "√", "是", "1")


def _choice_value(q: dict, answer: str) -> Optional[str]:
    """把「题库/模型的答案」规范成能直接写进表单的取值；无法规范时返回 None。

    * 判断题 → ``"true"`` / ``"false"``（页面 JS：``itemAnswer == "true" ? true : false``）。
      ⚠️ 课程作业的判断题取值是字符串 ``"true"/"false"``，写「对/错」会让服务端
      直接拒绝整份提交（实测：提交请求 ~50ms 内失败，且该作业恰好全是判断题）。
    * 单选/多选 → 选项字母（``_resolve_choice_answer``）。
    """
    if q["type"] == "judgement":
        return "true" if str(answer).strip() in _JUDGEMENT_TRUE else "false"
    return _resolve_choice_answer(str(answer), q["options"])


def _ask_verifier(verifier: Any, q: dict) -> Optional[str]:
    """向大模型复核一道题。失败、拒答、空答案都返回 None（视为「没有意见」）。"""
    if verifier is None:
        return None
    try:
        answer = verifier.query({
            "title": q["title"],
            "type": q["type"],
            "options": "\n".join(q["options"]),
        })
    except Exception as exc:  # noqa: BLE001 - 复核失败不影响主答案
        logger.debug("大模型复核失败（{}）: {}", q["id"], exc)
        return None
    if not answer or _is_refusal(str(answer)):
        return None
    return str(answer).strip()


_IMAGE_HOSTS = ("p.ananas.chaoxing.com", "p.cldisk.com", "p.cdn.chaoxing.com")

# 与 api/vision_ocr.py 的 PROVIDER_DEFAULTS 对齐：这些供应商不填接口地址也能用。
_OCR_PROVIDER_ENDPOINT = {
    "openai": "https://api.openai.com/v1/chat/completions",
    "claude": "https://api.anthropic.com/v1/messages",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    "siliconflow": "https://api.siliconflow.cn/v1/chat/completions",
}

def ocr_gap() -> str:
    """返回「OCR 为什么用不了」的人话说明；可用时返回空串。

    两条链路任一条可用即可：本地 PaddleOCR（``CHAOXING_ENABLE_OCR``）或设置页里
    配的视觉大模型。2026-10-17 事故复盘：用户配了 provider=openai_compatible
    却没填接口地址，OCR 一直是关的，日志里只有一条 WARNING，整卷就这么瞎猜着
    交了 14 分。所以这里必须能被上层问出来，并且说清楚缺哪一项。
    """
    try:
        from api.decode import ENABLE_LOCAL_OCR
        from api.vision_ocr import is_vision_ocr_enabled
    except Exception:  # noqa: BLE001 - 探测失败按「不可用」处理
        return "OCR 组件没加载起来"
    if ENABLE_LOCAL_OCR or is_vision_ocr_enabled():
        return ""
    provider = os.environ.get("CHAOXING_VISION_OCR_PROVIDER", "").strip().lower()
    key = os.environ.get("CHAOXING_VISION_OCR_KEY", "").strip()
    endpoint = os.environ.get("CHAOXING_VISION_OCR_ENDPOINT", "").strip()
    if not provider:
        return "设置里没选 OCR 供应商"
    if not key:
        return "OCR 密钥是空的"
    if not endpoint and provider not in _OCR_PROVIDER_ENDPOINT:
        return f"供应商选了 {provider} 但没填接口地址"
    return "OCR 配置没生效"


def _ocr_available() -> bool:
    return not ocr_gap()



def _question_has_image(q: dict) -> bool:
    """题干或选项里有没有我们读不出来的图片。"""
    text = str(q.get("title") or "")
    if "<img" in text or any(h in text for h in _IMAGE_HOSTS):
        return True
    for opt in q.get("options") or []:
        opt = str(opt)
        if opt.strip() == "（图片选项）" or "<img" in opt or any(h in opt for h in _IMAGE_HOSTS):
            return True
    return False


def build_submission(parsed: dict, tiku: Any,
                     verifier: Any = None) -> tuple[dict, int, int, list]:
    """按题目查询题库并拼出提交字段。

    返回 ``(提交字段, 命中题数, 总题数, 逐题明细)``。未命中的题目保持空答案。

    ``verifier`` 是一份**独立的大模型题库**（通常是 AI），只在主题库不是大模型时
    传入：选择题会被复核一次，两边规范成选项字母后不一致时**采用模型的答案**。
    这么做的理由（实测 2026-10-16）：网络题库按题干模糊匹配，串题时会给出「语义
    完全不相干」的答案（例如问 Logistic 曲线形状却答「周期正弦曲线」），而这些
    字母答案能通过 check_answer 的宽松校验，于是整份作业被交成 4/9。大模型在
    同一份卷子上稳定 9/9，所以两个来源冲突时以大模型为准。
    """
    payload = dict(parsed["formData"])
    # 必填：题目 id 列表（缺失会被服务端判为「无效的参数」）
    if parsed.get("answerwqbid"):
        payload["answerwqbid"] = parsed["answerwqbid"]
    questions = parsed["questions"]
    found = 0
    details: list[dict] = []
    overridden = 0
    model_filled = 0
    # OCR 不可用的具体原因只探一次，逐题写进明细里，日志和审批预览都能说清楚。
    _ocr_gap_reason = ocr_gap()

    for q in questions:
        qid = q["id"]
        answer: Optional[str] = None
        reason = "未命中"
        from_model = False
        # 题干/选项是图片而 OCR 没起作用时，任何「答案」都是蒙的：直接把这一题
        # 判成没答上，并记下 ocrBlocked，让整卷被完整性闸门拦下来（见
        # submission_blockers 的 ocr_blocked）。绝不能再出现「看不见公式也照样
        # 把 29 题全交上去、14 分不可重做」这种事故。
        if _ocr_gap_reason and _question_has_image(q):
            for name, original in q["answerFields"].items():
                payload.setdefault(name, original)
            details.append({"qid": qid, "type": q["type"], "title": q["title"],
                            "answer": None, "answered": False,
                            "reason": "题干是图片且 OCR 没生效：%s" % _ocr_gap_reason,
                            "ocrBlocked": True})
            logger.warning("题目 {} 的题干/选项是图片，但 OCR 不可用（{}），按未命中处理",
                           qid, _ocr_gap_reason)
            continue
        if tiku is not None:
            try:
                answer = tiku.query({
                    "title": q["title"],
                    "type": q["type"],
                    "options": "\n".join(q["options"]),
                })
            except Exception as exc:  # noqa: BLE001 - 单题失败不应中断
                logger.debug("题库查询失败（{}）: {}", qid, exc)

        # 题库有时会把「我看不到题目」这类**拒答/元回复**当成答案返回。
        # 这种文本绝不能写进作业（曾经真的把「请提供题目的文字描述」交上去过）。
        if answer and _is_refusal(str(answer)):
            logger.warning("题库对题目 {} 返回了拒答文本，按未命中处理: {}",
                           qid, str(answer)[:60])
            answer = None
            reason = "题库拒答"

        # 大模型：有主答案的选择题做复核，**没有主答案的题直接补答**。
        # 2026-10-17 事故：概率论「第二章章节测验」29 题题库只命中 4 题，
        # 其余 25 题留空交上去，整卷 14 分（且该作业不可重做）。从此
        # 「题库没命中就留空」不再是可接受的默认——先让模型顶上，模型也
        # 答不上才算没答上，而没答上的卷子根本不允许交（见
        # fanya_task.handle_course_work 的完整性闸门）。
        model_answer = None
        if verifier is not None and (answer is None or q["type"] in _CHOICE_TYPES):
            model_answer = _ask_verifier(verifier, q)
        if answer and model_answer and q["type"] in _CHOICE_TYPES:
            primary = _choice_value(q, str(answer))
            checked_value = _choice_value(q, model_answer)
            if primary and checked_value and primary != checked_value:
                logger.warning("题目 {} 题库答案与大模型复核不一致（题库 {} / 模型 {}），"
                               "采用模型答案", qid, primary, checked_value)
                answer = model_answer
                overridden += 1
        elif answer is None and model_answer:
            answer = model_answer
            from_model = True
            model_filled += 1
            logger.info("题目 {} 题库未命中，由大模型补答：{}", qid, str(model_answer)[:60])

        if answer:
            found += 1
        # 无论是否命中都要带上字段，否则表单校验可能不通过；
        # 注意这里要保留原始值（例如 answertype<qid>=4），不能用空串覆盖。
        for name, original in q["answerFields"].items():
            payload.setdefault(name, original)

        if not answer:
            details.append({"qid": qid, "type": q["type"], "title": q["title"],
                            "answer": None, "answered": False, "reason": reason})
            continue

        if q["type"] in ("single", "judgement", "multiple"):
            value = _choice_value(q, str(answer))
            if value is None:
                # 答案既不是选项字母，也对不上任何选项原文：这一题宁可留空。
                # 旧行为是把整段文本写进单选字段，服务端判错，用户看到的
                # 「我的答案」也变成一段话（曾经真的发生过）。
                logger.warning("题目 {} 的答案无法对应到选项字母，本題按未命中处理：{}",
                               qid, str(answer)[:60])
                found -= 1
                details.append({"qid": qid, "type": q["type"], "title": q["title"],
                                "answer": str(answer).strip(), "answered": False,
                                "reason": "答案无法对应选项"})
                continue
            for name in q["answerFields"]:
                if name.startswith("answer") and not name.startswith("answertype"):
                    payload[name] = value
        elif q["type"] == "completion":
            parts = re.split(r"[\s,，、;；]+", str(answer).strip())
            keys = [n for n in q["answerFields"]
                    if n.startswith("answer") and not n.startswith("answertype")]
            for idx, key in enumerate(sorted(keys)):
                if idx < len(parts):
                    payload[key] = parts[idx]
        else:  # shortanswer 及其它：整段文本写入第一个 answer 字段
            for name in q["answerFields"]:
                if name.startswith("answer") and not name.startswith("answertype"):
                    payload[name] = str(answer).strip()
                    break
        details.append({"qid": qid, "type": q["type"], "title": q["title"],
                        "answer": str(answer).strip(), "answered": True,
                        "source": "模型" if from_model else "题库"})

    rate = (found / len(questions)) if questions else 0.0
    logger.info("课程作业共 {} 题，命中 {} 题（覆盖率 {:.0%}）{}",
                len(questions), found, rate,
                f"，其中大模型补答 {model_filled} 题" if model_filled else "")
    if overridden:
        logger.info("其中 {} 题按大模型复核结果改写过（两个来源答案不一致）", overridden)
    blank = [d for d in details if not d.get("answered")]
    if blank:
        logger.warning("仍有 {} 题没有答案（{}）：这些题会留空，整卷不允许交",
                       len(blank),
                       "；".join(str(d.get("title") or d.get("qid"))[:30] for d in blank[:8]))
    return payload, found, len(questions), details


# 题库把「我看不到题目」这类回复当成答案时的识别词
_REFUSAL_MARKERS = (
    "无法看到", "无法查看", "看不到", "无法识别", "看不清", "无法读取",
    "请提供题目", "请提供文字", "请重新上传", "重新上传清晰", "题目图片",
    "图片链接", "无法解答", "无法回答", "我无法", "抱歉我",
    "作为一个AI", "作为AI", "没有具体的题目", "没有题目文字",
)


def _is_refusal(text: str) -> bool:
    """判断题库返回的是不是「拒答 / 元回复」而不是真正的答案。"""
    if not text:
        return False
    stripped = text.strip()
    # 真正的答案很少会这么长又全是这种口吻；命中任意标记即视为拒答
    return any(marker in stripped for marker in _REFUSAL_MARKERS)


def submit_work(session: requests.Session, action: str, payload: dict,
                referer: str = "", timeout: float = 30.0) -> tuple[bool, str]:
    """提交（或保存）课程作业。

    页面自己的 ``saveAnswer`` / ``confirmSubmitWork`` 都会把这几项拼到 action 后面
    （``ua=pc&formType=post&saveStatus=1&version=1``），缺了会被服务端判为
    「无效的提交，参数异常」。提交与保存共用同一套参数，区别只在表单里的
    ``pyFlag``（``1``=保存草稿，空=正式提交）。

    注意：正式提交会把答案真实写入作业记录，不可撤销。
    """
    url = action if action.startswith("http") else f"{MOOC1_DOMAIN}{action}"
    separator = "&" if "?" in url else "?"
    url = f"{url}{separator}ua=pc&formType=post&saveStatus=1&version=1"

    headers = {
        "Referer": referer or url,
        "X-Requested-With": "XMLHttpRequest",
        "Origin": MOOC1_DOMAIN,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        resp = session.post(url, data=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"提交作业请求失败: {exc}"
    if resp.status_code != 200:
        return False, f"提交作业返回 HTTP {resp.status_code}"
    try:
        body = resp.json()
    except ValueError:
        return False, f"提交作业返回非 JSON: {resp.text[:200]}"
    if body.get("status"):
        return True, str(body.get("msg") or "提交成功")
    return False, str(body.get("msg") or body)


def check_work_gate(session: requests.Session, html: str, referer: str,
                    timeout: float = 30.0) -> tuple[bool, str]:
    """提交前的 ``/mooc-ans/work/validate`` 闸门。

    返回 ``(是否可继续, 说明)``。页面自己提交前也会调这个接口，因此必须复现，
    否则行为与浏览器不一致：

    * ``1`` = 账号异常 → 放弃
    * ``2`` = 需要验证码 → 无法全自动完成（会把验证码图片地址提示出来）
    * ``3`` = 可直接提交
    """
    fields = parse_hidden_inputs(html)
    course_id = fields.get("courseId") or fields.get("courseid")
    class_id = fields.get("classId") or fields.get("clazzid") or fields.get("clazzId")
    cpi = fields.get("cpi")
    if not (course_id and class_id and cpi):
        # 拿不到参数就不阻断（老版本页面可能没有这个闸门）
        logger.debug("作业页缺少 courseId/classId/cpi，跳过 validate 闸门")
        return True, ""

    url = "https://mooc1.chaoxing.com/mooc-ans/work/validate"
    try:
        resp = session.get(url, params={"courseId": course_id, "classId": class_id,
                                        "cpi": cpi},
                           headers={"Referer": referer,
                                    "X-Requested-With": "XMLHttpRequest"},
                           timeout=timeout)
        status = (resp.json() or {}).get("status")
    except Exception as exc:  # noqa: BLE001 - 闸门不可用时不应阻断主流程
        logger.debug("调用作业 validate 闸门失败（忽略）: {}", exc)
        return True, ""

    if status == 3:
        return True, ""
    if status == 2:
        return False, ("该作业提交需要输入验证码，无法自动完成"
                       "（验证码图片：/mooc-ans/verifyCode/stuWork），请手动提交")
    if status == 1:
        return False, "作业提交被拒绝：账号状态异常，请手动在网页上提交"
    return True, ""


def parse_hidden_inputs(html: str) -> dict:
    """抓取页面隐藏域（id 或 name 作键）。"""
    fields: dict[str, str] = {}
    for key, value in re.findall(
            r'<input[^>]*?(?:id|name)\s*=\s*["\']([^"\']+)["\'][^>]*?value\s*=\s*["\']([^"\']*)["\']',
            html, re.I):
        fields.setdefault(key, value)
    for value, key in re.findall(
            r'<input[^>]*?value\s*=\s*["\']([^"\']*)["\'][^>]*?(?:id|name)\s*=\s*["\']([^"\']+)["\']',
            html, re.I):
        fields.setdefault(key, value)
    return fields


def complete_course_work(session: requests.Session, entry_url: str, tiku: Any, *,
                         save_only: bool = False, cover_rate: float = 0.0,
                         on_log: Optional[Callable] = None, timeout: float = 30.0,
                         verifier: Any = None, single_shot: bool = False,
                         allow_single_shot: bool = False,
                         trust_page_limit: bool = False
                         ) -> tuple[bool, str]:
    """完成一个课程作业任务点。

    ``save_only=True``（对应题库设置 ``submit=1``）只保存草稿不提交；
    ``cover_rate`` 是最低命中率（0~1 的小数），未达到时同样只保存不提交，
    避免把大片空白答案交上去。

    ``single_shot=True``（平台不给重做次数、或查不到）时，本次**只保存草稿**，
    除非 ``allow_single_shot=True`` 且整卷都答上 —— 交错了没有第二次机会。
    """
    log = on_log or (lambda msg: logger.info(msg))

    try:
        resp = session.get(entry_url, headers={"Referer": entry_url}, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"打开作业页失败: {exc}"
    if resp.status_code != 200:
        return False, f"打开作业页返回 HTTP {resp.status_code}"

    html = resp.content.decode("utf-8", errors="replace")
    final_url = resp.url
    if "已过期" in html or "已结束" in html:
        return False, "该作业已过期或已结束"

    # 已提交的作业会 302 到 mooc2/work/view（「作业详情」，没有作答表单）
    if "work/view" in final_url or "作业详情" in html:
        return False, "该作业已经提交过了，无需重复作答"

    try:
        parsed = parse_work_page(html)
    except WorkError as exc:
        # 没有作答表单通常意味着已提交 / 已批阅
        if "作业详情" in html or "查看" in html and "重做" in html:
            return False, "该作业已经提交过了，无需重复作答"
        return False, str(exc)

    # 提交前闸门：页面自身在提交前会调用 /mooc-ans/work/validate。
    # 1=账号异常（放弃）  2=需要验证码（无法全自动）  3=可直接提交
    gate_ok, gate_msg = check_work_gate(session, html, final_url, timeout)
    if not gate_ok:
        return False, gate_msg

    questions = parsed["questions"]
    if not questions:
        return False, "未在作业页解析到任何题目"

    prepared = build_prepared(parsed, final_url, tiku, cover_rate=cover_rate, log=log,
                              verifier=verifier, single_shot=single_shot,
                              allow_single_shot=allow_single_shot,
                              trust_page_limit=trust_page_limit)
    if prepared["total"] == 0:
        return False, "作业页没有题目"

    # 一题都没答上时连草稿都不保存：空白卷对用户没有任何价值，而某些课程把
    # pyFlag=1 也当作一次作答（不想在这种作业上冒任何风险）。
    if prepared["found"] == 0:
        return False, (f"{prepared['total']} 题全部未命中（可能是纯图片题），"
                       "没有可提交的答案，本次不保存也不提交")

    if save_only:
        prepared["do_submit"] = False
        log("题库设置为「只保存不提交」，本次不提交")

    ok, msg = submit_prepared(session, prepared, timeout=timeout)
    if not ok:
        return False, msg
    if prepared.get("blocked_reason"):
        msg = f"{msg}（未自动交卷：{prepared['blocked_reason']}）"
    return True, msg


def build_prepared(parsed: dict, referer: str, tiku: Any, *, cover_rate: float = 0.0,
                   log: Optional[Callable] = None, verifier: Any = None,
                   single_shot: bool = False, allow_single_shot: bool = False,
                   trust_page_limit: bool = False) -> dict:
    """把「读到的题目 + 题库答案」整理成一个可提交/可送审的载荷（不做任何写操作）。

    ``single_shot=True`` 表示这是「平台不给重做次数（或查不到）」的作业：
    除非 ``allow_single_shot`` 显式放行且整卷都答上，否则 ``do_submit`` 一律
    为 False（只保存草稿）。判断依据见 :func:`submission_blockers`。

    ``trust_page_limit=True``（题库设置 ``allow_page_submit_times``）才允许
    「作答页写着可提交 N≥2 次」直接把一次性标记翻掉；默认不信 —— 页面上的
    提交次数与教师给的「重做次数」是不是同一件事，2026-10-16 的语料还没有
    验证过（见 docs/FANYA_TASK_API.md「作业允许提交几次」）。
    """
    log = log or (lambda m: logger.info(m))
    payload, found, total, details = build_submission(parsed, tiku, verifier=verifier)
    rate = (found / total) if total else 0.0

    # 作答页自己也写着「允许提交多少次」（id-only 隐藏域，2026-10-16 语料实测）：
    #   剩余 <= 1 次  → 一定是一次性作业，标记坐实；
    #   剩余 >= 2 次  → 页面说还能再交，但它与「教师给的重做次数」是否同一件事
    #                   没有验证过，所以默认仍按一次性处理，只是把数字告诉用户。
    remaining = parsed.get("submitRemaining")
    limit, used = parsed.get("submitLimit"), parsed.get("submitCount")
    single_shot_note = ""
    if remaining is not None:
        if remaining <= 1:
            if not single_shot:
                log(f"作答页写着本作业只允许提交 {limit} 次"
                    f"（已提交 {used or 0} 次），按一次性作业处理")
            single_shot = True
        elif trust_page_limit:
            if single_shot:
                log(f"作答页写着本作业可提交 {limit} 次（已提交 {used or 0} 次，"
                    f"剩 {remaining} 次）：交错了还能在课程页面重做")
            single_shot = False
        else:
            single_shot = True
            single_shot_note = (f"作答页写着本作业可提交 {limit} 次（已提交 {used or 0} 次，"
                                f"剩 {remaining} 次），但能不能真的重做还没验证过")
            log(single_shot_note + "：本次仍按一次性作业处理（只保存草稿）。"
                "想按页面次数直接交卷，可在题库设置里加 allow_page_submit_times=true")

    reasons = submission_blockers(total=total, found=found, rate=rate,
                                  cover_rate=cover_rate, single_shot=single_shot,
                                  allow_single_shot=allow_single_shot,
                                  single_shot_note=single_shot_note,
                                  ocr_blocked=sum(1 for d in details if d.get("ocrBlocked")))
    if not reasons and remaining is not None and remaining >= 2 and found < total:
        log(f"本作业还有 {total - found} 题没答上，得分可能偏低；"
            f"平台还剩 {remaining} 次提交机会，交完可在课程页面重做")
    if reasons:
        log("本次不自动提交：" + "；".join(reasons)
            + "（改为只保存草稿，可在课程页面核对后自己提交）")
    return {
        "action": parsed["action"],
        "referer": referer,
        "payload": payload,
        "found": found,
        "total": total,
        "rate": rate,
        "details": details,
        "ocr_blocked": sum(1 for d in details if d.get("ocrBlocked")),
        # 不满足「可以自动交卷」的条件时默认不提交；
        # 是否提交最终由调用方（含用户审批）决定。
        "do_submit": not reasons,
        "blocked_reason": "；".join(reasons),
        "single_shot": bool(single_shot),
        "single_shot_note": single_shot_note,
        "submitLimit": parsed.get("submitLimit"),
        "submitCount": parsed.get("submitCount"),
        "submitRemaining": remaining,
        "answerwqbid": parsed.get("answerwqbid", ""),
    }


def summarize_prepared(prepared: dict, limit: int = 6) -> str:
    """把送审载荷整理成人看的预览文本。"""
    remaining = prepared.get("submitRemaining")
    if remaining is None:
        chance = "（平台允许提交的次数：页面上读不到，按只有一次处理）"
    elif remaining <= 1:
        chance = f"（平台还剩 {remaining} 次提交机会：交卷不可逆，请先核对答案）"
    else:
        chance = (f"（平台还剩 {remaining} 次提交机会"
                  f"：可提交 {prepared.get('submitLimit')} 次、已提交 "
                  f"{prepared.get('submitCount') or 0} 次，交错还能重做）")
    lines = [f"共 {prepared['total']} 题，题库命中 {prepared['found']} 题"
             f"（覆盖率 {prepared['rate']:.0%}）{chance}"]
    for row in prepared.get("details", [])[:limit]:
        mark = "✓" if row.get("answered") else "✗"
        answer = (row.get("answer") or "(未命中)").replace("\n", " ")
        lines.append(f"{mark} [{row.get('type')}] {row.get('title', '')[:60]} → {answer[:60]}")
    if len(prepared.get("details", [])) > limit:
        lines.append(f"… 其余 {len(prepared['details']) - limit} 题见日志")
    return "\n".join(lines)


def submit_prepared(session: requests.Session, prepared: dict, *,
                    force_submit: Optional[bool] = None,
                    timeout: float = 30.0) -> tuple[bool, str]:
    """提交（或保存）已经准备好的作业载荷。

    ``force_submit`` 为 ``None`` 时沿用 ``prepared["do_submit"]``；
    为 ``True`` 表示用户已审批通过，正式提交（``pyFlag`` 置空）。
    """
    do_submit = prepared["do_submit"] if force_submit is None else bool(force_submit)
    payload = dict(prepared["payload"])
    # pyFlag=1 表示「保存」，空串表示「提交」（与项目既有 study_work 一致）
    payload["pyFlag"] = "" if do_submit else "1"
    ok, msg = submit_work(session, prepared["action"], payload,
                          referer=prepared.get("referer", ""), timeout=timeout)
    if not ok:
        return False, f"{'提交' if do_submit else '保存'}作业失败: {msg}"
    return True, (f"作业已提交（{prepared['total']} 题，命中 {prepared['found']} 题）"
                  if do_submit else
                  f"作业已保存为草稿（{prepared['total']} 题，命中 {prepared['found']} 题）")


def prepare_course_work(session: requests.Session, entry_url: str, tiku: Any, *,
                        cover_rate: float = 0.0, on_log: Optional[Callable] = None,
                        timeout: float = 30.0, verifier: Any = None,
                        single_shot: bool = False, allow_single_shot: bool = False,
                        trust_page_limit: bool = False
                        ) -> tuple[bool, str, dict]:
    """**只准备、不写任何东西**：读题 + 查题库，返回送审载荷。

    用于「生成后暂停、逐条审批」流程 —— 在用户批准之前，既不保存草稿也不提交。
    ``single_shot`` 会随载荷带给审批界面：一次性作业在批准前必须让用户看到
    「交上去就不能改了」。
    """
    log = on_log or (lambda m: logger.info(m))
    try:
        resp = session.get(entry_url, headers={"Referer": entry_url}, timeout=timeout)
    except requests.RequestException as exc:
        return False, f"打开作业页失败: {exc}", {}
    if resp.status_code != 200:
        return False, f"打开作业页返回 HTTP {resp.status_code}", {}

    html = resp.content.decode("utf-8", errors="replace")
    final_url = resp.url
    if "已过期" in html or "已结束" in html:
        return False, "该作业已过期或已结束", {}
    if "work/view" in final_url or "作业详情" in html:
        return False, "该作业已经提交过了，无需重复作答", {}
    try:
        parsed = parse_work_page(html)
    except WorkError as exc:
        return False, str(exc), {}

    if not parsed["questions"]:
        return False, "未在作业页解析到任何题目", {}

    gate_ok, gate_msg = check_work_gate(session, html, final_url, timeout)
    if not gate_ok:
        return False, gate_msg, {}

    prepared = build_prepared(parsed, final_url, tiku, cover_rate=cover_rate, log=log,
                              verifier=verifier, single_shot=single_shot,
                              allow_single_shot=allow_single_shot,
                              trust_page_limit=trust_page_limit)
    prepared["entry_url"] = entry_url
    return True, summarize_prepared(prepared), prepared
