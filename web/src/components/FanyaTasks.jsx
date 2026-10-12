import React, { useState, useEffect, useRef, useCallback } from 'react';
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from './ui/Card';
import Button from './ui/Button';
import {
  ListChecks, Loader2, Play, Home, RefreshCw, CheckCircle2, XCircle,
  AlertCircle, Square, ChevronDown, ChevronRight, Sparkles, FileText,
  Video, MessagesSquare, Bot, ClipboardList, HelpCircle, Info,
} from 'lucide-react';
import api from '../api/axios';

// 任务点类型 → 图标
const TYPE_ICONS = {
  视频: Video,
  文档: FileText,
  主题讨论: MessagesSquare,
  AI实践: Bot,
  作业: ClipboardList,
  测验: HelpCircle,
  自测: HelpCircle,
  章节: FileText,
  知识点: Sparkles,
  线下课堂: Info,
  AI课堂: Sparkles,
  分组任务: ClipboardList,
  问卷: ClipboardList,
};

// 支持状态 → 展示样式
const STATUS_STYLES = {
  auto: { label: '可自动完成', className: 'bg-green-100 text-green-700 border-green-200' },
  partial: { label: '需先备材料', className: 'bg-amber-100 text-amber-700 border-amber-200' },
  manual: { label: '需人工完成', className: 'bg-gray-100 text-gray-600 border-gray-200' },
  pending: { label: '暂未支持', className: 'bg-gray-100 text-gray-600 border-gray-200' },
};

const LOG_LEVEL_COLORS = {
  error: 'text-red-600',
  warning: 'text-amber-600',
  success: 'text-green-600',
  info: 'text-gray-600',
};

// 上次盘点结果缓存：回到本页直接展示，不必每次重新盘点
const PREVIEW_KEY = 'chaoxing.fanyaPreview';

// 多数任务返回 0~1 的比例，但个别任务返回的是 8.33 这样的百分数，
// 一律按比例渲染会显示成「833%」。超出 [0,1] 时直接显示原始值。
const formatProgress = (value) => {
  const num = Number(value);
  if (!Number.isFinite(num)) return String(value ?? '-');
  if (num >= 0 && num <= 1) return `${(num * 100).toFixed(1)}%`;
  return `${num}（原始值）`;
};

const TypeIcon = ({ label }) => {
  const Icon = TYPE_ICONS[label] || ListChecks;
  return <Icon className="w-4 h-4" />;
};

