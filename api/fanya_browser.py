# -*- coding: utf-8 -*-
"""浏览器通道：补齐纯 HTTP **做不到**的累计观看/阅读时长。

为什么需要这个模块（全部是受控实测结论，不是推测）
--------------------------------------------------
任务点的完成判据里有一类 **累计时长门槛**：
``planBreakthroughSet.enableDocumentWatchDuration / enableVideoWatchDuration``
对应阈值 ``documentWatchDuration / videoWatchDuration``（**分钟**），
判据是服务端 ``score.totalTime``（**毫秒**）≥ 阈值 × 60000。

``totalTime`` 只按**真实经过的时间**累计，且：

* **视频**：``videoDataLog/dataLog`` 心跳按真实墙上时钟累计（一次性跳到片尾、
  几秒内连发心跳、10 倍速节奏都计 0；3 倍速约被采信 0.63 倍）。
  → 这部分 ``api/fanya_task._satisfy_watch_duration`` 已能纯 HTTP 完成。
* **文档**：``documentStudy/readEnd`` **只**把 ``completeWeightRate`` 置 100，
  **一分一秒都不计入 ``totalTime``**。实测：打开 learnPage → 真等 40 秒 →
  调 readEnd，``totalTime`` 仍是 0；连调 3 次仍是 0。
  → 纯 HTTP **不可能**补足文档时长。

文档时长真正的通道在**云盘阅读器 iframe**（``pan-yz.chaoxing.com``）里：
学生真实滚动、阅读器判到底（``scrollBottom <= 40``）后
``parent.postMessage({isFinished:1})`` → 外层调 ``readEnd``，
同时阅读器按停留时间打 ``data-xxt.aichaoxing.com/analysis/ac_mark`` 埋点。

★ **埋点不可重放**（实测）：把抓到的 ``ac_mark`` URL 原样重发（带正确 ``enc``
签名）、篡改 ``enc`` 重发、伪造 ``h``（停留毫秒）重发 —— 三种都是 ``totalTime``
Δ=0。它是纯统计埋点，不做计分。

★ **只有真实浏览器阅读才计量**（实测，planId=8360000）：
用 Playwright 驱动真实 Edge、注入 session cookie、打开 learnPage、
循环把阅读器 iframe 滚到底 →
``totalTime`` 0 → 255000 → 285000 → 290000 → **300000**，``isFinish``
由 False 翻为 **True**（阈值 5.0 分钟 = 300000 ms）。

因此本模块是「文档类时长门槛」的唯一可行手段，也是「浏览器通道」的起点。

依赖与沙箱
----------
``playwright`` 是**可选依赖**：没装或没有可用浏览器时，本模块抛
``BrowserUnavailable``，调用方应据此**如实回报失败**，不要假装完成。

★ Windows 沙箱下 ``%TEMP%`` 可能不可写，Playwright 会报 ``mkdtemp`` 失败。
  本模块在导入 Playwright **之前**把 ``TEMP/TMP/TMPDIR`` 指向工作区内的
  ``_pw_tmp``（可用 ``FANYA_BROWSER_TMP`` 覆盖）。

★ requests 的 cookie jar 里 ``domain`` 常是**空串**；Playwright 会把它当
  host-only，``passport2.chaoxing.com`` 收不到就被踢回登录页。必须显式写成
  ``.chaoxing.com``（与 CDP ``Network.setCookie`` 的历史经验一致）。
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable, Optional

from loguru import logger

from . import paths

__all__ = [
    "BrowserUnavailable",
    "playwright_available",
    "document_learn_url",
    "read_document_in_browser",
]

#: 文档学习页（外层壳，真正的阅读器在其中以 iframe 加载）
DOC_LEARN_PAGE = "https://task.chaoxing.com/documentStudy/learnPage"

#: 阅读器 iframe 的域名特征
_READER_HOST = "pan-yz.chaoxing.com"

#: 跳过这些静态资源，避免请求日志噪音
_SKIP_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".woff",
             ".woff2", ".ttf", ".svg", ".ico", ".map")


class BrowserUnavailable(RuntimeError):
    """Playwright 或可用浏览器缺失 —— 调用方应如实回报，不要假装完成。"""


def _prepare_temp() -> str:
    """把 TEMP/TMP/TMPDIR 指向工作区内可写目录。

    ★ 必须在导入 ``playwright`` 之前调用：Playwright 启动浏览器时会在
      ``%TEMP%`` 下 ``mkdtemp``；沙箱下该目录不可写会直接失败。
      可用环境变量 ``FANYA_BROWSER_TMP`` 覆盖。
    """
    tmp = os.environ.get("FANYA_BROWSER_TMP") or os.path.join(paths.app_dir(), "_pw_tmp")
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError:
        tmp = os.path.join(os.getcwd(), "_pw_tmp")
        os.makedirs(tmp, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = tmp
    return tmp


def playwright_available() -> bool:
    """是否**能**起浏览器（装了 playwright 且至少一个 channel 可用）。"""
    _prepare_temp()
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except ImportError:
        return False
    return True


def document_learn_url(encry_plan_user_id: str) -> str:
    """由 ``encryPlanUserId`` 拼出文档学习页 URL。"""
    return f"{DOC_LEARN_PAGE}?encryPlanUserId={encry_plan_user_id}"


def _cookies_for_playwright(session: Any) -> list:
    """把 requests 的 cookie 转成 Playwright 格式。

    ★ domain 强制为 ``.chaoxing.com``：requests 里常是空串，Playwright 会当
      host-only 处理，导致 passport2 认证不上、被踢回登录页。
    """
    out = []
    for c in session.cookies:
        dom = (c.domain or "").lstrip(".")
        if dom and "chaoxing" not in dom:
            continue
        out.append({
            "name": c.name,
            "value": c.value,
            "domain": ".chaoxing.com",
            "path": c.path or "/",
            "secure": False,
            "httpOnly": False,
            "sameSite": "Lax",
        })
    return out


def read_document_in_browser(
    session: Any,
    learn_page_url: str,
    required_ms: int,
    read_state: Callable[[], tuple],
    *,
    budget_seconds: float = 1800.0,
    poll_seconds: float = 0.0,
    scroll_steps: int = 12,
    scroll_step_ms: int = 900,
    scroll_px: int = 900,
    headless: bool = True,
    should_stop: Optional[Callable[[], bool]] = None,
    stall_rounds_limit: int = 2,
    max_reopens: int = 12,
    on_log: Optional[Callable[[str], None]] = None,
) -> tuple[bool, str]:
    """在真实浏览器里滚动阅读文档，直到累计阅读时长达标或超出预算。

    参数
    ----
    session
        已登录的 ``requests.Session``（cookie 会注入浏览器）。
    learn_page_url
        文档学习页（``document_learn_url(epu)``）。
    required_ms
        要求的累计毫秒数（``watch_duration_ms(extra, "document")``）。
    read_state
        无参回调，返回 ``(isFinish, total_time_ms, complete_weight_rate)``；
        由调用方回读服务端，避免本模块依赖 ``FanyaTaskClient``。
    budget_seconds
        真实等待预算，超出即如实返回失败。
    should_stop
        可选中断回调（返回 True 立即收手）。界面上的「停止」要能在文档
        阅读中途生效，否则一个 30 分钟的文档会把停止请求一起卡住。
    scroll_steps / scroll_step_ms / scroll_px
        每一轮把「向下滚动」铺开成 ``scroll_steps`` 次、每次间隔
        ``scroll_step_ms`` 毫秒、每次 ``scroll_px`` 像素。铺开是必要的：
        实测滚动到新内容才计入时长，一次跳到底再干等则一分不记。
    poll_seconds
        每轮回读服务端前的额外等待（默认 0：滚动本身就占满了一轮）。
    stall_rounds_limit
        连续多少轮回读「时长没有增加」就认为阅读器已经滚到底（默认 2 轮）。
    max_reopens
        最多重开阅读页多少次（默认 12 次）。
    on_log
        日志回调（默认 ``logger.info``）。

    为什么需要「重开阅读页」：服务端只按真实阅读行为累计时长，而阅读器
    滚到底之后就不再计入 —— 此时原地空转再久也不会涨（实测 160 秒 Δ0）。
    重新打开阅读页会开始一个新的阅读会话、触发下一段结算（实测一次重开
    约 +15 秒），这是不伪造上报的前提下唯一还能推进的做法。

    返回
    ----
    ``(ok, message)``。**ok 只在本轮真的观测到 isFinish 翻转为 True 时为真**；
    仅 totalTime 达标而 isFinish 未翻转会返回失败并说明「平台尚未结算」，
    绝不假称成功。
    """
    log = on_log or (lambda m: logger.info(m))

    try:
        _prepare_temp()
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - 环境相关
        raise BrowserUnavailable(
            "未安装 playwright，无法用浏览器补齐文档阅读时长"
            "（pip install playwright 后需 `playwright install msedge`）"
        ) from exc

    def _state() -> tuple:
        try:
            st = read_state()
        except Exception as exc:  # noqa: BLE001
            logger.debug("回读任务点状态失败: {}", exc)
            return (False, 0, None)
        return st if st else (False, 0, None)

    before = _state()
    if before[0]:
        return True, "该任务点已经是完成状态"
    if required_ms > 0 and before[1] >= required_ms:
        log(f"累计阅读已达 {before[1]/1000:.0f} 秒 ≥ 要求 "
            f"{required_ms/1000:.0f} 秒，等待平台结算")
    else:
        log(f"需要累计阅读 {required_ms/1000/60:.1f} 分钟"
            f"（当前 {before[1]/1000:.0f} 秒）—— 纯 HTTP 的 readEnd 不计入，"
            f"改用真实浏览器滚动阅读")

    # 优先用系统 Edge；失败依次回退 chromium / chrome
    last_exc: Optional[Exception] = None
    launch_errors: list = []

    with sync_playwright() as pw:
        browser = None
        for kwargs in ({"channel": "msedge"}, {}, {"channel": "chrome"}):
            try:
                browser = pw.chromium.launch(
                    headless=headless,
                    args=["--no-first-run", "--no-default-browser-check"],
                    **kwargs,
                )
                break
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                launch_errors.append(f"{kwargs or 'bundled'} → {type(exc).__name__}")
        if browser is None:
            raise BrowserUnavailable(
                "无法启动任何浏览器：" + "；".join(launch_errors)
            ) from last_exc

        ctx = browser.new_context(viewport={"width": 1400, "height": 900})
        try:
            ctx.add_cookies(_cookies_for_playwright(session))
        except Exception as exc:  # noqa: BLE001
            log(f"（部分 cookie 被浏览器拒收：{type(exc).__name__}，继续尝试）")

        page = ctx.new_page()
        t0 = time.time()

        def _open_reader() -> tuple:
            """打开学习页并取出阅读器 iframe；返回 ``(reader, 错误信息)``。"""
            page.goto(learn_page_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(6000)

            title = ""
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                pass
            if "登录" in title:
                return None, ("浏览器里未带上登录态（被重定向到登录页）—— "
                              "请确认本机 cookie 仍然有效")

            for f in page.frames:
                if _READER_HOST in (f.url or ""):
                    return f, None
            return None, ("文档阅读器 iframe（pan-yz.chaoxing.com）未加载，"
                          "可能该文档已变更或需要验证码")

        try:
            reader, err = _open_reader()
            if err:
                browser.close()
                return False, err

            best = int(before[1] or 0)
            stall_rounds = 0
            reopens = 0
            while time.time() - t0 < budget_seconds:
                if should_stop is not None and should_stop():
                    browser.close()
                    return False, (f"已按停止请求中止文档阅读（服务端已记 "
                                   f"{best/1000:.0f} 秒，任务点未完成）")

                # 阅读器按「滚动到新内容」累计时长（实测：原地滚动 0×、
                # 向下滚 ≈1×、密集滚偶发 1.4×）—— 所以把滚动铺满整轮，而不是
                # 一次跳到底再干等：一次到底之后，剩下的等待时间一分都不记。
                for _ in range(max(1, scroll_steps)):
                    try:
                        reader.evaluate(f"() => window.scrollBy(0, {scroll_px})")
                    except Exception:  # noqa: BLE001
                        pass
                    page.wait_for_timeout(scroll_step_ms)
                try:
                    page.mouse.wheel(0, 2000)
                except Exception:  # noqa: BLE001
                    pass
                if poll_seconds > 0:
                    page.wait_for_timeout(int(poll_seconds * 1000))

                st = _state()
                elapsed = time.time() - t0
                log(f"累计阅读 {st[1]/1000:.0f} 秒 / 要求 {required_ms/1000:.0f} 秒"
                    f"（已等 {elapsed:.0f} 秒，isFinish={st[0]}）")
                if st[0]:
                    browser.close()
                    return True, (f"已在浏览器中阅读完成（服务端记 "
                                  f"{st[1]/1000:.0f} 秒 ≥ {required_ms/1000:.0f} 秒）")

                if int(st[1] or 0) > best:
                    best = int(st[1] or 0)
                    stall_rounds = 0
                    continue

                stall_rounds += 1
                if stall_rounds < stall_rounds_limit:
                    continue
                if reopens >= max_reopens:
                    log(f"已重开阅读页 {reopens} 次仍无新增（停在 "
                        f"{best/1000:.0f} 秒），不再空转")
                    break
                reopens += 1
                log(f"已连续 {stall_rounds} 轮回读没有新增（{best/1000:.0f} 秒）"
                    f"—— 阅读器已滚到底，重开阅读页触发下一段结算"
                    f"（第 {reopens}/{max_reopens} 次）")
                reader, err = _open_reader()
                if err:
                    browser.close()
                    return False, err
                # 新一轮从顶部重新读一遍，别停在底部空转
                try:
                    reader.evaluate("() => window.scrollTo(0, 0)")
                except Exception:  # noqa: BLE001
                    pass
                stall_rounds = 0

            after = _state()
            browser.close()
            if required_ms > 0 and after[1] >= required_ms and not after[0]:
                return False, (f"累计阅读已记 {after[1]/1000:.0f} 秒（≥ "
                               f"{required_ms/1000:.0f} 秒）但服务端尚未把任务点"
                               f"标为完成；稍后重试或等待平台结算")
            reopened = f"，重开阅读页 {reopens} 次" if reopens else ""
            return False, (f"超出单任务点时间上限（{budget_seconds:.0f} 秒"
                           f"{reopened}），已记 {after[1]/1000:.0f} 秒，未达 "
                           f"{required_ms/1000:.0f} 秒")
        finally:
            try:
                if browser.is_connected():
                    browser.close()
            except Exception:  # noqa: BLE001
                pass
