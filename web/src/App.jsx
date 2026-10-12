import React, { useState, useEffect } from 'react';
import Login from './components/Login';
import CourseSelection from './components/CourseSelection';
import StudyProgress from './components/StudyProgress';
import FanyaTasks from './components/FanyaTasks';
import api from './api/axios';

function App() {
  const [step, setStep] = useState('login');
  const [userInfo, setUserInfo] = useState(null);
  const [taskId, setTaskId] = useState(null);
  // 课程管理页勾选的课程：上传给子页面用，保证「自动完成任务」只盘点勾选的课
  const [allCourses, setAllCourses] = useState([]);
  const [selectedCourses, setSelectedCourses] = useState([]);

  // 桌面版的「关掉窗口即退出」：页面定时心跳；关闭/跳走时用 sendBeacon 通知后端。
  // 后端只有在收到关闭信号且安静 15 秒后才退出，所以刷新页面不会误杀进程。
  useEffect(() => {
    const ping = () => {
      fetch('/api/heartbeat', { method: 'POST' }).catch(() => {});
    };
    ping();
    const timer = setInterval(ping, 5000);
    const bye = () => {
      try {
        navigator.sendBeacon('/api/window-closed');
      } catch (err) {
        /* 忽略 */
      }
    };
    window.addEventListener('pagehide', bye);
    window.addEventListener('beforeunload', bye);
    return () => {
      clearInterval(timer);
      window.removeEventListener('pagehide', bye);
      window.removeEventListener('beforeunload', bye);
    };
  }, []);

  const handleLoginSuccess = (info) => {
    setUserInfo(info);
    setStep('courses');
  };

  const handleSelectionChange = ({ selectedCourses: picked, courses }) => {
    setSelectedCourses(picked || []);
    setAllCourses(courses || []);
  };

  const handleStartStudy = async (settings) => {
    try {
      const response = await api.post('/start', {
        username: userInfo.username,
        password: userInfo.password,
        ...settings,
      });

      if (response.data.status) {
        setTaskId(response.data.data.task_id);
        setStep('progress');
      }
    } catch (err) {
      console.error('启动学习任务失败:', err);
      alert('启动学习任务失败，请重试');
    }
  };

  const handleLogout = () => {
    setUserInfo(null);
    setTaskId(null);
    setStep('login');
  };

  const handleBackToHome = () => {
    setTaskId(null);
    setStep('courses');
  };

  return (
    <div className="App">
      {step === 'login' && <Login onLoginSuccess={handleLoginSuccess} />}
      {step === 'courses' && (
        <CourseSelection
          userInfo={userInfo}
          onStartStudy={handleStartStudy}
          onLogout={handleLogout}
          onOpenFanyaTasks={() => setStep('fanya')}
          onSelectionChange={handleSelectionChange}
        />
      )}
      {step === 'fanya' && (
        <FanyaTasks
          userInfo={userInfo}
          selectedCourseIds={selectedCourses}
          allCourses={allCourses}
          onBack={() => setStep('courses')}
          onLogout={handleLogout}
        />
      )}
      {step === 'progress' && taskId && (
        <StudyProgress taskId={taskId} onBack={handleBackToHome} />
      )}
    </div>
  );
}

export default App;
