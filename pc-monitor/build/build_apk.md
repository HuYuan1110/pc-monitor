# 手机 APP（Android APK）打包详细步骤

把网页控制台打包成真正的 Android APK。前提：电脑已安装
[Node.js](https://nodejs.org/) 与 [Android Studio](https://developer.android.com/studio)（含 SDK/Java）。

> 说明：APK 本质是把"访问固定局域网 IP 的网页"包成壳，运行时仍需手机与电脑同局域网。
> 如果只想"像 APP 一样用"，直接看 README 第三节的 PWA 方式即可，无需打包。

## 一、准备页面文件

先确认电脑上的监控代理已在运行（`python agent.py`），然后拉取页面：

```bash
# Windows 命令提示符或 PowerShell
mkdir pcm-apk
cd pcm-apk
curl -o index.html http://127.0.0.1:8888/
```

> 重要：`index.html` 里的数据请求地址是相对路径 `/api/stream`，
> 打包成 APK 后需要指向电脑的固定 IP。建议把电脑 IP 设为**固定 IP**（路由器里做 DHCP 静态绑定），
> 然后打开 `index.html`，把脚本中默认连接地址改为 `http://<电脑IP>:8888`。
> 更简单的做法：保持 PWA 方式，不打包 APK。

## 二、初始化 Capacitor

```bash
cd pcm-apk
npm init -y
npm install @capacitor/core @capacitor/cli @capacitor/android
mkdir www
copy index.html www\index.html
npx cap init "PCMonitor" "com.example.pcmonitor" --web-dir www
npx cap add android
```

## 三、放行明文 HTTP 与联网权限

编辑 `android\app\src\main\AndroidManifest.xml`：

1. 在 `<manifest>` 内加入：
   ```xml
   <uses-permission android:name="android.permission.INTERNET"/>
   <uses-permission android:name="android.permission.ACCESS_NETWORK_STATE"/>
   ```
2. 在 `<application>` 标签上加入：
   ```xml
   android:usesCleartextTraffic="true"
   ```

## 四、打包 APK

```bash
npx cap sync
cd android
gradlew.bat assembleDebug
```

产物：`android\app\build\outputs\apk\debug\app-debug.apk`

拷到手机安装，打开即可看到监控界面（需手机与电脑同一局域网）。

## 附：改回电脑 IP 的通用做法

在 `www\index.html` 里找到 `new EventSource('/api/stream')` 与 `fetch('/api/...')`，
把相对路径替换为绝对地址 `http://192.168.x.x:8888/api/stream` 等，再执行 `npx cap sync`。