const FanyaTasks = ({ userInfo, selectedCourseIds = [], allCourses = [],
                      onBack, onLogout }) => {
  const [phase, setPhase] = useState('scan'); // scan | preview | running | done
  const [scanTaskId, setScanTaskId] = useState(null);
  const [runTaskId, setRunTaskId] = useState(null);
  const [preview, setPreview] = useState(null);
  const [scanState, setScanState] = useState(null);
  const [runState, setRunState] = useState(null);
  const [results, setResults] = useState([]);
  const [logs, setLogs] = useState([]);
  const [selected, setSelected] = useState(new Set());
  const [expanded, setExpanded] = useState(new Set());
  const [error, setError] = useState('');
  const [tikuConfig, setTikuConfig] = useState({});
  const [stopping, setStopping] = useState(false);
  // 考试默认只暂存不交卷；必须显式打开才会提交（避免误交卷）
  const [examSubmit, setExamSubmit] = useState(false);
  // 分组任务默认只生成内容不提交；且默认拒绝覆盖小组已有内容
  const [groupSubmit, setGroupSubmit] = useState(false);
  const [groupForceOverwrite, setGroupForceOverwrite] = useState(false);
  // AI实践（代码实践/思维阶梯/情景对话）默认走审批；打开此项才允许直接提交
  const [allowAiSubmit, setAllowAiSubmit] = useState(false);
  // 已有未完成作答记录时改为「重做」（先结束旧记录再建新卷）
  const [aiRedo, setAiRedo] = useState(false);
  // 待审批队列：处理器只生成内容，用户逐条批准后才真正提交
  const [approvals, setApprovals] = useState([]);
  const [deciding, setDeciding] = useState('');
  // 默认只盘点课程管理页勾选的课程；勾选「盘点全部课程」才扫全库
  const [scanAll, setScanAll] = useState(false);
  // 重做模式：允许选中**已完成**的任务点再答一次
  const [redo, setRedo] = useState(false);
  // 上传作品：题目若声明「禁止复制 AI 输出」时，默认只出草稿；
  // 打开此项才允许提交 AI 原文（等于明知违反题目要求仍提交）
  const [uploadAiText, setUploadAiText] = useState(true);
  const [previewAt, setPreviewAt] = useState(null);
  const logsEndRef = useRef(null);
  const timerRef = useRef(null);

  // 盘点范围：勾选的课程（未勾选时后端会回退为全部）
  const scopeIds = scanAll ? [] : (selectedCourseIds || []);
  const scopeTitle = scanAll
    ? '全部课程'
    : (selectedCourseIds?.length
      ? (allCourses || [])
        .filter((c) => selectedCourseIds.includes(c.courseId))
        .map((c) => c.title)
        .join('、') || `${selectedCourseIds.length} 门已勾选课程`
      : '未勾选任何课程（将回退为全部课程）');

  useEffect(() => {
    // 载入已保存的 AI 接口配置（用于生成讨论回复等文本作答）
    api.get('/config').then((res) => {
      if (res.data.status && res.data.data?.settings?.tiku_config) {
        setTikuConfig(res.data.data.settings.tiku_config);
      }
    }).catch(() => {});

    // 恢复上次盘点结果：不用每次进来都重新盘点
    try {
      const raw = window.localStorage.getItem(PREVIEW_KEY);
      if (raw) {
        const cached = JSON.parse(raw);
        if (cached?.preview?.courses) {
          setPreview(cached.preview);
          setPreviewAt(cached.at || null);
          const auto = new Set();
          const expand = new Set();
          cached.preview.courses.forEach((c) => {
            if (!c.hasTaskEngine) return;
            expand.add(c.courseId);
            c.tasks.forEach((t) => t.items.forEach((i) => {
              if (!i.isFinish && i.status === 'auto') auto.add(i.planId);
            }));
          });
          setSelected(auto);
          setExpanded(expand);
          setPhase('preview');
        }
      }
    } catch (err) {
      /* 缓存坏了就当没有 */
    }
  }, []);

  useEffect(() => {
    logsEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logs]);

  useEffect(() => () => { if (timerRef.current) clearInterval(timerRef.current); }, []);

  // 待审批队列：独立轮询，跑完盘点/执行后都能看到新产生的待审项
  const fetchApprovals = useCallback(async () => {
    try {
      const res = await api.get('/fanya/approvals');
      if (res.data.status) setApprovals(res.data.data.items || []);
    } catch (err) {
      /* 忽略单次轮询失败 */
    }
  }, []);

  useEffect(() => {
    fetchApprovals();
    const t = setInterval(fetchApprovals, 4000);
    return () => clearInterval(t);
  }, [fetchApprovals]);

  const decideApproval = async (id, approve) => {
    if (!approve && !window.confirm('拒绝这一项？不会提交任何内容。')) return;
    if (approve && !window.confirm('批准并提交这一项？提交后无法撤销。')) return;
    setDeciding(id);
    try {
      // 审批会真的执行提交链路（上传附件/交卷/触发评分），可能耗时数分钟；
      // 默认 30s 超时会让界面误报「超时」，其实后端仍在正常执行。
      const res = await api.post('/fanya/approve', { id, approve },
        { timeout: 600000 });
      if (!res.data.status) {
        alert(`处理失败：${res.data.msg || ''}`);
      } else if (approve) {
        alert(`已提交：${res.data.msg || '成功'}`);
      }
    } catch (err) {
      alert('审批请求失败：' + (err.response?.data?.msg || err.message));
    } finally {
      setDeciding('');
      fetchApprovals();
    }
  };

  const fetchLogs = useCallback(async (taskId) => {
    try {
      const res = await api.get(`/logs/${taskId}`);
      if (res.data.status && res.data.data.length > 0) {
        setLogs((prev) => [...prev, ...res.data.data]);
      }
    } catch (err) {
      /* 忽略单次轮询失败 */
    }
  }, []);

  // ---------------------------------------------------------------- 阶段一：盘点
  const startScan = async () => {
    setError('');
    setLogs([]);
    setPreview(null);
    try {
      const res = await api.post('/fanya/preview', {
        username: userInfo.username,
        password: userInfo.password,
        use_cookies: true,
        // 只盘点勾选的课程；scanAll 时才传空数组（后端空数组 = 全部课程）
        course_list: scopeIds,
        tiku_config: tikuConfig,
        // 重做模式：让后端也识别已完成的任务点（AI实践子模式等）
        options: { redo },
      });
      if (!res.data.status) {
        setError(res.data.msg || '启动盘点失败');
        return;
      }
      const id = res.data.data.task_id;
      setScanTaskId(id);
      setPhase('scan');

      let scanFails = 0;
      const scanStarted = Date.now();
      timerRef.current = setInterval(async () => {
        // 兜底：无论「请求一直失败」还是「请求成功但后端永不返回结果」，
        // 都不能无限转圈 —— 超过 90 秒复位并明确报错。
        if (Date.now() - scanStarted > 90000) {
          clearInterval(timerRef.current);
          setScanTaskId(null);
          setPhase('scan');
          setError('盘点超时（90 秒未返回结果），请重新点击「开始盘点任务」');
          return;
        }
        fetchLogs(id);
        try {
          const r = await api.get(`/fanya/preview/${id}`);
          scanFails = 0;
          if (r.data.status) {
            setScanState(r.data.data.task);
            if (r.data.data.preview) {
              const got = r.data.data.preview;
              setPreview(got);
              // 默认勾选所有「可自动完成」的未完成任务点
              const auto = new Set();
              const expand = new Set();
              got.courses.forEach((c) => {
                if (!c.hasTaskEngine) return;
                expand.add(c.courseId);
                c.tasks.forEach((t) => t.items.forEach((i) => {
                  if (!i.isFinish && i.status === 'auto') auto.add(i.planId);
                }));
              });
              setSelected(auto);
              setExpanded(expand);
              setPhase('preview');
              // 盘点已出结果，复位 id：之后任何清 preview 的操作都不会再
              // 触发「假盘点中」（按钮文案/禁用条件依赖 scanTaskId）
              setScanTaskId(null);
              // 缓存下来：下次进这个页面直接显示，不必重新盘点
              const at = Date.now();
              setPreviewAt(at);
              try {
                window.localStorage.setItem(PREVIEW_KEY,
                  JSON.stringify({ at, preview: got }));
              } catch (err) {
                /* 存不下就算了，不影响功能 */
              }
            }
            if (['completed', 'error'].includes(r.data.data.task.status)) {
              clearInterval(timerRef.current);
              if (r.data.data.task.status === 'error') {
                setError(r.data.data.task.error || '盘点失败');
              }
            }
          }
        } catch (err) {
          // 后端可能重启过 → 该 task_id 已失效。连续失败就明确报错并复位，
          // 否则界面会永远停在「盘点中...」。
          scanFails += 1;
          if (scanFails >= 4) {
            clearInterval(timerRef.current);
            setScanTaskId(null);
            setPhase('scan');
            setError('盘点任务已失效（后端可能重启过），请重新点击「开始盘点任务」');
          }
        }
      }, 1500);
    } catch (err) {
      setError('启动盘点失败，请检查后端服务');
    }
  };

  // ---------------------------------------------------------------- 阶段二：执行
  const startExecute = async () => {
    if (selected.size === 0) {
      alert('请至少勾选一个任务点');
      return;
    }
    const ok = window.confirm(
      `将对勾选的 ${selected.size} 个任务点执行自动完成。\n\n` +
      '【审批模式已开启】作业 / 主题讨论 / 代码实践 等会**先只生成内容**，' +
      '在下方「待审批」列表里给你逐条核对，你点「批准并提交」后才真正写入超星。' +
      '\n视频、文档、章节这类学习进度上报不属于内容提交，会直接完成。' +
      (examSubmit
        ? '\n\n⚠️ 已开启「考试交卷」：自测/测验会自动交卷（不走审批）。'
        : '\n\n自测/测验只会暂存作答，不会交卷。') +
      (groupSubmit
        ? (groupForceOverwrite
          ? '\n\n🚨 已开启「分组任务提交 + 强制覆盖」：会覆盖小组已有答案，抹掉组员已写好的内容！'
          : '\n\n⚠️ 已开启「分组任务提交」：小组已有内容时仍会被拒绝覆盖。')
        : '\n\n分组任务只会生成答案，不会提交。') +
      (allowAiSubmit
        ? '\n\n⚠️ 已开启「允许 AI实践直接提交」：思维阶梯/情景对话会直接交卷，不经审批。'
        : '\n\nAI实践：代码实践走审批；思维阶梯/情景对话会拒绝直接交卷。')
    );
    if (!ok) return;

    setError('');
    setLogs([]);
    setResults([]);
    const courseIds = new Set();
    preview?.courses.forEach((c) => {
      c.tasks.forEach((t) => t.items.forEach((i) => {
        if (selected.has(i.planId)) courseIds.add(c.courseId);
      }));
    });

    try {
      const res = await api.post('/fanya/execute', {
        username: userInfo.username,
        password: userInfo.password,
        use_cookies: true,
        course_list: [...courseIds],
        plan_ids: [...selected],
        tiku_config: tikuConfig,
        options: {
          exam_submit: examSubmit,
          group_submit: groupSubmit,
          group_force_overwrite: groupForceOverwrite,
          allow_ai_submit: allowAiSubmit,
          ai_redo: aiRedo,
          upload_ai_text: uploadAiText,
          redo,
          // 审批提交：默认开启 —— 只生成内容，逐条批准后才真正提交
          require_approval: true,
        },
      });
      if (!res.data.status) {
        setError(res.data.msg || '启动执行失败');
        return;
      }
      const id = res.data.data.task_id;
      setRunTaskId(id);
      setPhase('running');

      let runFails = 0;
      const runStarted = Date.now();
      timerRef.current = setInterval(async () => {
        if (Date.now() - runStarted > 1800000) {
          clearInterval(timerRef.current);
          setRunTaskId(null);
          setPhase('done');
          setError('执行超过 30 分钟仍未结束，已停止等待。'
            + '任务可能仍在后台运行，请查看实时日志或重新盘点。');
          return;
        }
        fetchLogs(id);
        try {
          const r = await api.get(`/fanya/result/${id}`);
          if (r.data.status) {
            setRunState(r.data.data.task);
            if (r.data.data.result?.results) {
              setResults(r.data.data.result.results);
            }
            if (['completed', 'error', 'stopped'].includes(r.data.data.task.status)) {
              clearInterval(timerRef.current);
              setPhase('done');
              if (r.data.data.task.status === 'error') {
                setError(r.data.data.task.error || '执行失败');
              }
            }
          }
        } catch (err) {
          runFails += 1;
          if (runFails >= 6) {
            clearInterval(timerRef.current);
            setRunTaskId(null);
            setPhase('done');
            setError('执行任务已失效（后端可能重启过），请重新盘点后再执行');
          }
        }
      }, 2000);
    } catch (err) {
      setError('启动执行失败，请检查后端服务');
    }
  };

  const handleStop = async () => {
    if (!runTaskId) return;
    if (!window.confirm('确定要停止吗？当前任务点会收尾后退出。')) return;
    setStopping(true);
    try {
      await api.post(`/task/${runTaskId}/stop`);
    } catch (err) {
      alert('停止请求失败');
    } finally {
      setStopping(false);
    }
  };

  const toggleSelect = (planId) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(planId)) next.delete(planId); else next.add(planId);
      return next;
    });
  };

  const toggleExpand = (courseId) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(courseId)) next.delete(courseId); else next.add(courseId);
      return next;
    });
  };

  const selectAllCompletable = () => {
    const next = new Set();
    preview?.courses.forEach((c) => c.tasks.forEach((t) => t.items.forEach((i) => {
      if (!i.isFinish && ['auto', 'partial'].includes(i.status)) next.add(i.planId);
    })));
    setSelected(next);
  };

  // ---------------------------------------------------------------- 渲染片段
  // 待审批队列：处理器只生成内容，这里逐条批准/拒绝
  const renderApprovals = () => {
    if (approvals.length === 0) return null;
    const pendingCount = approvals.filter((a) => a.status === 'pending').length;
    return (
      <Card className="mt-6 border-amber-300">
        <CardHeader className="pb-3">
          <CardTitle className="text-base flex items-center">
            <ClipboardList className="mr-2 h-4 w-4 text-amber-600" />
            待审批（{pendingCount}）
          </CardTitle>
          <CardDescription>
            以下内容**尚未提交到超星**。请逐条核对后点「批准并提交」，或点「拒绝」丢弃。
            进程重启后未处理的条目会失效。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {approvals.map((item) => (
            <div key={item.id} className="rounded-lg border p-3">
              <div className="flex items-start justify-between gap-3">
                <div className="flex-1 min-w-0">
                  <div className="text-xs text-muted-foreground">
                    {item.kind}
                    {item.status !== 'pending' && (
                      <span className={`ml-2 px-2 py-0.5 rounded-full border ${
                        item.status === 'approved'
                          ? 'bg-green-100 text-green-700 border-green-200'
                          : item.status === 'rejected'
                            ? 'bg-gray-100 text-gray-600 border-gray-200'
                            : 'bg-red-100 text-red-700 border-red-200'
                      }`}>
                        {item.status === 'approved' ? '已提交'
                          : item.status === 'rejected' ? '已拒绝' : '提交失败'}
                      </span>
                    )}
                  </div>
                  <div className="font-medium text-sm mt-0.5 break-words">{item.label}</div>
                  <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words
                                  rounded bg-gray-50 border p-2 text-xs text-gray-800">
                    {item.preview}
                  </pre>
                  {item.result && (
                    <div className="text-xs text-muted-foreground mt-1">
                      结果：{item.result}
                    </div>
                  )}
                </div>
                {item.status === 'pending' && (
                  <div className="flex flex-col gap-2 flex-shrink-0">
                    <Button size="sm" disabled={deciding === item.id}
                      onClick={() => decideApproval(item.id, true)}>
                      {deciding === item.id ? '提交中…' : '批准并提交'}
                    </Button>
                    <Button size="sm" variant="outline" disabled={deciding === item.id}
                      onClick={() => decideApproval(item.id, false)}>
                      拒绝
                    </Button>
                  </div>
                )}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>
    );
  };

  const renderLogs = () => (
    <Card className="mt-6">
      <CardHeader className="pb-3">
        <CardTitle className="text-base flex items-center">
          <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          实时日志
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="bg-gray-900 rounded-lg p-3 h-64 overflow-y-auto font-mono text-xs">
          {logs.length === 0 && <p className="text-gray-500">等待日志...</p>}
          {logs.map((log, idx) => (
            <div key={idx} className={LOG_LEVEL_COLORS[log.level] || 'text-gray-300'}>
              <span className="text-gray-500">
                {new Date(log.timestamp * 1000).toLocaleTimeString('zh-CN')}{' '}
              </span>
              {log.message}
            </div>
          ))}
          <div ref={logsEndRef} />
        </div>
      </CardContent>
    </Card>
  );

  const renderScan = () => (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center">
          <ListChecks className="mr-2 h-5 w-5" />
          课程「任务」模块自动完成
        </CardTitle>
        <CardDescription>
          盘点课程页左侧「任务」标签下的任务点（含 AI实践、主题讨论、作业、文档、视频、
          自测、测验等），并给出可自动化判定。此步骤只读取数据，不会修改任何学习记录。
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="rounded-lg border bg-gray-50 p-3 mb-3 text-sm">
          <div className="font-medium">盘点范围：{scanAll ? '全部课程' : '仅课程管理页勾选的课程'}</div>
          <div className="text-muted-foreground mt-1 break-words">{scopeTitle}</div>
          <label className="flex items-center space-x-2 mt-2 cursor-pointer">
            <input
              type="checkbox"
              checked={scanAll}
              onChange={(e) => setScanAll(e.target.checked)}
            />
            <span className="text-xs">
              盘点全部课程（默认只扫你勾选的，速度快很多）
            </span>
          </label>
        </div>
        {previewAt && (
          <div className="mb-3 text-xs text-muted-foreground">
            上次盘点：{new Date(previewAt).toLocaleString('zh-CN')}
            （已缓存，点「重新盘点」才会刷新）
          </div>
        )}
        {scanState && (
          <div className="mb-4 text-sm text-muted-foreground">
            正在读取：{scanState.current_course || '准备中...'}
          </div>
        )}
        {error && (
          <div className="mb-4 flex items-start space-x-2 text-sm text-red-600">
            <AlertCircle className="w-4 h-4 mt-0.5" />
            <span>{error}</span>
          </div>
        )}
      </CardContent>
      <CardFooter>
        <Button onClick={startScan} disabled={!!scanTaskId && !preview && !error}>
          {scanTaskId && !preview && !error
            ? <><Loader2 className="mr-2 h-4 w-4 animate-spin" />盘点中...</>
            : <><RefreshCw className="mr-2 h-4 w-4" />开始盘点任务</>}
        </Button>
      </CardFooter>
    </Card>
  );

  const renderPreview = () => {
    const s = preview.summary;
    // 折叠起来的高级开关也要能被看见 —— 危险的开关不能悄悄开着
    const advancedOn = [examSubmit, groupSubmit, groupForceOverwrite,
      allowAiSubmit, aiRedo, uploadAiText].filter(Boolean).length;
    const coursesWithTasks = preview.courses.filter((c) => c.hasTaskEngine);
    return (
      <>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              <span className="flex items-center">
                <CheckCircle2 className="mr-2 h-5 w-5 text-green-600" />
                任务盘点结果
              </span>
              <Button variant="outline" size="sm" onClick={startScan}>
                <RefreshCw className="mr-2 h-4 w-4" />重新盘点
              </Button>
            </CardTitle>
            <CardDescription>
              共 {s.courseCount} 门课程存在任务，{s.taskCount} 个任务，
              待完成 {s.pendingCount} 个任务点（其中可自动完成 {s.completableCount} 个）
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-4">
              {[
                { label: '待完成任务点', value: s.pendingCount, color: 'text-gray-900' },
                { label: '可自动完成', value: s.completableCount, color: 'text-green-600' },
                { label: '需人工完成', value: s.manualCount, color: 'text-amber-600' },
                { label: '暂未支持', value: s.unsupportedCount, color: 'text-gray-500' },
              ].map((item) => (
                <div key={item.label} className="rounded-lg border p-3">
                  <div className={`text-2xl font-semibold ${item.color}`}>{item.value}</div>
                  <div className="text-xs text-muted-foreground mt-1">{item.label}</div>
                </div>
              ))}
            </div>

            <div className="space-y-3">
              {coursesWithTasks.map((course) => {
                const pending = course.tasks.reduce((n, t) => n + t.pendingCount, 0);
                const isOpen = expanded.has(course.courseId);
                return (
                  <div key={course.courseId} className="rounded-lg border">
                    <button
                      className="w-full flex items-center justify-between p-3 text-left hover:bg-gray-50"
                      onClick={() => toggleExpand(course.courseId)}
                    >
                      <span className="flex items-center font-medium">
                        {isOpen ? <ChevronDown className="w-4 h-4 mr-1" />
                          : <ChevronRight className="w-4 h-4 mr-1" />}
                        {course.title}
                      </span>
                      <span className="text-xs text-muted-foreground">
                        待完成 {pending} 个
                      </span>
                    </button>
                    {isOpen && (
                      <div className="border-t px-3 pb-3">
                        {course.error && (
                          <p className="text-xs text-red-600 py-2">{course.error}</p>
                        )}
                        {course.tasks.map((task) => (
                          <div key={task.taskId} className="mt-3">
                            <div className="flex items-center justify-between text-sm">
                              <span className="font-medium">{task.name}</span>
                              <span className="text-xs text-muted-foreground">
                                进度 {formatProgress(task.progress)}
                                {!task.allowStudy && ' · 不在学习时间内'}
                              </span>
                            </div>
                            <div className="mt-2 space-y-1">
                              {task.items.filter((i) => redo || !i.isFinish).map((item) => {
                                const st = STATUS_STYLES[item.status] || STATUS_STYLES.pending;
                                const selectable = ['auto', 'partial'].includes(item.status);
                                return (
                                  <label
                                    key={item.planId}
                                    className={`flex items-start space-x-2 p-2 rounded border text-sm ${
                                      selectable ? 'cursor-pointer hover:bg-gray-50' : 'opacity-70'
                                    }`}
                                  >
                                    <input
                                      type="checkbox"
                                      className="mt-1"
                                      disabled={!selectable}
                                      checked={selected.has(item.planId)}
                                      onChange={() => toggleSelect(item.planId)}
                                    />
                                    <span className="flex-1">
                                      <span className="flex items-center space-x-2">
                                        <TypeIcon label={item.typeLabel} />
                                        <span>{item.name}</span>
                                      </span>
                                      <span className="mt-1 flex flex-wrap items-center gap-2">
                                        <span className="text-xs text-muted-foreground">
                                          {item.typeLabel}
                                        </span>
                                        <span className={`text-xs px-2 py-0.5 rounded-full border ${st.className}`}>
                                          {st.label}
                                        </span>
                                        {item.isFinish && (
                                          <span className="text-xs px-2 py-0.5 rounded-full border bg-emerald-50 text-emerald-700 border-emerald-200">
                                            已完成{item.score ? ' · 可重做' : ''}
                                          </span>
                                        )}
                                        {item.note && (
                                          <span className="text-xs text-muted-foreground">
                                            {item.note}
                                          </span>
                                        )}
                                        {item.entryUrl && !selectable && (
                                          <a
                                            href={item.entryUrl}
                                            target="_blank"
                                            rel="noreferrer"
                                            className="text-xs text-blue-600 hover:underline"
                                            onClick={(e) => e.stopPropagation()}
                                          >
                                            去手动完成 →
                                          </a>
                                        )}
                                      </span>
                                    </span>
                                  </label>
                                );
                              })}
                              {task.pendingCount === 0 && (
                                <p className="text-xs text-green-600 py-1">该任务已全部完成</p>
                              )}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </CardContent>
          <CardFooter className="flex flex-wrap gap-2">
            <Button variant="outline" size="sm" onClick={selectAllCompletable}>
              全选可完成项
            </Button>
            <Button variant="outline" size="sm" onClick={() => setSelected(new Set())}>
              清空选择
            </Button>
            <details className="w-full rounded-md border px-3 py-2 text-sm">
              <summary className="cursor-pointer select-none text-muted-foreground">
                高级选项
                {advancedOn > 0 ? (
                  <span className="ml-1 rounded-full border border-amber-300 bg-amber-50 px-2 py-0.5 text-[11px] text-amber-700">
                    已开启 {advancedOn} 项
                  </span>
                ) : (
                  <span className="ml-1 text-xs">（默认全关，通常不需要动）</span>
                )}
              </summary>
              <div className="mt-3 flex flex-col gap-2">
                <label className="flex items-center space-x-2 text-sm px-2 py-1 rounded border cursor-pointer">
                  <input
                    type="checkbox"
                    checked={examSubmit}
                    onChange={(e) => setExamSubmit(e.target.checked)}
                  />
                  <span>
                    考试交卷
                    <span className="text-xs text-muted-foreground ml-1">
                      （默认关闭：自测/测验只暂存作答，不交卷）
                    </span>
                  </span>
                </label>
                <label className="flex items-center space-x-2 text-sm px-2 py-1 rounded border cursor-pointer">
                  <input
                    type="checkbox"
                    checked={groupSubmit}
                    onChange={(e) => setGroupSubmit(e.target.checked)}
                  />
                  <span>
                    分组任务提交
                    <span className="text-xs text-muted-foreground ml-1">
                      （默认关闭：只生成小组答案不提交）
                    </span>
                  </span>
                </label>
                <label
                  className={`flex items-center space-x-2 text-sm px-2 py-1 rounded border ${
                    groupSubmit ? 'cursor-pointer' : 'opacity-50 cursor-not-allowed'
                  }`}
                  title="小组已有内容时，开启此项才会覆盖组员已写好的答案"
                >
                  <input
                    type="checkbox"
                    disabled={!groupSubmit}
                    checked={groupForceOverwrite}
                    onChange={(e) => setGroupForceOverwrite(e.target.checked)}
                  />
                  <span>
                    强制覆盖小组内容
                    <span className="text-xs text-red-600 ml-1">
                      （危险：会抹掉组员已提交的答案）
                    </span>
                  </span>
                </label>
                <label className="flex items-center space-x-2 text-sm px-2 py-1 rounded border cursor-pointer">
                  <input
                    type="checkbox"
                    checked={allowAiSubmit}
                    onChange={(e) => setAllowAiSubmit(e.target.checked)}
                  />
                  <span>
                    允许 AI实践直接提交
                    <span className="text-xs text-muted-foreground ml-1">
                      （默认关闭：思维阶梯/情景对话未验证，会拒绝直接交卷）
                    </span>
                  </span>
                </label>
                <label className="flex items-center space-x-2 text-sm px-2 py-1 rounded border cursor-pointer">
                  <input
                    type="checkbox"
                    checked={aiRedo}
                    onChange={(e) => setAiRedo(e.target.checked)}
                  />
                  <span>
                    已有作答记录时重做
                    <span className="text-xs text-muted-foreground ml-1">
                      （默认关闭＝续做未完成记录；开启则先结束旧记录再建新卷。
                      平台没有「放弃记录」接口，只能这样重做）
                    </span>
                  </span>
                </label>
                <label className="flex items-center space-x-2 text-sm px-2 py-1 rounded border cursor-pointer">
                  <input
                    type="checkbox"
                    checked={uploadAiText}
                    onChange={(e) => setUploadAiText(e.target.checked)}
                  />
                  <span>
                    上传作品时提交 AI 原文（默认开）
                    <span className="text-xs text-muted-foreground ml-1">
                      （部分题目会写「禁止直接大段复制 AI 输出，用自己的话整理」。
                      默认仍按你的要求提交 AI 原文，并在日志里如实记录；
                      取消勾选则遇到这类题目只生成草稿、不提交）
                    </span>
                  </span>
                </label>

              </div>
            </details>
            <label className="flex items-center space-x-2 text-sm px-2 py-1 rounded border cursor-pointer">
              <input
                type="checkbox"
                checked={redo}
                onChange={(e) => {
                  const checked = e.target.checked;
                  setRedo(checked);
                  // 已完成条目需要重新识别（AI实践子模式等），旧的盘点结果不能复用。
                  // ⚠️ 必须同时清掉 scanTaskId：按钮的文案与禁用条件是
                  // `scanTaskId && !preview && !error`，只清 preview 会残留旧 id，
                  // 界面立刻变成「盘点中...」且按钮被禁用，而轮询并没有启动 —— 永久转圈。
                  setScanTaskId(null);
                  setScanState(null);
                  setPreview(null);
                  setPreviewAt(null);
                  setSelected(new Set());
                  setExpanded(new Set());
                  setPhase('scan');
                  try { window.localStorage.removeItem(PREVIEW_KEY); } catch (err) { /* 忽略 */ }
                }}
              />
              <span>
                重做模式
                <span className="text-xs text-muted-foreground ml-1">
                  （可选中已完成的 AI实践 再答一次；其它类型平台重做流程未探明，会被拒绝。
                  切换后会清空盘点结果，请重新盘点）
                </span>
              </span>
            </label>
            <Button onClick={startExecute} disabled={selected.size === 0}>
              <Play className="mr-2 h-4 w-4" />
              确认执行（已选 {selected.size} 个）
            </Button>
          </CardFooter>
        </Card>
        {renderLogs()}
      </>
    );
  };

  const renderRunning = () => {
    const stats = runState?.stats || {};
    return (
      <>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center justify-between">
              <span className="flex items-center">
                <Loader2 className="mr-2 h-5 w-5 animate-spin text-blue-600" />
                正在自动完成任务...
              </span>
              <Button variant="destructive" size="sm" onClick={handleStop} disabled={stopping}>
                <Square className="mr-2 h-4 w-4" />停止
              </Button>
            </CardTitle>
            <CardDescription>
              {runState?.current_task ? `当前：${runState.current_task}` : '准备中...'}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="grid grid-cols-3 gap-3">
              {[
                { label: '成功', value: stats.done || 0, color: 'text-green-600' },
                { label: '失败', value: stats.failed || 0, color: 'text-red-600' },
                { label: '跳过', value: stats.skipped || 0, color: 'text-gray-500' },
              ].map((item) => (
                <div key={item.label} className="rounded-lg border p-3 text-center">
                  <div className={`text-2xl font-semibold ${item.color}`}>{item.value}</div>
                  <div className="text-xs text-muted-foreground mt-1">{item.label}</div>
                </div>
              ))}
            </div>
          </CardContent>
        </Card>
        {renderLogs()}
      </>
    );
  };

  const renderDone = () => {
    const stats = runState?.stats || {};
    return (
      <>
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center">
              <CheckCircle2 className="mr-2 h-5 w-5 text-green-600" />
              执行完成
            </CardTitle>
            <CardDescription>
              成功 {stats.done || 0} 个 · 失败 {stats.failed || 0} 个 · 跳过 {stats.skipped || 0} 个
            </CardDescription>
          </CardHeader>
          <CardContent>
            {error && (
              <div className="mb-4 flex items-start space-x-2 text-sm text-red-600">
                <AlertCircle className="w-4 h-4 mt-0.5" /><span>{error}</span>
              </div>
            )}
            <div className="space-y-1 max-h-96 overflow-y-auto">
              {results.map((r) => (
                <div key={`${r.planId}-${r.planId}`}
                  className="flex items-start space-x-2 p-2 rounded border text-sm">
                  {r.ok ? (
                    <CheckCircle2 className="w-4 h-4 mt-0.5 text-green-600 flex-shrink-0" />
                  ) : r.skipped ? (
                    <Info className="w-4 h-4 mt-0.5 text-gray-400 flex-shrink-0" />
                  ) : (
                    <XCircle className="w-4 h-4 mt-0.5 text-red-600 flex-shrink-0" />
                  )}
                  <span className="flex-1">
                    <span className="text-xs text-muted-foreground">
                      {r.courseTitle} / {r.taskName} / {r.typeLabel}
                    </span>
                    <div>{r.name}</div>
                    <div className="text-xs text-muted-foreground mt-0.5">{r.message}</div>
                  </span>
                </div>
              ))}
            </div>
          </CardContent>
          <CardFooter className="flex gap-2">
            <Button onClick={startScan} variant="outline">
              <RefreshCw className="mr-2 h-4 w-4" />重新盘点
            </Button>
            <Button onClick={onBack}>
              <Home className="mr-2 h-4 w-4" />返回课程
            </Button>
          </CardFooter>
        </Card>
        {renderLogs()}
      </>
    );
  };

  return (
    <div className="min-h-screen bg-gradient-to-br from-blue-50 to-indigo-100 p-6">
      <div className="max-w-6xl mx-auto">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-3xl font-bold text-gray-900">自动完成任务</h1>
            <p className="text-muted-foreground mt-1">
              盘点并自动完成课程「任务」标签下的任务点
            </p>
          </div>
          <div className="flex gap-2">
            <Button variant="outline" onClick={onBack}>
              <Home className="mr-2 h-4 w-4" />返回
            </Button>
            {onLogout && (
              <Button variant="ghost" onClick={onLogout}>退出登录</Button>
            )}
          </div>
        </div>

        {phase === 'scan' && renderScan()}
        {phase === 'preview' && preview && renderPreview()}
        {phase === 'running' && renderRunning()}
        {phase === 'done' && renderDone()}
        {renderApprovals()}
      </div>
    </div>
  );
};

export default FanyaTasks;


