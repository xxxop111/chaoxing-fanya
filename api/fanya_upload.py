# -*- coding: utf-8 -*-
"""泛雅 AI实践「上传作品」(``data.type == 1``) 自动完成。

协议来源：``_findings_ai.md`` §2 + ``_probe/file-mode_answer.js`` 静态分析，
并用**只读**方式从一个已完成记录里核对了平台真实存储结构：

```json
"files": [{"fileName": "…jpg", "fileObjectId": "…", "fileType": "jpg"}, …]
```

调用序列（与 ``initAnswerByFileCard`` 一致，请求体为
``{type, files: JSON.stringify([{fileCardId, files:[{fileType,fileObjectId,name}]}])}``）：

1. ``GET  answer/load-data``                     → type=1 / rules / recordUuid / aiEnc
2. ``POST upload-ans/upload/uploadNew``           → 先把文件传进超星网盘，拿 fileObjectId
3. ``POST answer/init`` 或 ``answer/update-file`` → 挂到记录上（已有记录则更新）
4. ``GET  answer/check-file-status``              → 等解析（图片通常直接是 "1"）
5. ``POST answer/submit``                         → 交卷
6. ``POST /ai-ans/.../evaluate/stream``           → 触发 AI 评分（SSE）

**作品内容怎么来**：任务要求通常包含「AI 提示词 + 说明 + 反思」这类分项。
本模块按题目要求分节生成，并渲染成 PNG 图片（Pillow + 微软雅黑，支持中文换行）。

⚠️ **关于「禁止复制 AI 输出」**：有些题目原文明确要求
「禁止直接大段复制 AI 输出，用自己的话整理」。这类题默认**只生成草稿不提交**，
因为直接上传等于违反题目自己写的规则；需要用户显式开启 ``upload_ai_text`` 覆盖。
"""

from __future__ import annotations

import io
import json
import os
import re
import time
from typing import Any, Callable, Optional

import requests
from loguru import logger

from api import fanya_ladder as ld

MOOC2 = ld.MOOC2
AI_ANS = ld.AI_ANS
PAN_UPLOAD = "https://mooc1.chaoxing.com/upload-ans/upload/uploadNew"
MODE_UPLOAD = 1

# 中文字体候选
_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
]

# 判定「禁止复制 AI 输出」的标记
_COPY_BAN_MARKERS = (
    "禁止直接大段复制", "禁止复制", "用自己的话", "用自己的语言",
    "不能只抄", "不要直接复制", "禁止抄袭",
)
# 判定「需要提交对话实录」的标记
_DIALOGUE_MARKERS = ("对话实录", "持续对话", "轮问答", "与AI的对话", "与 AI 的对话", "对话过程")


class UploadError(Exception):
    """上传作品流程失败。"""


class UploadAbort(UploadError):
    """出于安全/规则考虑主动中止。"""


# ----------------------------------------------------------------------
# 文本 → PNG
# ----------------------------------------------------------------------


