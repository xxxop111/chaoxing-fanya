import React, { useState, useEffect } from 'react';
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from './ui/Card';
import Button from './ui/Button';
import Input from './ui/Input';
import Label from './ui/Label';
import { Play, Loader2, BookOpen, Settings, LogOut, ListChecks,
         ChevronRight, Check, User } from 'lucide-react';
import { Disclosure, DisclosureButton, DisclosurePanel } from '@headlessui/react';
import api from '../api/axios';
import AdvancedSettings from './AdvancedSettings';

/**
 * 脱敏显示账号：不要直接把手机号摆在界面上（截图会泄露）。
 * 11 位手机号 → 183****6684；其它长度保留头尾、中间打星。
 */
function maskAccount(name) {
  const s = String(name || '');
  if (!s) return '';
  if (/^\d{11}$/.test(s)) return `${s.slice(0, 3)}****${s.slice(-4)}`;
  if (s.length <= 4) return '****';
  return `${s.slice(0, 2)}${'*'.repeat(Math.max(2, s.length - 4))}${s.slice(-2)}`;
}

const CourseSelection = ({ userInfo, onStartStudy, onLogout, onOpenFanyaTasks,
                          onSelectionChange }) => {
  const [courses, setCourses] = useState([]);
  const [selectedCourses, setSelectedCourses] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showSettings, setShowSettings] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveStatus, setSaveStatus] = useState('');
  const [settings, setSettings] = useState({
    speed: 1.0,
    jobs: 4,
    notopen_action: 'retry',
    max_chapters: 0,
    tiku_config: {},
    notification_config: {},
    ocr_config: {},
  });

  useEffect(() => {
    fetchConfig();
    fetchCourses();
  }, []);

  // 把当前勾选同步给上层：这样点「自动完成任务」时会**只盘点你勾的课程**，
  // 不需要先点「保存当前配置」。
  useEffect(() => {
    if (typeof onSelectionChange === 'function') {
      onSelectionChange({ selectedCourses, courses });
    }
  }, [selectedCourses, courses, onSelectionChange]);

  const fetchConfig = async () => {
    try {
      const response = await api.get('/config');
      if (response.data.status && response.data.data) {
        const cfg = response.data.data;
        if (cfg.settings) {
          setSettings((prev) => ({
            ...prev,
            ...cfg.settings,
          }));
        }
        if (Array.isArray(cfg.selectedCourses)) {
          setSelectedCourses(cfg.selectedCourses);
        }
      }
    } catch (err) {
      console.error('加载已保存配置失败:', err);
    }
  };

  const fetchCourses = async () => {
    try {
      const response = await api.post('/courses', {
        username: userInfo.username,
        password: userInfo.password,
      });

      if (response.data.status) {
        setCourses(response.data.data);
      }
    } catch (err) {
      console.error('获取课程列表失败:', err);
    } finally {
      setLoading(false);
    }
  };

  const toggleCourse = (courseId) => {
    setSelectedCourses((prev) =>
      prev.includes(courseId)
        ? prev.filter((id) => id !== courseId)
        : [...prev, courseId]
    );
  };

  const handleStartStudy = () => {
    onStartStudy({
      ...settings,
      course_list: selectedCourses.length > 0 ? selectedCourses : courses.map(c => c.courseId),
    });
  };

  const handleSaveConfig = async () => {
    try {
      setSaving(true);
      setSaveStatus('');
      const payload = {
        settings,
        selectedCourses,
      };
      const response = await api.post('/config', payload);
      if (!response.data.status) {
        console.error('保存配置失败:', response.data.msg);
        setSaveStatus(response.data.msg || '保存失败');
      } else {
        setSaveStatus('保存成功');
      }
    } catch (err) {
      console.error('保存配置请求失败:', err);
      setSaveStatus('保存请求失败');
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100">
        <div className="text-center">
          <Loader2 className="w-12 h-12 animate-spin text-primary mx-auto mb-4" />
          <p className="text-muted-foreground">加载课程列表中...</p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-50">
      <div className="max-w-6xl mx-auto px-6 py-8">
        {/* 页头：说清"下一步干什么"，并显示当前登录身份 */}
        <div className="flex items-center justify-between mb-8">
          <div>
            <h1 className="text-2xl font-semibold tracking-tight text-gray-900">课程管理</h1>
            <p className="text-sm text-muted-foreground mt-1">
              勾选课程后，用下方两条流程完成学习任务
            </p>
          </div>
          <div className="flex items-center gap-3">
            {userInfo?.username && (
              <span className="hidden items-center gap-2 rounded-full border bg-white px-3 py-1.5 text-sm text-muted-foreground sm:flex">
                <User className="h-3.5 w-3.5" />
                {maskAccount(userInfo.username)}
              </span>
            )}
            <Button variant="outline" size="sm" onClick={onLogout}>
              <LogOut className="mr-2 h-4 w-4" />
              退出登录
            </Button>
          </div>
        </div>

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-2">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center justify-between">
                  <span className="flex items-center">
                    <BookOpen className="mr-2 h-5 w-5" />
                    课程列表
                  </span>
                  <span className={`text-sm font-normal ${
                    selectedCourses.length > 0 ? 'text-primary' : 'text-muted-foreground'
                  }`}>
                    {selectedCourses.length > 0
                      ? `已选 ${selectedCourses.length} 门`
                      : '未选择'}
                  </span>
                </CardTitle>
                <CardDescription>
                  勾选要在下方流程中处理的课程；一门都不勾时会处理全部课程。
                </CardDescription>
              </CardHeader>
              <CardContent>
                <div className="space-y-3 max-h-[500px] overflow-y-auto">
                  {courses.map((course) => (
                    <div
                      key={course.courseId}
                      className={`relative cursor-pointer overflow-hidden rounded-lg border p-4 transition-all ${
                        selectedCourses.includes(course.courseId)
                          ? 'border-primary bg-primary/5 shadow-sm'
                          : 'border-gray-200 bg-white hover:border-gray-300 hover:shadow-sm'
                      }`}
                      onClick={() => toggleCourse(course.courseId)}
                    >
                      <div className="flex items-center justify-between gap-3">
                        <div className="min-w-0 flex-1">
                          <h3 className="truncate font-medium text-gray-900">
                            {course.displayTitle || course.title}
                          </h3>
                        </div>
                        <span className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 transition-colors ${
                          selectedCourses.includes(course.courseId)
                            ? 'border-primary bg-primary'
                            : 'border-gray-300'
                        }`}>
                          {selectedCourses.includes(course.courseId) && (
                            <Check className="h-3 w-3 text-white" />
                          )}
                        </span>
                      </div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
          </div>

          <div className="lg:col-span-1">
            <Card>
              <CardHeader>
                <CardTitle className="flex items-center">
                  <Settings className="mr-2 h-5 w-5" />
                  学习配置
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-4">
                {/* 默认只露常用项；限速/并发/章节限制/OCR/通知等收进「高级设置」。
                    这些参数 99% 的情况不需要动，全部平铺只会淹没新用户。 */}
                <Disclosure>
                  <DisclosureButton className="group flex w-full items-center justify-between rounded-md border px-3 py-2 text-sm text-muted-foreground transition-colors hover:bg-gray-50 hover:text-foreground">
                    <span>
                      高级设置
                      <span className="ml-1 text-xs">
                        （倍速、并发、章节限制、题库、OCR、通知等）
                      </span>
                    </span>
                    <ChevronRight className="h-4 w-4 shrink-0 transition-transform duration-200 group-data-[open]:rotate-90" />
                  </DisclosureButton>
                  <DisclosurePanel
                    transition
                    className="mt-3 space-y-4 origin-top transition duration-200 ease-out data-[closed]:-translate-y-1 data-[closed]:opacity-0"
                  >
                    <div className="space-y-2">
                      <Label htmlFor="speed">播放倍速</Label>
                      <Input
                        id="speed"
                        type="number"
                        min="1"
                        max="2"
                        step="0.1"
                        value={settings.speed}
                        onChange={(e) => setSettings({ ...settings, speed: parseFloat(e.target.value) })}
                      />
                      <p className="text-xs text-muted-foreground">范围: 1.0 - 2.0</p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="jobs">并发章节数</Label>
                      <Input
                        id="jobs"
                        type="number"
                        min="1"
                        max="10"
                        value={settings.jobs}
                        onChange={(e) => setSettings({ ...settings, jobs: parseInt(e.target.value) })}
                      />
                      <p className="text-xs text-muted-foreground">同时处理的章节数量</p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="maxChapters">只刷前 N 章</Label>
                      <Input
                        id="maxChapters"
                        type="number"
                        min="0"
                        value={settings.max_chapters ?? 0}
                        onChange={(e) =>
                          setSettings({
                            ...settings,
                            max_chapters: parseInt(e.target.value || '0', 10) || 0,
                          })
                        }
                      />
                      <p className="text-xs text-muted-foreground">
                        按章节序号限制：只处理前 N 章，第 N 章之后完全不动。填 0 表示不限制。
                      </p>
                    </div>

                    <div className="space-y-2">
                      <Label htmlFor="notopen">未开放章节处理</Label>
                      <select
                        id="notopen"
                        className="flex h-10 w-full rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        value={settings.notopen_action}
                        onChange={(e) => setSettings({ ...settings, notopen_action: e.target.value })}
                      >
                        <option value="retry">重试</option>
                        <option value="ask">询问</option>
                        <option value="continue">跳过</option>
                      </select>
                    </div>

                    <div className="pt-4 border-t">
                      <AdvancedSettings settings={settings} onChange={setSettings} />
                    </div>
                  </DisclosurePanel>
                </Disclosure>
              </CardContent>
              <CardFooter className="flex flex-col space-y-3">
                {/* 两个入口处理的是**两套互不相通的系统**，之前界面上完全没说明，
                    导致「章节测验为什么没做」这类困惑。这里直接讲清分工。 */}
                <div className="w-full rounded-lg border bg-gray-50 p-3">
                  <p className="text-xs font-medium mb-2">两条流程，处理的是两套不同的东西</p>
                  <div className="space-y-2 text-xs text-muted-foreground">
                    <div className="flex gap-2">
                      <span className="shrink-0 rounded border border-blue-200 bg-blue-50 px-1.5 py-0.5 text-[11px] text-blue-700">
                        章节
                      </span>
                      <span>
                        <span className="font-medium text-foreground">开始学习</span>
                        ：课程页「章节」里的任务点 —— 视频、文档、作业、
                        <span className="font-medium text-foreground">章节测验</span>、阅读
                      </span>
                    </div>
                    <div className="flex gap-2">
                      <span className="shrink-0 rounded border border-purple-200 bg-purple-50 px-1.5 py-0.5 text-[11px] text-purple-700">
                        任务
                      </span>
                      <span>
                        <span className="font-medium text-foreground">自动完成任务</span>
                        ：课程页「任务」标签里的任务点 —— AI实践、主题讨论、
                        分组任务、任务里的作业与测验
                      </span>
                    </div>
                  </div>
                </div>

                <Button className="w-full" onClick={handleStartStudy}>
                  <Play className="mr-2 h-4 w-4" />
                  开始学习（章节任务点）
                </Button>
                <Button
                  type="button"
                  variant="secondary"
                  className="w-full"
                  onClick={onOpenFanyaTasks}
                >
                  <ListChecks className="mr-2 h-4 w-4" />
                  自动完成任务（任务标签）
                </Button>
                <button
                  type="button"
                  className="text-xs text-muted-foreground underline underline-offset-2 hover:text-foreground disabled:opacity-50"
                  onClick={handleSaveConfig}
                  disabled={saving}
                >
                  {saving ? '保存中…' : '保存当前配置'}
                </button>
                {saveStatus && (
                  <p className="text-xs text-muted-foreground text-center">
                    {saveStatus}
                  </p>
                )}
              </CardFooter>
            </Card>

            <Card className="mt-4">
              <CardContent className="pt-6">
                <div className="space-y-2 text-sm">
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">总课程数:</span>
                    <span className="font-semibold">{courses.length}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-muted-foreground">已选择:</span>
                    <span className="font-semibold">
                      {selectedCourses.length > 0 ? selectedCourses.length : '全部'}
                    </span>
                  </div>
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
};

export default CourseSelection;
