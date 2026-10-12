import React, { useState, useEffect } from 'react';
import { Card, CardHeader, CardTitle, CardDescription, CardContent, CardFooter } from './ui/Card';
import Button from './ui/Button';
import Input from './ui/Input';
import Label from './ui/Label';
import { LogIn, Loader2, ShieldCheck, ShieldAlert } from 'lucide-react';
import api from '../api/axios';

// 「记住密码」把账号密码存在本机浏览器里（localStorage）。
// 这是本机自用工具，文件与配置本来就在你机器上；但仍然明文保存，
// 界面上会明确提示，你可以随时取消勾选把它清掉。
const REMEMBER_KEY = 'chaoxing.rememberedLogin';

const loadRemembered = () => {
  try {
    const raw = window.localStorage.getItem(REMEMBER_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch (err) {
    return null;
  }
};

const Login = ({ onLoginSuccess }) => {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [useCookies, setUseCookies] = useState(false);
  const [remember, setRemember] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  // 打开页面时若之前勾了「记住密码」就自动填上
  useEffect(() => {
    const saved = loadRemembered();
    if (!saved) return;
    setUsername(saved.username || '');
    setPassword(saved.password || '');
    setUseCookies(Boolean(saved.useCookies));
    setRemember(true);
  }, []);

  const handleLogin = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);

    try {
      const response = await api.post('/login', {
        username,
        password,
        use_cookies: useCookies,
      });

      if (response.data.status) {
        try {
          if (remember) {
            window.localStorage.setItem(REMEMBER_KEY, JSON.stringify({
              username, password, useCookies,
            }));
          } else {
            window.localStorage.removeItem(REMEMBER_KEY);
          }
        } catch (err) {
          console.warn('保存登录信息失败:', err);
        }
        onLoginSuccess({ username, password });
      } else {
        setError(response.data.msg || '登录失败');
      }
    } catch (err) {
      setError(err.response?.data?.msg || '网络错误，请稍后重试');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="min-h-screen flex items-center justify-center bg-gradient-to-br from-blue-50 to-indigo-100 p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-1">
          <div className="flex items-center justify-center mb-4">
            <div className="p-3 bg-primary rounded-full">
              <LogIn className="w-8 h-8 text-white" />
            </div>
          </div>
          <CardTitle className="text-center text-3xl">超星学习通</CardTitle>
          <CardDescription className="text-center">
            自动化学习助手 - 登录以继续
          </CardDescription>
        </CardHeader>
        <form onSubmit={handleLogin}>
          <CardContent className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">手机号</Label>
              <Input
                id="username"
                type="text"
                placeholder="请输入手机号"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                required
              />
            </div>
            <div className="space-y-2">
              <Label htmlFor="password">密码</Label>
              <Input
                id="password"
                type="password"
                placeholder="请输入密码"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required={!useCookies}
                disabled={useCookies}
              />
            </div>
            <div className="flex items-center space-x-2">
              <input
                id="use-cookies"
                type="checkbox"
                className="w-4 h-4 rounded border-gray-300 text-primary focus:ring-primary"
                checked={useCookies}
                onChange={(e) => setUseCookies(e.target.checked)}
              />
              <Label htmlFor="use-cookies" className="cursor-pointer text-sm font-normal">
                使用Cookie登录（需提前配置cookies.txt）
              </Label>
            </div>
            <div className="space-y-1">
              <div className="flex items-center space-x-2">
                <input
                  id="remember"
                  type="checkbox"
                  className="w-4 h-4 rounded border-gray-300 text-primary focus:ring-primary"
                  checked={remember}
                  onChange={(e) => {
                    setRemember(e.target.checked);
                    if (!e.target.checked) {
                      try { window.localStorage.removeItem(REMEMBER_KEY); } catch (err) { /* ignore */ }
                    }
                  }}
                />
                <Label htmlFor="remember" className="cursor-pointer text-sm font-normal flex items-center">
                  {remember
                    ? <ShieldCheck className="w-4 h-4 mr-1 text-green-600" />
                    : <ShieldAlert className="w-4 h-4 mr-1 text-amber-500" />}
                  记住密码（下次自动填写）
                </Label>
              </div>
              <p className="text-xs text-muted-foreground pl-6">
                账号密码会以**明文**保存在本机浏览器里；公共电脑上请不要勾选。
              </p>
            </div>
            {error && (
              <div className="p-3 bg-destructive/10 text-destructive text-sm rounded-md">
                {error}
              </div>
            )}
          </CardContent>
          <CardFooter>
            <Button type="submit" className="w-full" disabled={loading}>
              {loading ? (
                <>
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  登录中...
                </>
              ) : (
                <>
                  <LogIn className="mr-2 h-4 w-4" />
                  登录
                </>
              )}
            </Button>
          </CardFooter>
        </form>
      </Card>
    </div>
  );
};

export default Login;