def _font(size: int):
    from PIL import ImageFont

    for path in _FONTS:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _wrap_cjk(text: str, font, max_width: int) -> list[str]:
    """按像素宽度折行；中文逐字、英文尽量按词。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw.strip():
            lines.append("")
            continue
        current = ""
        for token in re.findall(r"[A-Za-z0-9_\-\.]+|\s|.", raw):
            candidate = current + token
            if font.getlength(candidate) <= max_width:
                current = candidate
            else:
                if current.strip():
                    lines.append(current.rstrip())
                current = token.lstrip() if token.isspace() else token
        if current.strip():
            lines.append(current.rstrip())
    return lines


def render_pngs(title: str, body: str, *, max_pages: int = 9,
                page_lines: int = 34) -> list[bytes]:
    """把标题 + 正文渲染成分页 PNG（白底黑字，适合当作品截图上传）。"""
    from PIL import Image, ImageDraw

    width, margin, title_size, body_size = 1240, 56, 40, 28
    title_font, body_font = _font(title_size), _font(body_size)
    line_h = int(body_size * 1.65)

    lines = _wrap_cjk(body, body_font, width - margin * 2)
    per_page = page_lines
    pages: list[bytes] = []
    total = max(1, (len(lines) + per_page - 1) // per_page)
    total = min(total, max_pages)
    for page in range(total):
        chunk = lines[page * per_page:(page + 1) * per_page]
        height = margin * 2 + title_size + 30 + max(1, len(chunk)) * line_h + 40
        img = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(img)
        draw.text((margin, margin), title, fill="#1a1a1a", font=title_font)
        draw.line([(margin, margin + title_size + 14),
                   (width - margin, margin + title_size + 14)], fill="#c8c8c8", width=2)
        y = margin + title_size + 30
        for line in chunk:
            draw.text((margin, y), line, fill="#222222", font=body_font)
            y += line_h
        draw.text((width - margin - 120, height - 34),
                  f"第 {page + 1}/{total} 页", fill="#888888", font=_font(20))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        pages.append(buf.getvalue())
    return pages


# ----------------------------------------------------------------------
# 内容生成
# ----------------------------------------------------------------------


def needs_dialogue(requirement: str) -> bool:
    return any(m in requirement for m in _DIALOGUE_MARKERS)


def copy_is_banned(requirement: str) -> bool:
    return any(m in requirement for m in _COPY_BAN_MARKERS)


def min_chars(requirement: str) -> int:
    m = re.search(r"不少于\s*(\d{2,5})\s*字", requirement)
    return int(m.group(1)) if m else 0


_DIALOGUE_SYSTEM = (
    "你是一名严谨的思考者，正在与生成式AI进行多轮对话。"
    "你的目标不是拿到「正确答案」，而是像审稿人一样追问、质疑、检验和反驳。"
    "只输出你这一轮的提问，不要任何旁白或解释。"
)
_AI_SYSTEM = (
    "你是一个生成式AI助手。请针对用户的问题给出有条理、有依据的回答，"
    "300 字以内，可以用分点，但不要输出 Markdown 标题符号。"
)
_WORK_SYSTEM = (
    "你是一名大学生，正在完成课程平台上的 AI 实践作品。"
    "请按题目要求逐项作答，语言朴实、条理清楚、有自己的判断，"
    "不要输出任何解释性旁白，不要使用 Markdown 标题符号。"
)

# 要求的五类提问（题目原文指定的追问类型）
_QUESTION_TYPES = [
    "追问证据：请说明你得出这个结论的依据是什么，能否给出具体数据或来源？",
    "质疑假设：你的论证建立在什么前提之上？这个前提是否一定成立？",
    "要求替代视角：这个问题有哪些反面观点？支持它们的理由是什么？",
    "识别谬误：请检查你上面的推理中是否存在逻辑错误或跳步。",
    "检验边界：在什么情况下你的结论会不成立？请给出反例或边界条件。",
]


def build_dialogue(generator: Any, requirement: str, *, turns: int = 5,
                   on_log: Optional[Callable] = None) -> list[tuple[str, str]]:
    """真的和 AI 聊一轮多轮对话，返回 ``[(学生提问, AI回答), …]``。

    这是本题的交付物本身（对话实录），所以是**真实进行的对话**而非编造。
    """
    log = on_log or (lambda m: None)
    topic_prompt = ("请基于下面这个课程任务，给我一个具有争议性或需要深入分析的具体话题，"
                    "只输出话题本身（20 字以内）：\n" + requirement[:800])
    topic = (generator.generate_text(_WORK_SYSTEM, topic_prompt,
                                     temperature=0.8, max_tokens=80) or "").strip()
    topic = topic.split("\n")[0][:40] or "人工智能是否会削弱人的独立思考能力"
    log(f"对话话题：{topic}")

    transcript: list[tuple[str, str]] = []
    history: list[str] = []
    for i in range(max(5, turns)):
        kind = _QUESTION_TYPES[i % len(_QUESTION_TYPES)]
        ask = (f"话题：{topic}\n这是第 {i + 1} 轮。请按「{kind.split('：')[0]}」的方式"
               f"提出一个具体、有锋芒的问题（40 字以内，只输出问题本身）。")
        if history:
            ask += "\n\n此前的对话：\n" + "\n".join(history[-3:])
        question = (generator.generate_text(_DIALOGUE_SYSTEM, ask,
                                            temperature=0.8, max_tokens=120) or "").strip()
        if not question:
            raise UploadAbort(f"第 {i + 1} 轮未能生成提问，已中止")
        answer = (generator.generate_text(_AI_SYSTEM, f"问题：{question}",
                                          temperature=0.6, max_tokens=700) or "").strip()
        if not answer:
            raise UploadAbort(f"第 {i + 1} 轮未收到 AI 回答，已中止")
        transcript.append((question, answer))
        history.append(f"我：{question}")
        history.append(f"AI：{answer[:200]}")
        log(f"第 {i + 1} 轮完成（提问 {len(question)} 字 / 回答 {len(answer)} 字）")
    return transcript, topic


def render_dialogue(transcript: list[tuple[str, str]], topic: str) -> str:
    """把对话整理成适合渲染成图片的纯文本。"""
    out = [f"话题：{topic}", ""]
    for idx, (q, a) in enumerate(transcript, 1):
        out.append(f"【第 {idx} 轮】")
        out.append(f"我：{q}")
        out.append(f"AI：{a}")
        out.append("")
    return "\n".join(out)


def build_work_text(generator: Any, requirement: str, *,
                    on_log: Optional[Callable] = None) -> str:
    """生成「文本作品」（≥题目要求字数）。"""
    log = on_log or (lambda m: None)
    want = min_chars(requirement)
    prompt = (f"题目要求：\n{requirement}\n\n"
              f"请提交一份不少于 {max(want, 300) + 120} 字的作答，"
              "分点组织，结合自己的专业背景，语言用自己的话。")
    text = (generator.generate_text(_WORK_SYSTEM, prompt,
                                    temperature=0.7, max_tokens=2600) or "").strip()
    if not text:
        raise UploadAbort("AI 未能生成作品正文，已中止")
    log(f"已生成作品正文 {len(text)} 字（题目要求不少于 {want or '—'} 字）")
    return text


# ----------------------------------------------------------------------
# 上传与提交
# ----------------------------------------------------------------------


def upload_to_pan(session: requests.Session, fields: dict, filename: str,
                  blob: bytes, mime: str = "image/png",
                  timeout: float = 90.0) -> str:
    """把文件传进超星网盘，返回 ``fileObjectId``。"""
    params = {"t": fields.get("currentTime") or "",
              "enc2": fields.get("currentUploadEnc") or "",
              "userId": fields.get("currentUserId") or ""}
    try:
        resp = session.post(PAN_UPLOAD, params=params,
                            files={"file": (filename, blob, mime)}, timeout=timeout)
    except requests.RequestException as exc:
        raise UploadError(f"上传网盘请求失败: {exc}") from exc
    if resp.status_code != 200:
        raise UploadError(f"上传网盘返回 HTTP {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        raise UploadError(f"上传网盘返回非 JSON: {resp.text[:200]}") from None
    if body.get("state") != "SUCCESS" or not body.get("url"):
        raise UploadError(f"上传网盘失败: {str(body)[:200]}")
    return str(body["url"])


def attach_files(session: requests.Session, ids: dict, data: dict,
                 uploaded: list[dict], *, timeout: float = 90.0) -> str:
    """把已上传的文件挂到记录上；返回 recordUuid。

    ⚠️ **`fileModeType=0`（单附件模式）的 `files` 是扁平数组**，不是卡片结构：

    ```json
    [{"fileObjectId": "…", "fileType": "jpeg", "fileName": "图片信息.jpeg"}]
    ```

    这一点是抓真实浏览器请求确认的（`POST answer/update-file` 的表单字段 `files`），
    并与平台存储回显完全一致（`load-data` 的 `fileObjectId`/`fileName`/`fileType`
    被正确填充）。此前按多卡片模式发 `[{"fileCardId":…,"files":[…]}]`，服务端会
    返回 `{"data":{},"status":true}` **静默忽略**，结果交上去是一条没有附件的空记录。
    字段名是 `fileName`（**不是** `name`），`fileType` 是扩展名（如 `png`/`jpeg`）。
    """
    payload_files = [
        {"fileObjectId": item["fileObjectId"],
         "fileType": item["fileType"],
         "fileName": item["name"]}
        for item in uploaded
    ]
    files_json = json.dumps(payload_files, ensure_ascii=False)
    payload = {"type": MODE_UPLOAD, "files": files_json}
    params = {"courseid": ids["courseid"], "clazzid": ids["clazzid"],
              "cpi": ids["cpi"], "publishRelationUuid": ids["publishRelationUuid"]}

    existing = (data.get("recordUuid") or "").strip()
    if existing:
        resp = session.post(f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/update-file",
                            params={**params, "recordUuid": existing},
                            data=payload, timeout=timeout)
        if resp.status_code != 200 or not _ok(resp):
            raise UploadError(f"update-file 失败: {resp.text[:200]}")
        logger.info("已把 {} 个文件挂到已有记录 {}", len(uploaded), existing)
        return existing

    init = session.post(f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/init",
                        params=params, data=payload, timeout=timeout)
    if init.status_code != 200:
        raise UploadError(f"answer/init 返回 HTTP {init.status_code}")
    body = init.json()
    record_uuid = ((body.get("data") or {}).get("recordUuid") or "").strip()
    if not record_uuid:
        msg = str(body.get("msg") or "")
        if "已存在" in msg:
            raise UploadAbort(f"服务端报告已存在未完成记录但未返回 recordUuid：{msg}")
        raise UploadError(f"answer/init 未返回 recordUuid: {str(body)[:200]}")
    logger.info("已建卷 {} 并挂上 {} 个文件", record_uuid, len(uploaded))
    return record_uuid


def _ok(resp: requests.Response) -> bool:
    try:
        return bool(resp.json().get("status"))
    except ValueError:
        return False


def wait_parse(session: requests.Session, ids: dict, record_uuid: str, *,
               uploaded: Optional[list[dict]] = None,
               tries: int = 8, delay: float = 3.0,
               on_log: Optional[Callable] = None) -> dict:
    """轮询 check-file-status 直到解析完成。

    真实页面调用这个接口时会带 ``fileObjectId`` + ``fileType``（见
    ``file-mode/answer.js`` 的 ``checkCardFileStatus``）；漏传会得到空响应
    ``{"data":{}}``，看起来像"没挂上"。
    """
    log = on_log or (lambda m: None)
    last: dict = {}
    params = {"courseid": ids["courseid"], "clazzid": ids["clazzid"],
              "cpi": ids["cpi"], "publishRelationUuid": ids["publishRelationUuid"],
              "recordUuid": record_uuid}
    first = (uploaded or [{}])[0]
    if first.get("fileObjectId"):
        params["fileObjectId"] = first["fileObjectId"]
        params["fileType"] = first.get("fileType") or ""
        params["fileName"] = first.get("name") or first.get("fileName") or ""
    for i in range(tries):
        try:
            resp = session.get(
                f"{MOOC2}/mooc2-ans/ai-evaluate/v2/answer/check-file-status",
                params=params, timeout=30)
            last = resp.json() if resp.status_code == 200 else {}
        except (requests.RequestException, ValueError) as exc:
            log(f"解析状态查询失败（继续重试）：{exc}")
            last = {}
        data = last.get("data") or {}
        status = str(data.get("fileParseStatus") or data.get("status") or "")
        if status in ("1", "2", "3"):
            log(f"文件解析状态：{status}")
            return last
        time.sleep(delay)
    log(f"文件解析未在预期时间内完成，继续按原流程提交（最后响应：{str(last)[:120]}）")
    return last


def save_pngs(pages: list[bytes], label: str) -> str:
    """把作品图片保存到磁盘，返回目录路径（供手动上传）。"""
    safe = re.sub(r"[\\/:*?\"<>|]+", "_", label).strip("_")[:60] or "作品"
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "作品", safe)
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    for idx, blob in enumerate(pages, 1):
        with open(os.path.join(out_dir, f"{idx:02d}.png"), "wb") as fp:
            fp.write(blob)
    return out_dir


def complete_upload_task(session: requests.Session, course: dict, study_url: dict,
                         generator: Any, *, options: Optional[dict] = None,
                         on_log: Optional[Callable] = None,
                         approval: Optional[Callable[[str, Callable], None]] = None,
                         submit_enabled: bool = True) -> tuple[bool, str]:
    """完成一个「上传作品」任务点。

    ⚠️ ``submit_enabled`` 目前**必须保持 False**：``fileModeType=0`` 的挂附件载荷
    尚未解析正确（实测卡片式/扁平式都被服务端静默忽略，交上去会是一条**没有附件
    的空记录**，评分服务随即返回 500）。因此默认只生成作品并**保存到磁盘**，
    由用户自行在浏览器上传。

    ``approval`` 不为 None 时把 ``(预览文本, 提交回调)`` 交给审批流程。
    """
    opts = options or {}
    log = on_log or (lambda m: logger.info(m))
    ids = ld._ids(study_url, course)
    if not ids.get("publishRelationUuid"):
        raise UploadAbort("未能从任务点解析出 publishRelationUuid")

    data = ld.load_data(session, ids)
    if data.get("type") != MODE_UPLOAD:
        raise UploadAbort(f"该任务点不是上传作品（type={data.get('type')}）")

    requirement = ld.strip_html(data.get("requirement") or data.get("content") or "")
    rules = data.get("rules") or []
    categories = {r.get("category") for r in rules if isinstance(r, dict)}
    if "image" not in categories:
        raise UploadAbort(f"该任务不接受图片附件（仅 {sorted(c for c in categories if c)}），"
                          "本模块目前只生成图片作品")
    max_count = next((int(r.get("maxCount") or 9) for r in rules
                      if isinstance(r, dict) and r.get("category") == "image"), 9)
    log(f"题目要求 {len(requirement)} 字｜允许图片最多 {max_count} 张")

    if not getattr(generator, "available", False):
        raise UploadAbort("未配置 AI 接口（endpoint/key/model），无法生成作品")

    banned = copy_is_banned(requirement)
    if banned and not opts.get("upload_ai_text"):
        raise UploadAbort(
            "该题目原文要求「禁止直接大段复制 AI 输出，用自己的话整理」，"
            "而当前设置为「不提交 AI 原文」→ 已改为只生成草稿（不提交）。"
            "如需直接提交 AI 原文，请在界面勾选「上传作品时提交 AI 原文」")
    if banned:
        # 按用户要求放行，但在日志里如实记录，避免"静默违反题目要求"
        log("注意：该题目原文要求「用自己的话整理、禁止大段复制 AI 输出」，"
            "但按你的设置（提交 AI 原文）继续提交")

    # ---- 生成作品 ----
    if needs_dialogue(requirement):
        transcript, topic = build_dialogue(generator, requirement,
                                           turns=int(opts.get("dialogue_turns") or 5),
                                           on_log=log)
        body = render_dialogue(transcript, topic)
        title = f"{course.get('title') or ''}｜AI 对话实录"
    else:
        body = build_work_text(generator, requirement, on_log=log)
        title = f"{course.get('title') or ''}｜AI 实践作品"

    pages = render_pngs(title, body, max_pages=max_count)
    if not pages:
        raise UploadAbort("未能渲染出任何图片，已中止")
    log(f"已渲染 {len(pages)} 张作品图片")

    # 提前取一次性上传凭据，便于回调里直接上传
    entry = session.get(ids["url"], timeout=30)
    entry.encoding = "utf-8"
    fields = ld.parse_hidden_inputs(entry.text)
    if not (fields.get("currentUploadEnc") and fields.get("currentUserId")):
        raise UploadAbort("入口页缺少上传凭据（currentUploadEnc/currentUserId），"
                          "无法上传附件")

    preview = (f"{title}\n（共 {len(pages)} 张图片）\n\n"
               + "\n".join(body.splitlines()[:40]))

    # ---- 兜底：显式关掉自动上传时，只保存到磁盘，绝不写账号 ----
    if not submit_enabled:
        out_dir = save_pngs(pages, f"{course.get('title') or ''}_{data.get('title') or '作品'}")
        log(f"作品已保存到 {out_dir}")
        msg = (f"已生成作品 {len(pages)} 张图并保存到：{out_dir}\n"
               "⚠️ 自动上传附件尚未验证通过（本模式挂附件载荷未解析正确，"
               "贸然提交会得到一条没有附件的空记录，评分服务会报 500）。"
               "请在浏览器里打开该任务点，手动上传这个目录里的图片。")
        if approval is not None:
            approval(preview + f"\n\n保存位置：{out_dir}", lambda: (True, msg))
            return True, f"已生成作品（{len(pages)} 张图片），待审批查看（不会自动上传）"
        return True, msg

    def _submit() -> tuple[bool, str]:
        uploaded = []
        for idx, blob in enumerate(pages, 1):
            name = f"{idx:02d}.png"
            object_id = upload_to_pan(session, fields, name, blob)
            uploaded.append({"fileType": "png", "fileObjectId": object_id, "name": name})
            log(f"已上传第 {idx}/{len(pages)} 张（objectId={object_id[:12]}…）")
        # 重新读一次，拿最新的 recordUuid（可能已有未完成记录）
        fresh = ld.load_data(session, ids)
        record_uuid = attach_files(session, ids, fresh, uploaded)
        # 回读校验：确认附件**真的**挂上了再交卷（否则会交一条空记录）
        after = ld.load_data(session, ids)
        if not (after.get("fileObjectId") or after.get("files")):
            return False, ("附件未能挂到记录上（服务端静默忽略），已中止交卷以免"
                           f"提交空记录。记录 {record_uuid} 仍为未提交状态")
        log(f"附件已确认挂上：fileObjectId={str(after.get('fileObjectId'))[:16]}…")
        wait_parse(session, ids, record_uuid, uploaded=uploaded, on_log=log)
        try:
            status = ld.answer_submit(session, ids, record_uuid)
            log(f"已交卷：recordStatus={status}")
        except ld.LadderError as exc:
            return False, f"交卷失败：{exc}"
        # ⚠️ 交卷**已经成功**了 —— 之后的评分/取分环节无论出什么问题，都绝不能把
        # 结果变成"失败"：否则用户会以为没交上而重复提交（实测就是这么误报的）。
        score = None
        try:
            stream = ld.evaluate_stream(session, ids, record_uuid,
                                        fresh.get("aiEnc") or data.get("aiEnc") or "",
                                        max_seconds=float(opts.get("eval_seconds") or 300))
            log(f"评分流结束：status={(stream or {}).get('status') if isinstance(stream, dict) else stream}")
        except Exception as exc:  # noqa: BLE001 - 评分失败不影响已完成的交卷
            log(f"评分流异常（交卷已完成，不影响提交结果）：{exc}")
        try:
            result = ld.wait_result(session, ids, record_uuid, tries=15, delay=3.0)
            fresh2 = ld.load_data(session, ids)
            score = ld.extract_score(result if isinstance(result, dict) else {},
                                     fresh2 if isinstance(fresh2, dict) else {})
        except Exception as exc:  # noqa: BLE001 - 取分失败同样不影响交卷
            log(f"取分环节异常（交卷已完成，不影响提交结果）：{type(exc).__name__}: {exc}")
        return True, (f"已上传 {len(uploaded)} 张作品并交卷"
                      + (f"，得分 {score}" if score is not None else "，评分稍后刷新"))

    if approval is not None:
        approval(preview, _submit)
        return True, f"已生成作品（{len(pages)} 张图片），待审批后上传提交"

    return _submit()
